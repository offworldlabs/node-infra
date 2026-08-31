#!/usr/bin/env python3
"""Give nodes that asked for it a Cloudflare tunnel, and take it away when they stop.

The node never calls us. It states what its owner wants by publishing a
`remote_access` Mender inventory attribute (owl-os ships
mender-inventory-retina-remote-access, which reports the presence of a marker
file retina-gui writes). We read that, reconcile it against Cloudflare, and
upload the connector token back to the device over Mender file transfer.

That inversion is the whole point: there is no node-facing endpoint here, so
there is nothing to authenticate. Mender's PAT proves we are Offworld and
Mender's device auth proves the node is the node, both of which already work.

## Who may then reach the node

A Cloudflare Access application per hostname, whose policy references the
support group by id. Nothing personal appears in this repo or on any node:
membership lives in that one group, so adding or removing someone takes effect
across the whole fleet at once, with no application edited and no node touched.

Per node rather than one wildcard application, because a wildcard over the zone
would also cover the hand-built hostnames on it, several of which serve people
outside the support team.

## Why this is a separate script from auto_accept.py

auto_accept.py accepts devices and deploys OS updates for the entire fleet. If
tunnel work lived inside its loop, a Cloudflare outage or an unhandled
exception would stop device acceptance, which is a far worse failure than this
feature being unavailable. Keeping them apart means auto_accept.py changes by
zero lines, so nothing here can affect how existing nodes are handled.

It also wants a different cadence. Thirty seconds is right for catching a
newly-flashed board; it is needlessly frequent for tunnel state.

## Three states, not two

`remote_access` is read as absent, true or false, and absent is NOT false:

    absent   the node is on an OS that cannot report this. Do nothing, ever.
    true     ensure a tunnel exists.
    false    ensure one does not.

Collapsing absent into false would look equivalent and is not. An OS rollback
removes the inventory script, the attribute vanishes, and a working tunnel gets
torn down for an owner who never asked for that. auto_accept.py's
extract_wizard_pending does collapse them, which is correct for its question
and wrong for this one.

## Cost

One paginated inventory call per pass returns every device with its attributes,
so the read is O(1) in fleet size rather than one call per node. Cloudflare is
touched only when a node's intent differs from what we last recorded, so the
steady state is zero Cloudflare calls no matter how many nodes there are.

Environment variables:
    MENDER_PAT: Personal Access Token for Mender API (required)
    MENDER_SERVER: Mender server URL (default: https://hosted.mender.io)
    NODE_ID_PREFIX: Only consider devices whose node_id starts with this (optional)
    REMOTE_ACCESS_DOMAIN: Zone the tunnel hostnames live under (default: retnode.com)
    CLOUDFLARE_API_TOKEN: Scoped to Tunnel:Edit and DNS:Edit (required to --apply)
    CLOUDFLARE_ACCOUNT_ID: (required to --apply)
    CLOUDFLARE_ZONE_ID: zone id for REMOTE_ACCESS_DOMAIN (required to --apply)
    CLOUDFLARE_ACCESS_GROUP_ID: the support group each node's policy references
    CLOUDFLARE_ACCESS_TEAM_DOMAIN: <team>.cloudflareaccess.com, sent to nodes
    REMOTE_ACCESS_SERVICE: what the tunnel points at (default: http://localhost:80)
"""
import argparse
import json
import os
import re
import sys
import time

import requests

MENDER_SERVER = os.environ.get("MENDER_SERVER", "https://hosted.mender.io")
MENDER_PAT = os.environ.get("MENDER_PAT")
NODE_ID_PREFIX = os.environ.get("NODE_ID_PREFIX", "")
REMOTE_ACCESS_DOMAIN = os.environ.get("REMOTE_ACCESS_DOMAIN", "retnode.com")
#: What the tunnel points at on the node. Port 80 is retina-gui in production;
#: overridable so a proof of concept can aim at a second instance on another
#: port without touching the running GUI.
REMOTE_ACCESS_SERVICE = os.environ.get("REMOTE_ACCESS_SERVICE", "http://localhost:80")

CF_API = "https://api.cloudflare.com/client/v4"
CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN")
CF_ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
CF_ZONE = os.environ.get("CLOUDFLARE_ZONE_ID")
#: The Access group allowed to reach nodes. Referenced by id from every node's
#: application, so no address ever appears in this repo's config, and adding or
#: removing someone is one edit that applies to the whole fleet at once rather
#: than a sweep across as many applications as there are nodes.
CF_ACCESS_GROUP = os.environ.get("CLOUDFLARE_ACCESS_GROUP_ID")
CF_ACCESS_TEAM_DOMAIN = os.environ.get("CLOUDFLARE_ACCESS_TEAM_DOMAIN")

#: How long an engineer's Access session lasts. Matches the existing
#: applications; worth revisiting separately, since this one gates every node in
#: the fleet rather than a single host.
ACCESS_SESSION_DURATION = "24h"

STATE_FILE = os.environ.get(
    "TUNNEL_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tunnel-state.json"),
)

HEADERS = {"Authorization": f"Bearer {MENDER_PAT}"} if MENDER_PAT else {}

#: Both files node-infra stages for a node, and where owl-os's path unit
#: watches. access.json names the team and this node's Access application, which
#: retina-gui verifies assertions against; the node refuses every visitor until
#: it has them.
STAGING_ACCESS_PATH = "/home/node/.retina/access.json"

#: Where owl-os's cloudflared-token.path watches. Mender file transfer is
#: chrooted to /home/node and writes as that user, which is why the token lands
#: here rather than on /data; a path unit on the node does the privileged move.
STAGING_PATH = "/home/node/.retina/tunnel-token"

ABSENT = "absent"

#: Names this script is allowed to create, reconfigure or delete.
#:
#: retnode.com already carries hand-built tunnels for live customer nodes, some
#: serving several hostnames each, and ensure_tunnel() PUTs a tunnel's *entire*
#: ingress config. A miscomputed name would therefore not fail, it would quietly
#: replace a working tunnel's routing or delete a production DNS record. Nothing
#: existing is named ret<8 hex>, so pinning the shape makes that unreachable
#: rather than merely unlikely.
NODE_ID_RE = re.compile(r"^ret[0-9a-f]{8}$")


def _guard(node_id):
    """Raise unless this is a name we are allowed to touch."""
    if not node_id or not NODE_ID_RE.match(node_id):
        raise RuntimeError(
            f"refusing to act on {node_id!r}: not a ret<8 hex> node id. "
            f"retnode.com carries hand-built tunnels that this script must "
            f"never reconfigure or delete."
        )


# ── reading the fleet ────────────────────────────────────────────

def list_devices():
    """Every device, with its inventory attributes, in as few calls as possible.

    The inventory list endpoint returns attributes inline, so one paginated
    call covers the fleet. Doing this per device would be one request per node
    per pass, which is the thing that makes a naive version of this expensive.
    """
    devices = []
    page = 1
    while True:
        resp = requests.get(
            f"{MENDER_SERVER}/api/management/v1/inventory/devices",
            params={"per_page": 200, "page": page},
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        devices.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    return devices


def attribute(device, name):
    for attr in device.get("attributes", []):
        if attr.get("name") == name:
            return attr.get("value")
    return None


def remote_access_intent(device):
    """ABSENT, True or False. See the module docstring on why absent is not false."""
    value = attribute(device, "remote_access")
    if value is None:
        return ABSENT
    return str(value).lower() == "true"


# ── what we already did ──────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


# ── deciding ─────────────────────────────────────────────────────

def plan(devices, state):
    """Work out what needs doing. Pure: no API calls, no side effects.

    Returns a list of (action, node_id, device_id, why) with action in
    {"create", "teardown", "skip"}. Everything the dry run prints comes from
    here, so what you see is exactly what --apply would act on.
    """
    actions = []
    for device in devices:
        device_id = device.get("id")
        node_id = attribute(device, "node_id")

        if NODE_ID_PREFIX and not (node_id or "").startswith(NODE_ID_PREFIX):
            continue

        intent = remote_access_intent(device)
        known = state.get(node_id or device_id)

        if intent is ABSENT:
            # An OS that predates the feature, so it can neither ask for a
            # tunnel nor receive one. Never touched, and deliberately not
            # treated as a request to tear down anything it may already have.
            actions.append(("skip", node_id, device_id, "no remote_access attribute"))
        elif intent and not known:
            actions.append(("create", node_id, device_id, "owner turned it on"))
        elif intent and known:
            actions.append(("skip", node_id, device_id, "already provisioned"))
        elif not intent and known:
            actions.append(("teardown", node_id, device_id, "owner turned it off"))
        else:
            actions.append(("skip", node_id, device_id, "off, nothing provisioned"))
    return actions


# ── Cloudflare ───────────────────────────────────────────────────

def _cf(method, path, **kwargs):
    if not (CF_TOKEN and CF_ACCOUNT and CF_ZONE):
        raise RuntimeError(
            "Cloudflare is not configured. Set CLOUDFLARE_API_TOKEN, "
            "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_ZONE_ID before using --apply."
        )
    resp = requests.request(
        method, f"{CF_API}{path}",
        headers={"Authorization": f"Bearer {CF_TOKEN}"},
        timeout=30, **kwargs,
    )
    resp.raise_for_status()
    return resp.json()["result"]


def ensure_access_app(node_id):
    """Find or create this node's Access application, and return its audience.

    Per node rather than one wildcard application, because a wildcard over the
    zone would also cover the hand-built hostnames on it, several of which serve
    people who are not on the support team. Locking them out is not this
    feature's business.

    The policy references the support group by id rather than listing people, so
    membership lives in one object and changing it applies everywhere at once
    without touching a single application or node.
    """
    _guard(node_id)
    if not CF_ACCESS_GROUP:
        raise RuntimeError(
            "CLOUDFLARE_ACCESS_GROUP_ID is not set. Refusing to create an "
            "application with no policy, which would publish an unprotected "
            "hostname.")

    hostname = f"{node_id}.{REMOTE_ACCESS_DOMAIN}"
    apps = _cf("GET", f"/accounts/{CF_ACCOUNT}/access/apps", params={"per_page": 1000})
    app = next((a for a in apps if a.get("domain") == hostname), None)

    if app is None:
        app = _cf("POST", f"/accounts/{CF_ACCOUNT}/access/apps", json={
            "name": f"node {node_id}",
            "domain": hostname,
            "type": "self_hosted",
            "session_duration": ACCESS_SESSION_DURATION,
        })

    # Created separately rather than inline, and checked every time. An
    # application with no policy admits nobody, but one whose policy was removed
    # by hand would otherwise sit there looking provisioned.
    policies = _cf("GET", f"/accounts/{CF_ACCOUNT}/access/apps/{app['id']}/policies")
    has_group = any(
        inc.get("group", {}).get("id") == CF_ACCESS_GROUP
        for p in policies if p.get("decision") == "allow"
        for inc in (p.get("include") or [])
    )
    if not has_group:
        _cf("POST", f"/accounts/{CF_ACCOUNT}/access/apps/{app['id']}/policies", json={
            "name": "support team",
            "decision": "allow",
            "include": [{"group": {"id": CF_ACCESS_GROUP}}],
        })

    return app["aud"]


def destroy_access_app(node_id):
    """Remove this node's Access application, if it has one."""
    _guard(node_id)
    hostname = f"{node_id}.{REMOTE_ACCESS_DOMAIN}"
    for app in _cf("GET", f"/accounts/{CF_ACCOUNT}/access/apps", params={"per_page": 1000}):
        if app.get("domain") == hostname:
            _cf("DELETE", f"/accounts/{CF_ACCOUNT}/access/apps/{app['id']}")


def ensure_tunnel(node_id):
    """Find or create this node's tunnel, and return (tunnel_id, token, aud).

    Looks up by name before creating, so a lost state file recovers the
    existing tunnel instead of leaving an orphan behind and making a second.
    """
    _guard(node_id)
    existing = _cf("GET", f"/accounts/{CF_ACCOUNT}/cfd_tunnel",
                   params={"name": node_id, "is_deleted": "false"})
    if existing:
        tunnel_id = existing[0]["id"]
    else:
        created = _cf("POST", f"/accounts/{CF_ACCOUNT}/cfd_tunnel",
                      json={"name": node_id, "config_src": "cloudflare"})
        tunnel_id = created["id"]

    hostname = f"{node_id}.{REMOTE_ACCESS_DOMAIN}"
    _cf("PUT", f"/accounts/{CF_ACCOUNT}/cfd_tunnel/{tunnel_id}/configurations",
        json={"config": {"ingress": [
            {"hostname": hostname, "service": REMOTE_ACCESS_SERVICE},
            {"service": "http_status:404"},
        ]}})

    # Before the DNS record, always. The moment that record resolves the
    # hostname serves this node's interface, so creating the policy afterwards
    # leaves a window in which anyone who finds the name is inside.
    aud = ensure_access_app(node_id)

    _upsert_dns(node_id, tunnel_id)

    token = _cf("GET", f"/accounts/{CF_ACCOUNT}/cfd_tunnel/{tunnel_id}/token")
    return tunnel_id, token, aud


def _upsert_dns(node_id, tunnel_id):
    """One proxied CNAME to the tunnel. No node IP is ever published."""
    _guard(node_id)
    hostname = f"{node_id}.{REMOTE_ACCESS_DOMAIN}"
    target = f"{tunnel_id}.cfargotunnel.com"
    existing = _cf("GET", f"/zones/{CF_ZONE}/dns_records", params={"name": hostname})
    body = {"type": "CNAME", "name": hostname, "content": target, "proxied": True}
    if existing:
        _cf("PUT", f"/zones/{CF_ZONE}/dns_records/{existing[0]['id']}", json=body)
    else:
        _cf("POST", f"/zones/{CF_ZONE}/dns_records", json=body)


def list_tunnels():
    """Every live tunnel, with its connection count.

    One call for the whole account, which is what keeps reconciliation O(1) in
    fleet size. The listing carries `connections`, so this also answers "is that
    node's connector actually attached" without asking the node anything.
    """
    return _cf("GET", f"/accounts/{CF_ACCOUNT}/cfd_tunnel",
               params={"is_deleted": "false", "per_page": 1000})


def list_dns():
    """Every DNS record in the zone. One call, same reasoning as list_tunnels."""
    return _cf("GET", f"/zones/{CF_ZONE}/dns_records", params={"per_page": 5000})


def destroy_tunnel(node_id, tunnel_id):
    """Reverse order: DNS first, then the tunnel once its connector has gone.

    A tunnel with live connections refuses deletion, which is why the node's
    token is cleared before this runs. Access dies with the DNS record either
    way, so an unreachable node cannot keep itself reachable.
    """
    _guard(node_id)
    hostname = f"{node_id}.{REMOTE_ACCESS_DOMAIN}"
    for record in _cf("GET", f"/zones/{CF_ZONE}/dns_records", params={"name": hostname}):
        _cf("DELETE", f"/zones/{CF_ZONE}/dns_records/{record['id']}")
    if tunnel_id:
        _cf("DELETE", f"/accounts/{CF_ACCOUNT}/cfd_tunnel/{tunnel_id}")


# ── talking to the device ────────────────────────────────────────

def list_access_apps():
    """Every Access application in the account. One call, like the others."""
    return _cf("GET", f"/accounts/{CF_ACCOUNT}/access/apps", params={"per_page": 1000})


def stage_on_device(device_id, path, content):
    """Put a file where owl-os's path unit is watching.

    An empty body is the documented "turn it off" signal: a *missing* file
    cannot mean that, because it is also what a token the node has already
    consumed looks like.

    NOTE: the deviceconnect file-transfer endpoint below has not been exercised
    against the tenant yet. Everything on the node side of it has.
    """
    resp = requests.put(
        f"{MENDER_SERVER}/api/management/v1/deviceconnect/devices/{device_id}/upload",
        headers=HEADERS,
        files={"path": (None, path), "file": (os.path.basename(path), content)},
        timeout=60,
    )
    if resp.status_code == 400:
        # Almost always the staging directory not existing, which means the node
        # is on an OS build without the cloudflared role. Worth saying, because
        # the bare status reads as a malformed request.
        raise requests.RequestException(
            f"upload of {path} refused (400). The staging directory probably "
            f"does not exist, which means this node predates the owl-os role "
            f"that creates it.")
    resp.raise_for_status()


# ── reconciling ──────────────────────────────────────────────────

def reconcile(wanted, state, tunnels, dns_records, access_apps):
    """Compare what should exist against what Cloudflare actually holds.

    `wanted` is the set of node_ids currently asking for a tunnel. Pure, like
    plan(): every judgement here is made from data already fetched, so a dry run
    shows exactly what a repair pass would do.

    Works from two bulk listings rather than per-node lookups. Checking each
    provisioned node individually would be two Cloudflare calls per node per
    pass, which is the thing that makes reconciliation too expensive to run
    often enough to be useful.

    Returns (repairs, orphans, notes):
      repairs  things we believe exist but do not, or point somewhere wrong.
               Fixed by re-running ensure_tunnel, which is idempotent.
      orphans  ret<8 hex> tunnels and records nothing wants any more. Reported
               rather than deleted unless --prune, because a Mender outage that
               returned a short device list would otherwise look exactly like a
               fleet that had all opted out.
      notes    true but not actionable here, such as a node being offline.
    """
    by_name = {t["name"]: t for t in tunnels}
    cnames = {r["name"]: r for r in dns_records if r.get("type") == "CNAME"}
    protected = {a.get("domain") for a in access_apps}

    repairs, orphans, notes = [], [], []

    for node_id, _record in sorted(state.items()):
        hostname = f"{node_id}.{REMOTE_ACCESS_DOMAIN}"
        tunnel = by_name.get(node_id)

        if not tunnel:
            repairs.append((node_id, "we recorded a tunnel that no longer exists"))
            continue

        # Checked before anything else about this node. A hostname that
        # resolves with no Access application in front of it is serving the
        # interface to whoever finds the name, which is a different order of
        # problem from a stale DNS record.
        if hostname not in protected:
            repairs.append((node_id, "NO ACCESS POLICY: this hostname is unprotected"))

        dns = cnames.get(hostname)
        if not dns:
            repairs.append((node_id, "tunnel exists but its DNS record is missing"))
        elif not dns.get("content", "").startswith(tunnel["id"]):
            repairs.append((node_id, "DNS points at a different tunnel"))
        elif not dns.get("proxied"):
            repairs.append((node_id, "DNS record is not proxied"))

        if not tunnel.get("connections"):
            # Not a repair. The node may simply be off, and re-provisioning
            # would not bring it back; only the node connecting does.
            notes.append((node_id, "no connector attached (node offline, "
                                   "or it never received its token)"))

    for tunnel in tunnels:
        name = tunnel["name"]
        if NODE_ID_RE.match(name) and name not in wanted and name not in state:
            orphans.append(("tunnel", name, tunnel["id"]))

    for app in access_apps:
        domain = app.get("domain") or ""
        node_id = domain.split(".")[0]
        if (domain.endswith("." + REMOTE_ACCESS_DOMAIN)
                and NODE_ID_RE.match(node_id)
                and node_id not in wanted and node_id not in state):
            orphans.append(("access-app", domain, app["id"]))

    for record in dns_records:
        name = record.get("name", "")
        node_id = name.split(".")[0]
        if (record.get("type") == "CNAME"
                and name.endswith("." + REMOTE_ACCESS_DOMAIN)
                and NODE_ID_RE.match(node_id)
                and node_id not in wanted and node_id not in state):
            orphans.append(("dns", name, record["id"]))

    return repairs, orphans, notes


# ── main ─────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="actually create, tear down and repair tunnels "
                             "(default is to report what would happen)")
    parser.add_argument("--prune", action="store_true",
                        help="also delete orphaned ret* tunnels and DNS records "
                             "that nothing wants. Implies --apply")
    parser.add_argument("--verbose", action="store_true",
                        help="list every device, not just the ones needing work")
    args = parser.parse_args()
    if args.prune:
        args.apply = True

    if not MENDER_PAT:
        print("Error: MENDER_PAT is not set", file=sys.stderr)
        return 1

    devices = list_devices()

    # A Mender outage that answered with an empty list would look identical to a
    # fleet that had all opted out, and we would tear down every tunnel we own.
    # Nothing legitimate produces zero devices, so treat it as a failure rather
    # than as instructions.
    if not devices:
        print("Error: Mender returned no devices. Refusing to act, since that is "
              "indistinguishable from every node having opted out.", file=sys.stderr)
        return 1

    state = load_state()
    actions = plan(devices, state)
    wanted = {node_id for action, node_id, _, _ in actions if action == "create"}
    wanted |= {node_id for node_id in state}

    counts = {}
    for action, _, _, _ in actions:
        counts[action] = counts.get(action, 0) + 1

    print(f"{len(devices)} device(s) in inventory, "
          f"{len(actions)} matching prefix {NODE_ID_PREFIX!r}")
    for action in ("create", "teardown", "skip"):
        if counts.get(action):
            print(f"  {action:9} {counts[action]}")

    todo = [a for a in actions if a[0] != "skip"]
    for action, node_id, device_id, why in (actions if args.verbose else todo):
        print(f"  [{action}] {node_id or device_id} ({why})")

    # Reconciliation needs to see what Cloudflare actually holds.
    repairs, orphans, notes = [], [], []
    if CF_TOKEN and CF_ACCOUNT and CF_ZONE:
        try:
            repairs, orphans, notes = reconcile(
                wanted, state, list_tunnels(), list_dns(), list_access_apps())
        except (requests.RequestException, RuntimeError) as e:
            print(f"  could not reconcile against Cloudflare: {e}", file=sys.stderr)
    elif args.apply:
        print("  Cloudflare is not configured; skipping reconciliation",
              file=sys.stderr)

    if repairs or orphans or notes:
        print("\nreconciliation")
        for node_id, why in repairs:
            print(f"  [repair]  {node_id} ({why})")
        for kind, name, _ in orphans:
            print(f"  [orphan]  {kind} {name} "
                  f"({'delete with --prune' if not args.prune else 'will delete'})")
        for node_id, why in notes:
            print(f"  [note]    {node_id} ({why})")

    if not args.apply:
        pending = len(todo) + len(repairs)
        print(f"\nDry run. {pending} action(s) not performed."
              if pending else "\nDry run. Nothing to do.")
        return 0

    for action, node_id, device_id, _ in todo:
        key = node_id or device_id
        try:
            if action == "create":
                tunnel_id, token, aud = ensure_tunnel(node_id)
                # access.json first: the node refuses every visitor until it can
                # verify assertions, so landing the token first would bring the
                # hostname up during the gap. It fails closed rather than open,
                # but it is an outage nobody needs to have.
                stage_on_device(device_id, STAGING_ACCESS_PATH, json.dumps({
                    "team_domain": CF_ACCESS_TEAM_DOMAIN,
                    "aud": aud,
                }).encode())
                stage_on_device(device_id, STAGING_PATH, token)
                state[key] = {"tunnel_id": tunnel_id, "device_id": device_id,
                              "hostname": f"{node_id}.{REMOTE_ACCESS_DOMAIN}",
                              "aud": aud,
                              "provisioned_at": time.time()}
                print(f"  provisioned {key}")
            elif action == "teardown":
                record = state.get(key, {})
                # Clear the node's token first so the connector stops; a tunnel
                # with live connections refuses deletion.
                try:
                    stage_on_device(device_id, STAGING_PATH, b"")
                    stage_on_device(device_id, STAGING_ACCESS_PATH, b"")
                except requests.RequestException as e:
                    print(f"  {key}: could not clear the node's files ({e}); "
                          f"removing the tunnel anyway, which revokes access",
                          file=sys.stderr)
                destroy_tunnel(node_id, record.get("tunnel_id"))
                destroy_access_app(node_id)
                state.pop(key, None)
                print(f"  tore down {key}")
        except (requests.RequestException, RuntimeError) as e:
            # One node's failure must not stop the rest of the pass.
            print(f"  {key}: {action} failed: {e}", file=sys.stderr)

    # Repairs are just ensure_tunnel again, which is idempotent by construction:
    # it finds the tunnel by name, rewrites the ingress and upserts the DNS
    # record. The token is not re-sent, because a node that already has a
    # working one does not need it and a node that does not is offline anyway.
    for node_id, why in repairs:
        try:
            tunnel_id, _ = ensure_tunnel(node_id)
            state.setdefault(node_id, {})["tunnel_id"] = tunnel_id
            state[node_id]["hostname"] = f"{node_id}.{REMOTE_ACCESS_DOMAIN}"
            print(f"  repaired {node_id} ({why})")
        except (requests.RequestException, RuntimeError) as e:
            print(f"  {node_id}: repair failed: {e}", file=sys.stderr)

    if args.prune:
        for kind, name, ident in orphans:
            try:
                if kind == "tunnel":
                    _guard(name)
                    _cf("DELETE", f"/accounts/{CF_ACCOUNT}/cfd_tunnel/{ident}")
                elif kind == "access-app":
                    _guard(name.split(".")[0])
                    _cf("DELETE", f"/accounts/{CF_ACCOUNT}/access/apps/{ident}")
                else:
                    _guard(name.split(".")[0])
                    _cf("DELETE", f"/zones/{CF_ZONE}/dns_records/{ident}")
                print(f"  pruned {kind} {name}")
            except (requests.RequestException, RuntimeError) as e:
                print(f"  {name}: prune failed: {e}", file=sys.stderr)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
