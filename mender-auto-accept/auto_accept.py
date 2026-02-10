#!/usr/bin/env python3
"""Auto-accept pending Mender devices.

Polls the Mender Management API for pending devices and accepts them automatically.
Optionally filters by device_type to only accept known device types.

Environment variables:
    MENDER_PAT: Personal Access Token for Mender API (required)
    MENDER_SERVER: Mender server URL (default: https://hosted.mender.io)
    DEVICE_TYPE_FILTER: Only accept devices with this type (optional)
"""
import os
import sys

import requests

MENDER_SERVER = os.environ.get("MENDER_SERVER", "https://hosted.mender.io")
MENDER_PAT = os.environ.get("MENDER_PAT")
DEVICE_TYPE_FILTER = os.environ.get("DEVICE_TYPE_FILTER", "")  # e.g., "pi5-v3-arm64"


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


def get_device_type(device: dict) -> str | None:
    """Extract device_type from device identity data."""
    for auth_set in device.get("auth_sets", []):
        identity = auth_set.get("identity_data", {})
        if "device_type" in identity:
            return identity["device_type"]
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
        device_type = get_device_type(device)

        # Optional filter by device type
        if DEVICE_TYPE_FILTER and device_type != DEVICE_TYPE_FILTER:
            print(f"Skipping device {device_id} (type: {device_type})")
            skipped += 1
            continue

        for auth_set in device.get("auth_sets", []):
            if auth_set["status"] == "pending":
                try:
                    accept_auth_set(device_id, auth_set["id"])
                    print(f"Accepted device {device_id} (type: {device_type})")
                    accepted += 1
                except requests.RequestException as e:
                    print(f"Error accepting device {device_id}: {e}", file=sys.stderr)

    print(f"Done: {accepted} accepted, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
