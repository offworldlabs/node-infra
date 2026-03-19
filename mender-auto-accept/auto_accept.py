#!/usr/bin/env python3
"""Auto-accept pending Mender devices and deploy OS updates for new nodes.

Polls the Mender Management API for pending devices, accepts them, then checks
if their OS is outdated. If so, creates a one-off deployment targeting that device.

Only stable releases (vX.X.X) are considered — RC, dev, beta etc. are filtered out.

Environment variables:
    MENDER_PAT: Personal Access Token for Mender API (required)
    MENDER_SERVER: Mender server URL (default: https://hosted.mender.io)
    NODE_ID_PREFIX: Only accept devices with node_id starting with this (optional)
    DEVICE_TYPE: Device type to match artifacts (default: pi5-v3-arm64)
"""
import json
import os
import re
import sys
import time

import requests

MENDER_SERVER = os.environ.get("MENDER_SERVER", "https://hosted.mender.io")
MENDER_PAT = os.environ.get("MENDER_PAT")
NODE_ID_PREFIX = os.environ.get("NODE_ID_PREFIX", "")  # e.g., "ret"
DEVICE_TYPE = os.environ.get("DEVICE_TYPE", "pi5-v3-arm64")
PENDING_DEPLOY_FILE = os.environ.get(
    "PENDING_DEPLOY_FILE", "/tmp/mender-pending-deploys.json"
)
HEADERS = {"Authorization": f"Bearer {MENDER_PAT}"} if MENDER_PAT else {}


def get_pending_devices() -> list[dict]:
    """Fetch devices with pending authorization status."""
    resp = requests.get(
        f"{MENDER_SERVER}/api/management/v2/devauth/devices",
        params={"status": "pending"},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def accept_auth_set(device_id: str, auth_set_id: str) -> None:
    """Accept a device's authentication set."""
    resp = requests.put(
        f"{MENDER_SERVER}/api/management/v2/devauth/devices/{device_id}/auth/{auth_set_id}/status",
        json={"status": "accepted"},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()


def get_node_id(device: dict) -> str | None:
    """Extract node_id from device identity data."""
    # Check top-level identity_data
    if "node_id" in device.get("identity_data", {}):
        return device["identity_data"]["node_id"]
    # Fall back to auth_sets
    for auth_set in device.get("auth_sets", []):
        identity = auth_set.get("identity_data", {})
        if "node_id" in identity:
            return identity["node_id"]
    return None


# --- OS deployment for new nodes ---


def get_device_inventory(device_id: str) -> dict:
    """Get device inventory attributes from Mender."""
    resp = requests.get(
        f"{MENDER_SERVER}/api/management/v1/inventory/devices/{device_id}",
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def extract_artifact_name(inventory: dict) -> str | None:
    """Extract artifact_name from inventory attributes."""
    for attr in inventory.get("attributes", []):
        if attr.get("name") == "artifact_name":
            return attr.get("value")
    return None


def parse_stable_version(name: str) -> tuple[int, ...] | None:
    """Parse semver from artifact name. Returns None for non-stable (rc, dev, etc.)."""
    match = re.search(r"v(\d+)\.(\d+)\.(\d+)$", name)
    if not match:
        return None
    return tuple(int(x) for x in match.groups())


def get_latest_stable_artifact() -> str | None:
    """Get latest stable OS artifact name for DEVICE_TYPE.

    Only considers stable releases matching vX.X.X — filters out rc, dev, beta, etc.
    """
    resp = requests.get(
        f"{MENDER_SERVER}/api/management/v1/deployments/artifacts",
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()

    best_name = None
    best_version = (-1,)

    for artifact in resp.json():
        if DEVICE_TYPE not in artifact.get("device_types_compatible", []):
            continue
        name = artifact.get("name", "")
        version = parse_stable_version(name)
        if version and version > best_version:
            best_version = version
            best_name = name

    return best_name


def create_deployment(device_id: str, artifact_name: str) -> None:
    """Create a one-off deployment targeting a single device."""
    resp = requests.post(
        f"{MENDER_SERVER}/api/management/v1/deployments/deployments",
        headers=HEADERS,
        json={
            "name": f"onboard-{device_id[:8]}-{int(time.time())}",
            "artifact_name": artifact_name,
            "devices": [device_id],
        },
        timeout=30,
    )
    resp.raise_for_status()


def deploy_if_outdated(device_id: str, node_id: str | None) -> bool:
    """Check device OS version and create deployment if outdated.

    Returns True if handled (deployed or up-to-date), False if inventory not ready yet.
    """
    label = node_id or device_id[:8]

    try:
        inventory = get_device_inventory(device_id)
    except requests.RequestException as e:
        print(f"  Could not get inventory for {label}: {e}", file=sys.stderr)
        return False

    current = extract_artifact_name(inventory)
    if not current:
        print(f"  No artifact_name in inventory for {label} yet, will retry")
        return False

    latest = get_latest_stable_artifact()
    if not latest:
        print(f"  No stable OS artifact found for {DEVICE_TYPE}", file=sys.stderr)
        return True  # Nothing we can do, don't keep retrying

    current_ver = parse_stable_version(current)
    latest_ver = parse_stable_version(latest)

    if current_ver and latest_ver and current_ver >= latest_ver:
        print(f"  {label} already up to date ({current})")
        return True

    try:
        create_deployment(device_id, latest)
        print(f"  Created deployment for {label}: {current} → {latest}")
    except requests.RequestException as e:
        print(f"  Error creating deployment for {label}: {e}", file=sys.stderr)

    return True


# --- Pending deploy queue ---
# Devices that were accepted but inventory wasn't ready yet.
# Persisted to disk so we retry across script invocations (runs every 30s via timer).


def load_pending_deploys() -> dict[str, str | None]:
    """Load {device_id: node_id} map of devices awaiting deployment."""
    try:
        with open(PENDING_DEPLOY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_pending_deploys(pending: dict[str, str | None]) -> None:
    """Save pending deploys to disk."""
    with open(PENDING_DEPLOY_FILE, "w") as f:
        json.dump(pending, f)


def main() -> int:
    if not MENDER_PAT:
        print("Error: MENDER_PAT environment variable not set", file=sys.stderr)
        return 1

    # --- Phase 1: Accept pending devices ---

    try:
        pending = get_pending_devices()
    except requests.RequestException as e:
        print(f"Error fetching pending devices: {e}", file=sys.stderr)
        return 1

    accepted = 0
    skipped = 0
    pending_deploys = load_pending_deploys()

    for device in pending:
        device_id = device["id"]
        node_id = get_node_id(device)

        # Optional filter by node_id prefix
        if NODE_ID_PREFIX and (not node_id or not node_id.startswith(NODE_ID_PREFIX)):
            print(f"Skipping {device_id} (node_id: {node_id})")
            skipped += 1
            continue

        for auth_set in device.get("auth_sets", []):
            if auth_set["status"] == "pending":
                try:
                    accept_auth_set(device_id, auth_set["id"])
                    print(f"Accepted {node_id or device_id}")
                    accepted += 1
                    # Queue for deployment check
                    pending_deploys[device_id] = node_id
                except requests.RequestException as e:
                    print(f"Error accepting {node_id or device_id}: {e}", file=sys.stderr)

    if not pending:
        print("No pending devices")

    if accepted:
        print(f"Accepted: {accepted}, skipped: {skipped}")

    # --- Phase 2: Deploy OS updates for queued devices ---

    if pending_deploys:
        print(f"Checking {len(pending_deploys)} device(s) for OS deployment...")
        done = []
        for device_id, node_id in pending_deploys.items():
            if deploy_if_outdated(device_id, node_id):
                done.append(device_id)

        for device_id in done:
            del pending_deploys[device_id]

    save_pending_deploys(pending_deploys)
    return 0


if __name__ == "__main__":
    sys.exit(main())
