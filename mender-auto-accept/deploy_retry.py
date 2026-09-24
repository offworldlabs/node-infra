#!/usr/bin/env python3
"""Re-issue Mender deployments that failed before the artifact reached the node.

Hosted Mender has a built-in "Retries" field on a deployment, but it is gated
behind a paid plan and this tenant is on `os`. Asking for it returns
403 "Feature not available in your Plan.", so the retry has to live out here.

Why a fresh deployment rather than leaning on the client's own retry: the client
does resume a broken download, but only while the error looks like a broken
stream. Once the CDN answers with an unexpected HTTP status the client treats it
as fatal. That is how nightcrawler2 lost owl-os-pi5-v0.16.1 on 2026-09-03: two
truncated streams, then a resumed range request that R2 answered 400, and the
deployment was over three minutes after it began, with eight of its ten retries
unused. A new deployment starts the download from zero against a newly signed
URL, which is what the hand fix does.


WHAT COUNTS AS FIXABLE

Only one thing: the artifact never finished arriving. Nothing was written to the
node, nothing about the node caused it, so an identical request has a genuinely
independent chance of succeeding. Everything else is refused, including failures
we cannot read, because the cost of being wrong is asymmetric. A missed retry
costs one hand deployment. A wrong retry reboots a live radar node on a timer,
repeatedly, for a reason that will not change.

So the test is an allowlist, not a blocklist. A failure is retried only if the
log positively shows the fetch failing, with nothing to suggest the node's own
state was involved. Silence, a truncated log, an unfamiliar error and an API
error all fall through to "do not touch it".

Two properties of Mender's device log make the naive version of this wrong, and
both were measured on real fleet logs rather than assumed:

  * The log is cumulative across attempts, not per attempt. nightcrawler1's
    carries three attempts at the same deployment. Matching over the whole thing
    would let a disk-full line from August veto a retry forever, and let an old
    transport error authorise one. So only the final attempt is considered.

  * "Installing artifact..." is printed about a second after the deployment
    starts, BEFORE the download, so it does not mark the install phase. Wilderness
    A's disk-full log shows it one second in, hours before nightcrawler1's
    download gave up. Phase cannot be inferred from it.

Runs on a timer, reports by default, and only creates deployments with --apply,
matching tunnel_sync.py.

Environment variables:
    MENDER_PAT: Personal Access Token for Mender API (required)
    MENDER_SERVER: Mender server URL (default: https://hosted.mender.io)
    DEPLOY_RETRY_MAX_ATTEMPTS: Retries per device+artifact (default: 2)
    DEPLOY_RETRY_MAX_PER_PASS: Deployments one pass may create (default: 5)
    DEPLOY_RETRY_BACKOFF_SECONDS: Wait between attempts (default: 1800)
    DEPLOY_RETRY_WINDOW_DAYS: How far back to consider failures (default: 7)
    DEPLOY_RETRY_STATE_FILE: Where attempt counts are recorded
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

MENDER_SERVER = os.environ.get("MENDER_SERVER", "https://hosted.mender.io")
MENDER_PAT = os.environ.get("MENDER_PAT")
MAX_ATTEMPTS = int(os.environ.get("DEPLOY_RETRY_MAX_ATTEMPTS", "2"))
MAX_PER_PASS = int(os.environ.get("DEPLOY_RETRY_MAX_PER_PASS", "5"))
BACKOFF_SECONDS = int(os.environ.get("DEPLOY_RETRY_BACKOFF_SECONDS", "1800"))
WINDOW_DAYS = int(os.environ.get("DEPLOY_RETRY_WINDOW_DAYS", "7"))

# systemd sets STATE_DIRECTORY from the unit's StateDirectory=. Preferring it
# means the packaged unit needs no path in its EnvironmentFile, and a hand run
# still works from the checkout.
_DEFAULT_STATE_DIR = os.environ.get("STATE_DIRECTORY") or os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("DEPLOY_RETRY_STATE_FILE",
                            os.path.join(_DEFAULT_STATE_DIR, ".deploy-retries.json"))
HEADERS = {"Authorization": f"Bearer {MENDER_PAT}"} if MENDER_PAT else {}

# The per-device deployments endpoint rejects anything above 20 outright, with
# an error object rather than a list. Measured, not guessed.
PER_PAGE = 20

# A device is mid-update in any of these, so leave it alone rather than stacking
# a second deployment behind the one it is already working through.
ACTIVE_STATUSES = frozenset({
    "pending", "downloading", "installing", "rebooting",
    "pause_before_installing", "pause_before_rebooting", "pause_before_committing",
})

# Reaching the artifact counts either way: "already-installed" is what Mender
# reports when the device turns out to have it, which is a recovery, not a miss.
LANDED_STATUSES = frozenset({"success", "already-installed"})

# Splits the cumulative device log into attempts. The client writes this line
# once per attempt, including repeats of the same deployment id.
ATTEMPT_START = re.compile(r"Deployment with ID \S+ started")

# The artifact never finished arriving. These are the only failures retried, and
# both are Mender's own wording for giving up on the fetch itself.
FETCH_FAILED = [
    re.compile(r"Unexpected status code while fetching artifact", re.I),
    re.compile(r"Giving up on resuming the download", re.I),
]

# The node's own condition caused the failure and still holds, so an identical
# deployment fails identically. Overrides everything.
DEVICE_STATE = [
    (re.compile(r"No space left on device", re.I), "no disk space on the node"),
    (re.compile(r"artifact_too_big|artifact is too big", re.I), "artifact too big for the device"),
    (re.compile(r"not compatible with device", re.I), "artifact incompatible with the device"),
    (re.compile(r"signature verification failed|invalid signature", re.I), "artifact signature rejected"),
]

# An update module or state script ran and failed. The bytes arrived; what they
# did on the node is the problem, and repeating it repeats the problem. Vetoes a
# retry even alongside a fetch error, since a cancelled GET is a normal
# consequence of an install aborting.
PROCESS_FAILED = re.compile(r"Process returned non-zero exit status|ArtifactRollback", re.I)


def api(path: str, params: dict | None = None, version: str = "v1") -> list | dict | None:
    """GET a management API path. Returns None rather than raising, so one bad
    response cannot strand the rest of the pass.

    Deployments are v1 and devauth is v2, so the version is explicit. Defaulting
    it silently would 404 every devauth call, which reads as "device is gone"
    and skips the whole fleet.
    """
    try:
        resp = requests.get(f"{MENDER_SERVER}/api/management/{version}/{path}", headers=HEADERS,
                            params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  API error on {path}: {e}", file=sys.stderr)
        return None


def device_log(deployment_id: str, device_id: str) -> str | None:
    """Fetch a device's log for one deployment.

    None means we could not read it, which is not the same as a log with nothing
    interesting in it: the first must never authorise a retry.
    """
    try:
        resp = requests.get(
            f"{MENDER_SERVER}/api/management/v1/deployments/deployments/{deployment_id}/devices/{device_id}/log",
            headers=HEADERS, timeout=30,
        )
        resp.raise_for_status()
        return resp.text
    except requests.RequestException:
        return None


def last_attempt(log: str) -> str:
    """The tail of the log from the final attempt marker onwards.

    Returns the whole log when there is no marker, which only affects clients
    older than the ones in this fleet, and is still safe: the classifier is
    allowlist-based, so extra text can only cause a refusal, never a retry it
    would not otherwise allow.
    """
    matches = list(ATTEMPT_START.finditer(log))
    return log[matches[-1].start():] if matches else log


def classify(log: str | None) -> tuple[bool, str]:
    """Decide whether a new deployment can fix this failure.

    Returns (retry, reason). Default is False: only a positively identified
    fetch failure, with no sign the node's own state was involved, is retried.
    """
    if log is None:
        return False, "could not read the deployment log"

    tail = last_attempt(log)
    if not tail.strip():
        return False, "no deployment log to read"

    for pattern, reason in DEVICE_STATE:
        if pattern.search(tail):
            return False, reason
    if PROCESS_FAILED.search(tail):
        return False, "an update step ran and failed on the node"
    if any(pattern.search(tail) for pattern in FETCH_FAILED):
        return True, "the artifact never finished downloading"
    return False, "failed for an unrecognised reason"


def parse_ts(value: str | None) -> datetime | None:
    """Parse Mender's RFC3339 timestamps, which carry a Z and sub-second digits."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def recent_failures() -> dict[tuple[str, str], dict]:
    """Find each device+artifact whose most recent deployment ended in failure.

    Keyed on the pair because that is the unit a retry addresses: the same node
    failing a different artifact is a separate problem with its own allowance.
    Only the newest failure for a pair is kept, so an old failure cannot revive
    a pair that has since failed again and been counted.
    """
    deployments = api("deployments/deployments", {"per_page": 100})
    if not isinstance(deployments, list):
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    found: dict[tuple[str, str], dict] = {}

    for dep in deployments:
        if not isinstance(dep, dict):
            continue
        created = parse_ts(dep.get("created"))
        if not created or created < cutoff:
            continue
        if not dep.get("statistics", {}).get("status", {}).get("failure"):
            continue

        artifact = dep.get("artifact_name")
        devices = api(f"deployments/deployments/{dep['id']}/devices")
        if not isinstance(devices, list) or not artifact:
            continue

        for dev in devices:
            if not isinstance(dev, dict) or dev.get("status") != "failure":
                continue
            key = (dev["id"], artifact)
            previous = found.get(key)
            if previous and previous["created"] >= created:
                continue
            found[key] = {
                "device_id": dev["id"],
                "artifact": artifact,
                "deployment_id": dep["id"],
                "created": created,
            }

    return found


def device_history(device_id: str) -> list[dict]:
    """A device's deployments, newest first."""
    history = api(f"deployments/deployments/devices/{device_id}", {"per_page": PER_PAGE})
    if not isinstance(history, list):
        return []
    return sorted(
        (h for h in history if isinstance(h, dict)),
        key=lambda h: h.get("deployment", {}).get("created") or "",
        reverse=True,
    )


def is_busy(history: list[dict]) -> bool:
    """Whether the device is already working through a deployment."""
    return any(h.get("device", {}).get("status") in ACTIVE_STATUSES for h in history)


def has_landed(history: list[dict], artifact: str, after: datetime) -> bool:
    """Whether the artifact reached the device after the failure we are looking at.

    Guards against retrying something a person already fixed by hand, and is why
    inventory's artifact_name is not used for this: a node running both a rootfs
    and a docker-compose artifact reports only the most recently installed of the
    two, so the OS can be a version behind while artifact_name looks current.
    That is precisely nightcrawler2's state, and reading it naively would mark a
    failed OS update as landed.
    """
    for entry in history:
        if entry.get("deployment", {}).get("artifact_name") != artifact:
            continue
        if entry.get("device", {}).get("status") not in LANDED_STATUSES:
            continue
        created = parse_ts(entry.get("deployment", {}).get("created"))
        if created and created > after:
            return True
    return False


def is_accepted(device_id: str) -> bool:
    """Whether Mender still holds the device as accepted.

    A network error answers True, matching auto_accept.is_device_accepted: a blip
    talking to Mender is not evidence a node was decommissioned, and the next
    pass will ask again. Only a definite 404 or a non-accepted status stops the
    retry.
    """
    try:
        resp = requests.get(
            f"{MENDER_SERVER}/api/management/v2/devauth/devices/{device_id}",
            headers=HEADERS,
            timeout=30,
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return resp.json().get("status") == "accepted"
    except requests.RequestException:
        return True


def create_retry(device_id: str, artifact: str, attempt: int) -> str | None:
    """Create a single-device deployment. Returns its id, or None on failure.

    Deliberately no `retries` field: this tenant's plan rejects the whole request
    with a 403 if one is present, which would fail every retry we make.
    """
    name = f"retry{attempt}-{device_id[:8]}-{artifact}"
    try:
        resp = requests.post(
            f"{MENDER_SERVER}/api/management/v1/deployments/deployments",
            headers=HEADERS,
            json={"name": name[:200], "artifact_name": artifact, "devices": [device_id]},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.headers.get("Location", "").rsplit("/", 1)[-1] or "created"
    except requests.RequestException as e:
        print(f"  Error creating retry deployment: {e}", file=sys.stderr)
        return None


def load_state() -> dict[str, dict]:
    """Load {"<device_id>|<artifact>": {"attempts", "last_attempt"}}."""
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, dict]) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def check_state_writable() -> bool:
    """Prove the attempt counts can be persisted, before anything is created.

    The counts are the only thing between a node that fails identically every
    time and an unbounded loop of deployments against it. If they cannot be
    written, the safe move is to do nothing at all: creating deployments and
    then failing to record them is how a retry becomes a storm. This is not
    hypothetical under the packaged unit, where ProtectSystem=strict leaves the
    checkout read-only and only StateDirectory writable.
    """
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        save_state(load_state())
        return True
    except OSError as e:
        print(f"Error: cannot write state file {STATE_FILE}: {e}", file=sys.stderr)
        return False


def prune_state(state: dict[str, dict], live: set[str]) -> dict[str, dict]:
    """Drop pairs that no longer have a failure in the window.

    Without this the file grows forever, and a node that failed months ago would
    keep its exhausted count and be refused a retry the next time it genuinely
    needs one.
    """
    return {k: v for k, v in state.items() if k in live}


def main() -> int:
    parser = argparse.ArgumentParser(description="Retry Mender deployments that failed to download.")
    parser.add_argument("--apply", action="store_true",
                        help="create the deployments (default: report what would happen)")
    args = parser.parse_args()

    if not MENDER_PAT:
        print("Error: MENDER_PAT environment variable not set", file=sys.stderr)
        return 1
    if args.apply and not check_state_writable():
        return 1

    failures = recent_failures()
    state = load_state()

    if not failures:
        print(f"No failed deployments in the last {WINDOW_DAYS} days")
        if args.apply:
            save_state({})
        return 0

    print(f"{len(failures)} failed device+artifact pair(s) in the last {WINDOW_DAYS} days")
    now = time.time()
    created = 0

    for (device_id, artifact), failure in sorted(failures.items()):
        key = f"{device_id}|{artifact}"
        label = f"{device_id[:8]} {artifact}"
        record = state.setdefault(key, {"attempts": 0, "last_attempt": 0})

        retry, reason = classify(device_log(failure["deployment_id"], device_id))
        if not retry:
            print(f"  {label}: not retrying, {reason}")
            continue
        if record["attempts"] >= MAX_ATTEMPTS:
            print(f"  {label}: giving up after {record['attempts']} attempt(s), {reason}")
            continue

        waited = now - record["last_attempt"]
        if record["attempts"] and waited < BACKOFF_SECONDS:
            print(f"  {label}: backing off, {BACKOFF_SECONDS - waited:.0f}s left")
            continue

        history = device_history(device_id)
        if has_landed(history, artifact, failure["created"]):
            print(f"  {label}: already landed since the failure, clearing")
            record["attempts"] = 0
            continue
        if is_busy(history):
            print(f"  {label}: already running a deployment, leaving it")
            continue
        if not is_accepted(device_id):
            print(f"  {label}: not accepted on Mender, skipping")
            continue

        attempt = record["attempts"] + 1
        if not args.apply:
            print(f"  {label}: would retry ({reason}), attempt {attempt}/{MAX_ATTEMPTS}")
            continue

        # A cap on one pass, so a fleet-wide outage cannot turn into a fleet-wide
        # burst of deployments. What is left over is picked up next pass.
        if created >= MAX_PER_PASS:
            print(f"  {label}: deferred, {MAX_PER_PASS} retries already created this pass")
            continue

        deployment_id = create_retry(device_id, artifact, attempt)
        if not deployment_id:
            continue

        # Recorded immediately, not at the end of the pass. If this process dies
        # mid-loop, the attempts already made must still be counted, or the next
        # pass repeats them with a fresh allowance.
        record["attempts"] = attempt
        record["last_attempt"] = now
        created += 1
        try:
            save_state(state)
        except OSError as e:
            print(f"Error: state write failed after creating {deployment_id}: {e}", file=sys.stderr)
            return 1
        print(f"  {label}: retry {attempt}/{MAX_ATTEMPTS} created ({reason}) -> {deployment_id}")

    if args.apply:
        save_state(prune_state(state, {f"{d}|{a}" for d, a in failures}))
        if created:
            print(f"Created {created} retry deployment(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
