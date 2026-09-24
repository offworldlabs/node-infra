#!/usr/bin/env python3
"""Per-node report on whether a release has landed, and if not, why.

A rollout used to be judged by reading its statistics, and a node that missed
it was found weeks later by accident: ret9573ecda failed two releases in a row
and ran with no radar for four weeks. This answers, for every accepted node:

  landed          on both target versions (flagged if its stack is unhealthy)
  pending         the rollout is waiting for it to check in; nothing to do
  test build      running a -dev build (Josh Test Node and Josh Test Node 2)
  retryable       failed for a reason deploy_retry would retry
  needs a person  failed for a reason no retry fixes
  not targeted    no deployment of the target reached it (accepted after the
                  rollout was created, for example)

Versions come from the rootfs-image and docker-compose inventory attributes,
never artifact_name, which is only the last artifact of any type. Failure
reasons come from deploy_retry.classify, so this and the retry agree.

Usage:
    rollout_report.py                       latest stable of each, from Mender
    rollout_report.py --retina-node retina-node-v0.4.6.0 --owl-os owl-os-pi5-v0.17.0
    rollout_report.py --json
"""

import argparse
import json
import re
import sys

import deploy_retry
from deploy_retry import api, device_history

OS_ATTR = "rootfs-image.owl-os-pi5.version"
STACK_ATTR = "data-docker.mender-docker-compose.retina-node.version"
PENDING = deploy_retry.ACTIVE_STATUSES
LANDED = deploy_retry.LANDED_STATUSES

# Stable only: owl-os is three-part, retina-node four-part. -dev and -rc are out.
STABLE = {
    "owl-os": re.compile(r"^owl-os-pi5-v(\d+)\.(\d+)\.(\d+)$"),
    "retina-node": re.compile(r"^retina-node-v(\d+)\.(\d+)\.(\d+)\.(\d+)$"),
}


def latest_stable(kind: str, names: list[str]) -> str | None:
    """The highest stable artifact name of one kind."""
    best = None
    for name in names:
        match = STABLE[kind].match(name)
        if match:
            version = tuple(int(x) for x in match.groups())
            if best is None or version > best[0]:
                best = (version, name)
    return best[1] if best else None


def on_target(attrs: dict, target_os: str, target_stack: str) -> tuple[bool, bool]:
    """Whether the node runs each target, from the per-type inventory attributes."""
    os_ok = f"owl-os-pi5-{attrs.get(OS_ATTR)}" == target_os
    stack_ok = attrs.get(STACK_ATTR) == target_stack
    return os_ok, stack_ok


def artifact_state(history: list[dict], artifact: str) -> tuple[str | None, str | None]:
    """The newest deployment of `artifact` to this node: (status, deployment id)."""
    for entry in history:
        if entry.get("deployment", {}).get("artifact_name") == artifact:
            return entry.get("device", {}).get("status"), entry.get("deployment", {}).get("id")
    return None, None


def missing_verdict(status: str | None, reason: str | None, retryable: bool,
                    in_rollout: bool = False, check_in: str = "") -> tuple[str, str]:
    """Verdict for one target the node is not on. Pure, for testing.

    A rollout hands a device its deployment only when the device checks in, so
    a node that was accepted before the rollout but has been offline since has
    no deployment yet and is still covered (Roswell). `in_rollout` says so.
    """
    if status is None and in_rollout:
        return "pending", f"offline since {check_in or 'unknown'}; the rollout reaches it when it checks in"
    if status is None:
        return "not targeted", "no deployment of it has reached this node"
    if status in PENDING:
        return "pending", f"deployment {status}"
    if status in LANDED:
        return "landed", "deployment succeeded but inventory has not caught up"
    if status == "failure":
        return ("retryable", reason or "") if retryable else ("needs a person", reason or "")
    return "needs a person", f"deployment {status}"


RANK = ["needs a person", "not targeted", "retryable", "pending", "test build", "landed"]


def is_test_build(attrs: dict) -> bool:
    return any("-dev" in str(attrs.get(k) or "") for k in (OS_ATTR, STACK_ATTR))


def rollout_start(deployments: list[dict], artifact: str) -> str | None:
    """When the first fleet-wide deployment of `artifact` was created.

    Fleet-wide means the "All devices" kind, which carries a filter on accepted
    devices; a single-device deployment says nothing about who else is covered.
    """
    starts = [d.get("created", "") for d in deployments
              if d.get("artifact_name") == artifact and (d.get("filter") or {}).get("terms")]
    return min(starts) if starts else None


def node_verdict(per_target: list[tuple[str, str]], stack: str | None) -> tuple[str, str]:
    """Worst verdict across targets; a landed node with an unhealthy stack is flagged."""
    worst = min(per_target, key=lambda v: RANK.index(v[0]))
    if worst[0] == "landed" and stack and stack not in ("up", "absent"):
        return "landed", f"but retina_stack={stack}"
    why = "; ".join(w for v, w in per_target if v != "landed" and w)
    return worst[0], why


def report(target_os: str, target_stack: str) -> list[dict]:
    rows = []
    deployments = api("deployments/deployments", {"per_page": 100}) or []
    starts = {a: rollout_start(deployments, a) for a in (target_os, target_stack)}
    devices = api("inventory/devices", {"per_page": 100}) or []
    for dev in devices:
        attrs = {}
        for a in dev.get("attributes", []):
            attrs.setdefault(a["name"], a["value"])
        if attrs.get("status") != "accepted":
            continue
        name = attrs.get("name") or attrs.get("node_id") or dev["id"][:8]
        if is_test_build(attrs):
            rows.append({"name": name, "device": dev["id"], "os": attrs.get(OS_ATTR),
                         "stack": attrs.get(STACK_ATTR), "retina_stack": attrs.get("retina_stack"),
                         "check_in": (attrs.get("check_in_time") or "")[:16],
                         "verdict": "test build", "why": ""})
            continue
        os_ok, stack_ok = on_target(attrs, target_os, target_stack)
        per_target = []
        history = None
        for artifact, ok in ((target_os, os_ok), (target_stack, stack_ok)):
            if ok:
                per_target.append(("landed", ""))
                continue
            if history is None:
                history = device_history(dev["id"])
            status, dep_id = artifact_state(history, artifact)
            retryable, reason = False, None
            if status == "failure" and dep_id:
                retryable, reason = deploy_retry.classify(deploy_retry.device_log(dep_id, dev["id"]))
            start = starts.get(artifact)
            in_rollout = bool(start) and (attrs.get("created_ts") or "") < start
            verdict, why = missing_verdict(status, reason, retryable, in_rollout,
                                           (attrs.get("check_in_time") or "")[:16])
            per_target.append((verdict, f"{artifact}: {why}" if why else artifact))
        verdict, why = node_verdict(per_target, attrs.get("retina_stack"))
        rows.append({
            "name": name,
            "device": dev["id"],
            "os": attrs.get(OS_ATTR),
            "stack": attrs.get(STACK_ATTR),
            "retina_stack": attrs.get("retina_stack"),
            "check_in": (attrs.get("check_in_time") or "")[:16],
            "verdict": verdict,
            "why": why,
        })
    return sorted(rows, key=lambda r: (RANK.index(r["verdict"]), r["name"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--owl-os", help="target OS artifact name (default: latest stable)")
    parser.add_argument("--retina-node", help="target retina-node artifact name (default: latest stable)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args()

    if not deploy_retry.MENDER_PAT:
        print("MENDER_PAT is not set", file=sys.stderr)
        return 1
    names = [a.get("name", "") for a in (api("deployments/artifacts") or [])]
    target_os = args.owl_os or latest_stable("owl-os", names)
    target_stack = args.retina_node or latest_stable("retina-node", names)
    if not target_os or not target_stack:
        print("could not work out the target release", file=sys.stderr)
        return 1

    rows = report(target_os, target_stack)
    if args.json:
        print(json.dumps({"owl_os": target_os, "retina_node": target_stack, "nodes": rows}, indent=2))
        return 0

    print(f"Target: {target_os} + {target_stack}")
    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("  ".join(f"{k}: {counts.get(k, 0)}" for k in reversed(RANK)))
    print()
    for r in rows:
        print(f"{r['verdict']:<15} {r['name']:<18} os={r['os'] or '?':<9} "
              f"stack={(r['stack'] or '?').replace('retina-node-', ''):<11} "
              f"health={r['retina_stack'] or '-':<9} seen={r['check_in'] or '?'}  {r['why']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
