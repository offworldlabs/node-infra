#!/usr/bin/env python3
"""Push a whole-fleet Mender snapshot to retina-server after each auto-accept pass.

Each retina-server in RETINA_SYNC_TARGETS mirrors the snapshot into its
mender_devices table and answers registration from it, so the Mender token
stays on this host instead of on every droplet.

Every push carries the whole fleet, and the server marks a device missing from
it as absent. So if any page of either fetch fails, nothing is pushed that pass:
a partial fleet would mark real devices absent. There is no retry queue. The
next pass is the retry, and a target that stays unreachable shows up on its own
side as a stale mirror.

The body follows the snapshot contract in retina-server's docs/mender-snapshot.md.
The receiver rejects unknown keys, so the keys here must match it exactly. The
snapshot is not filtered by NODE_ID_PREFIX: the server shows non-ret devices too.

auto_accept.py calls run() after its own pass. `python3 mender_snapshot.py`
pushes one snapshot by hand.

Environment variables:
    MENDER_PAT: Personal Access Token for Mender API (required)
    MENDER_SERVER: Mender server URL (default: https://hosted.mender.io)
    RETINA_SYNC_TARGETS: Comma-separated url=token pairs, one per environment.
        The url is the full endpoint and the token is that server's
        MENDER_SYNC_TOKEN. Unset or empty, no snapshot is built.
"""
import os
import re
import socket
import sys
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

MENDER_SERVER = os.environ.get("MENDER_SERVER", "https://hosted.mender.io")
MENDER_PAT = os.environ.get("MENDER_PAT")
RETINA_SYNC_TARGETS = os.environ.get("RETINA_SYNC_TARGETS", "")

DEVAUTH_PATH = "/api/management/v2/devauth/devices"
INVENTORY_PATH = "/api/management/v1/inventory/devices"
PER_PAGE = 500
# 25,000 devices. A list still going past that is a server misbehaving, not a fleet.
MAX_PAGES = 50
PUSH_TIMEOUT = 10
# Cloudflare's browser integrity check answers a script's default User-Agent with 403 error 1010.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
# Plain http is allowed only to these, for a server running on this host.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

RFC3339 = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})[Tt]([0-9]{2}:[0-9]{2}:[0-9]{2})"
                     r"(?:\.([0-9]{1,9}))?([Zz]|[+-][0-9]{2}:[0-9]{2})")
# Printable ASCII, no space. requests refuses some other header values with an
# error that quotes "Bearer <token>", and run() and push() print that error.
BEARER_TOKEN = re.compile(r"[\x21-\x7e]+")


def _bearer_safe(token: str) -> bool:
    return BEARER_TOKEN.fullmatch(token) is not None


# --- Targets ---


def _allowed_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    return bool(host) and (scheme == "https" or (scheme == "http" and host in LOCAL_HOSTS))


def parse_targets(raw: str | None) -> list[tuple[str, str]]:
    """RETINA_SYNC_TARGETS as (url, token) pairs, in order, bad entries skipped.

    Each entry splits at its first "=", because a token may contain "=".
    Errors name an entry by its position only: a malformed entry may have the
    token where the url should be, and it must not end up in the journal.
    """
    targets = []
    entries = [entry.strip() for entry in (raw or "").split(",")]
    for position, entry in enumerate((entry for entry in entries if entry), start=1):
        url, sep, token = entry.partition("=")
        url, token = url.strip(), token.strip()
        if not sep:
            problem = 'has no "="'
        elif not url or not token:
            problem = "has an empty url or token"
        elif not _allowed_url(url):
            problem = "is not an https url (http only for localhost)"
        elif not _bearer_safe(token):
            problem = "has a token with a space, control or non-ASCII character"
        else:
            targets.append((url, token))
            continue
        print(f"Error: RETINA_SYNC_TARGETS entry {position} {problem}, skipping it", file=sys.stderr)
    return targets


# --- Fetch ---


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    """UTC whole seconds with a "Z", the contract's timestamp format.

    isoformat rather than strftime, because isoformat always pads the year to
    four digits, so even Go's zero time comes out in a shape the receiver accepts.
    """
    return moment.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat() + "Z"


def fetch_all(path: str) -> list:
    """Every entry of a paged Mender list, deduplicated by id.

    Raises on any failed or malformed page rather than returning what it has,
    because a partial fleet pushed as the whole one would mark devices absent.
    Stops at the first empty page, or at a page that adds no new id after a
    short one, which is what a server ignoring `page` returns. After a full
    page that same repeat raises: the server may be ignoring `page` with more
    of the list still unread.
    """
    entries: list = []
    seen: set[str] = set()
    # Only a short page shows the list has ended. Page 1 has none before it,
    # so a first page with entries but no ids raises too.
    previous_full = True
    for page in range(1, MAX_PAGES + 1):
        resp = requests.get(
            f"{MENDER_SERVER}{path}",
            params={"page": page, "per_page": PER_PAGE},
            headers={"Authorization": f"Bearer {MENDER_PAT}"},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not isinstance(batch, list):
            raise ValueError(f"{path} page {page} is not a list")
        if not batch:
            return entries
        known = len(seen)
        fresh = []
        for entry in batch:
            entry_id = entry.get("id") if isinstance(entry, dict) else None
            if isinstance(entry_id, str) and entry_id:
                if entry_id in seen:
                    continue
                seen.add(entry_id)
            # Entries without an id are kept so build_snapshot can report them.
            fresh.append(entry)
        if len(seen) == known:
            if previous_full:
                raise RuntimeError(f"{path} page {page} has entries but no new id, so the list may be partial")
            return entries
        entries.extend(fresh)
        previous_full = len(batch) >= PER_PAGE
    raise RuntimeError(f"{path} still had entries after {MAX_PAGES} pages")


# --- Snapshot ---


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _nonempty_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def normalise_ts(value: object) -> str | None:
    """An RFC 3339 timestamp as UTC whole seconds with a "Z", or None if unreadable.

    Mender writes up to nine fractional digits; the fraction is cut to the six
    datetime holds, and then dropped, so seconds are truncated, not rounded.
    """
    if not isinstance(value, str):
        return None
    match = RFC3339.fullmatch(value)
    if not match:
        return None
    date, time_of_day, fraction, offset = match.groups()
    fraction = f".{fraction[:6].ljust(6, '0')}" if fraction else ""
    offset = "+00:00" if offset in ("Z", "z") else offset
    try:
        return _stamp(datetime.fromisoformat(f"{date}T{time_of_day}{fraction}{offset}"))
    except (ValueError, OverflowError):
        # Out-of-range fields, or an offset that pushes the date past year 1 or 9999.
        return None


def auth_state(device: dict) -> tuple[str, str | None, str | None]:
    """(auth_status, auth_set_id, auth_set_pubkey) for one devauth device.

    The same rule as _record in retina-server's services/mender.py, so the
    mirror and the live lookup agree. One accepted auth set is the device's
    key. More than one, or a device saying accepted with none, is "ambiguous":
    picking one would bless whichever key happened to come first. auth_sets
    that is not a list of dicts counts as having no accepted set.
    With one accepted set, a junk status, or "accepted" missing its id or pubkey, is "ambiguous" too.
    """
    auth_sets = device.get("auth_sets")
    accepted = []
    if isinstance(auth_sets, list) and all(isinstance(auth_set, dict) for auth_set in auth_sets):
        accepted = [auth_set for auth_set in auth_sets if auth_set.get("status") == "accepted"]
    if len(accepted) > 1:
        return "ambiguous", None, None
    if not accepted:
        status = _nonempty_str(device.get("status")) or "pending"
        return ("ambiguous" if status == "accepted" else status), None, None
    # The default applies only when the key is absent, as in _record, so a
    # junk status is never read as "accepted".
    status = device.get("status", "accepted")
    set_id, pubkey = accepted[0].get("id"), accepted[0].get("pubkey")
    if not _nonempty_str(status):
        return "ambiguous", None, None
    # The contract wants the accepted set's id and pubkey. The receiver would
    # store "accepted" without them with an issue, so it is sent as "ambiguous".
    if status == "accepted" and not (_nonempty_str(set_id) and _nonempty_str(pubkey)):
        return "ambiguous", None, None
    # A device status that disagrees with its accepted set is passed through,
    # as _record does. Registration refuses anything but "accepted" anyway.
    return status, _str_or_none(set_id), _str_or_none(pubkey)


def _attribute(attributes: object, name: str) -> str | None:
    """An inventory attribute's value, from the "inventory" scope if it has one."""
    if not isinstance(attributes, list):
        return None
    named = [attr for attr in attributes if isinstance(attr, dict) and attr.get("name") == name]
    chosen = next((attr for attr in named if attr.get("scope") == "inventory"), named[0] if named else None)
    return _str_or_none(chosen.get("value")) if chosen else None


def device_entry(device: dict, inventory: dict | None) -> dict:
    """One device in the contract's shape. inventory is None when Mender has none for it."""
    identity = device.get("identity_data")
    # An empty node_id goes out as null: the mirror keeps node_id unique.
    node_id = _nonempty_str(identity.get("node_id")) if isinstance(identity, dict) else None
    auth_status, auth_set_id, auth_set_pubkey = auth_state(device)
    attributes = inventory.get("attributes") if inventory else None
    return {
        "mender_device_id": device["id"],
        "node_id": node_id,
        "auth_status": auth_status,
        "auth_set_id": auth_set_id,
        "auth_set_pubkey": auth_set_pubkey,
        "device_type": _attribute(attributes, "device_type"),
        "artifact_name": _attribute(attributes, "artifact_name"),
        "inventory_updated_at": normalise_ts(inventory.get("updated_ts")) if inventory else None,
    }


def build_snapshot(generated_at: str, devices: list, inventory: list) -> dict:
    """The request body from the devauth and inventory lists, joined on device id."""
    by_id: dict[str, dict] = {}
    for entry in inventory:
        if isinstance(entry, dict) and _nonempty_str(entry.get("id")):
            by_id.setdefault(entry["id"], entry)

    entries = []
    for device in devices:
        device_id = device.get("id") if isinstance(device, dict) else None
        if not _nonempty_str(device_id):
            print(f"Warning: Mender device with no id left out of the snapshot: {str(device)[:200]}",
                  file=sys.stderr)
            continue
        entries.append(device_entry(device, by_id.get(device_id)))

    # Two devices claiming one node_id are both sent; the server decides.
    for node_id, count in Counter(entry["node_id"] for entry in entries if entry["node_id"]).items():
        if count > 1:
            ids = ", ".join(entry["mender_device_id"] for entry in entries if entry["node_id"] == node_id)
            print(f"Warning: node_id {node_id} is on {count} Mender devices ({ids})", file=sys.stderr)

    host = socket.gethostname().split(".")[0]
    return {"generated_at": generated_at, "source": f"mender-auto-accept@{host}", "devices": entries}


def collect() -> dict:
    """Fetch the whole fleet and build the body. Raises if any page fails.

    generated_at is stamped before the first request, so the server's
    staleness check measures from the oldest data in the snapshot.
    """
    generated_at = _stamp(_now())
    devices = fetch_all(DEVAUTH_PATH)
    inventory = fetch_all(INVENTORY_PATH)
    return build_snapshot(generated_at, devices, inventory)


# --- Push ---


def _label(url: str) -> str:
    """Host and path, for log lines."""
    parts = urlsplit(url)
    return parts.netloc.rpartition("@")[2] + parts.path


def push(body: dict, targets: list[tuple[str, str]]) -> int:
    """POST the body to each target in turn. Returns how many took it.

    One target failing never stops the next. A 409 counts as delivered: it
    means a newer snapshot already landed there. Redirects are not followed,
    so the token goes to the configured url and nowhere else.
    """
    pushed = 0
    for url, token in targets:
        label = _label(url)
        try:
            resp = requests.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
                timeout=PUSH_TIMEOUT,
                allow_redirects=False,
            )
        except Exception as e:
            print(f"Error pushing snapshot to {label}: {e}", file=sys.stderr)
            continue
        if 200 <= resp.status_code < 300:
            try:
                counts = resp.json()
            except ValueError:
                counts = None
            if not isinstance(counts, dict):
                counts = {}
            print(f"Pushed snapshot to {label}: {len(body['devices'])} devices, "
                  f"accepted {counts.get('accepted', '?')}, rejected {counts.get('rejected', '?')}, "
                  f"absent {counts.get('absent', '?')}")
            pushed += 1
        elif resp.status_code == 409:
            print(f"Snapshot not applied at {label}: a newer one already landed (409)")
            pushed += 1
        else:
            detail = f"HTTP {resp.status_code}"
            if 300 <= resp.status_code < 400:
                detail += f" (redirect to {resp.headers.get('Location')}, not followed)"
            if resp.text:
                detail += f": {resp.text[:200]}"
            print(f"Error pushing snapshot to {label}: {detail}", file=sys.stderr)
    print(f"Mender snapshot: pushed to {pushed} of {len(targets)} targets")
    return pushed


def run() -> None:
    """Build and push one snapshot. Never raises, so it cannot change
    auto_accept's exit status or bury its traceback."""
    try:
        if not MENDER_PAT:
            return
        targets = parse_targets(RETINA_SYNC_TARGETS)
        if not targets:
            return
        if not _bearer_safe(MENDER_PAT):
            print("Error: Mender snapshot not pushed: MENDER_PAT has a space, control or non-ASCII character",
                  file=sys.stderr)
            return
        push(collect(), targets)
    except Exception as e:
        print(f"Error: Mender snapshot not pushed: {e}", file=sys.stderr)


if __name__ == "__main__":
    run()
