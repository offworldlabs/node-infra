#!/usr/bin/env python3
"""Auto-accept pending Mender devices.

Polls the Mender Management API for pending devices and accepts them automatically.
Optionally filters by node_id prefix to only accept known devices.

Environment variables:
    MENDER_PAT: Personal Access Token for Mender API (required)
    MENDER_SERVER: Mender server URL (default: https://hosted.mender.io)
    NODE_ID_PREFIX: Only accept devices with node_id starting with this (optional)
"""
import os
import sys

import requests

MENDER_SERVER = os.environ.get("MENDER_SERVER", "https://hosted.mender.io")
MENDER_PAT = os.environ.get("MENDER_PAT")
NODE_ID_PREFIX = os.environ.get("NODE_ID_PREFIX", "")  # e.g., "ret"


def get_pending_devices() -> list[dict]:
    """Fetch devices with pending authorization status."""
    resp = requests.get(
        f"{MENDER_SERVER}/api/management/v2/devauth/devices",
        params={"status": "pending"},
        headers={"Authorization": f"Bearer {MENDER_PAT}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def accept_auth_set(device_id: str, auth_set_id: str) -> None:
    """Accept a device's authentication set."""
    resp = requests.put(
        f"{MENDER_SERVER}/api/management/v2/devauth/devices/{device_id}/auth/{auth_set_id}/status",
        json={"status": "accepted"},
        headers={"Authorization": f"Bearer {MENDER_PAT}"},
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


def main() -> int:
    if not MENDER_PAT:
        print("Error: MENDER_PAT environment variable not set", file=sys.stderr)
        return 1

    try:
        pending = get_pending_devices()
    except requests.RequestException as e:
        print(f"Error fetching pending devices: {e}", file=sys.stderr)
        return 1

    if not pending:
        print("No pending devices")
        return 0

    accepted = 0
    skipped = 0

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
                except requests.RequestException as e:
                    print(f"Error accepting {node_id or device_id}: {e}", file=sys.stderr)

    print(f"Done: {accepted} accepted, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
