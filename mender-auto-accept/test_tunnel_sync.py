"""Tests for the tunnel's ingress routing.

These simulate cloudflared's own rule matching rather than asserting the shape
of the list, because the failure that matters is not a missing rule. It is a
rule that is present, reads correctly, and never fires because something above
it matched first. That produces 404s indistinguishable from a dead origin, and
it is how a hand-built tunnel in this account was misconfigured.
"""

import re

import pytest
import tunnel_sync

NODE = "ret4c844c20"
HOST = f"{NODE}.{tunnel_sync.REMOTE_ACCESS_DOMAIN}"


def serves(path, node_id=NODE, hostname=None):
    """Which service answers `path`, under cloudflared's first-match rule.

    cloudflared walks ingress in order and takes the first entry whose hostname
    and path both match. Anything with no path matches every path.
    """
    hostname = hostname or f"{node_id}.{tunnel_sync.REMOTE_ACCESS_DOMAIN}"
    for rule in tunnel_sync.build_ingress(node_id):
        if "hostname" in rule and rule["hostname"] != hostname:
            continue
        if "path" in rule and not re.search(rule["path"], path):
            continue
        return rule["service"]
    raise AssertionError("ingress has no catch-all")


# ── the three views support actually uses ────────────────────────

@pytest.mark.parametrize("path", [
    "/display/map/",       # Passive Radar
    "/display/maxhold/",   # Max-hold
    "/controller/",        # Controller
    "/lib/blah2.css",      # assets those pages load
    "/js/plot_map.js",
])
def test_blah2_pages_reach_the_web_port(path):
    assert serves(path) == tunnel_sync.BLAH2_WEB_SERVICE


@pytest.mark.parametrize("path", [
    "/api/timestamp", "/api/detection", "/api/map",
    "/api/adsb2dd", "/api/config",
    "/capture/toggle", "/stash/detection",
])
def test_blah2_data_reaches_the_api_port(path):
    """The pages are useless without these; they are what the JS calls once it
    is same-origin, which it is whenever the host is not localhost."""
    assert serves(path) == tunnel_sync.BLAH2_API_SERVICE


# ── the collision that would break the interface ─────────────────

@pytest.mark.parametrize("path", [
    "/api/mode",
    "/api/mode/release-spectrum",
    "/api/fleet/peers",
    "/api/spectrum/ready",
    "/api/sdrconnect/ready",
])
def test_retina_gui_keeps_its_own_api(path):
    """The reason blah2's endpoints are named one by one instead of matching
    `^/api`. These belong to the GUI, share the hostname, and a blanket prefix
    would divert them to blah2: broken over the tunnel, fine on the LAN, so
    nobody would find it until support tried to use it."""
    assert serves(path) == tunnel_sync.REMOTE_ACCESS_SERVICE


@pytest.mark.parametrize("path", ["/", "/config", "/set-up", "/static/app.css"])
def test_the_interface_still_answers_everything_else(path):
    assert serves(path) == tunnel_sync.REMOTE_ACCESS_SERVICE


# ── ordering, which is the whole thing ───────────────────────────

def test_every_path_rule_precedes_the_catch_all():
    """A rule without a path matches everything. Put one above the path rules
    and they are silently disabled while still looking correct."""
    rules = tunnel_sync.build_ingress(NODE)
    first_pathless = next(i for i, r in enumerate(rules)
                          if "path" not in r and "hostname" in r)
    last_pathed = max(i for i, r in enumerate(rules) if "path" in r)
    assert last_pathed < first_pathless


def test_the_final_rule_is_a_catch_all():
    """cloudflared requires the list to end with a rule that matches anything."""
    last = tunnel_sync.build_ingress(NODE)[-1]
    assert "hostname" not in last and last["service"] == "http_status:404"


def test_another_node_is_not_served_by_this_tunnel():
    for rule in tunnel_sync.build_ingress(NODE):
        assert rule.get("hostname") in (HOST, None)


# ── which names this script may touch, and which it owns ─────────────
#
# Two questions, one shape, opposite failure modes. They used to share a
# constant, so widening the node id format for one purpose silently widened it
# for the other — and the other decides what --prune deletes.

#: Every tunnel on retnode.com as of 2026-09-21 that this script did not create.
#: Several serve live customer nodes. If any of these ever matches, --prune
#: deletes somebody's working tunnel.
HAND_BUILT = [
    "fairforest",
    "jonathan-node-1",
    "jonathan-node-2",
    "joshOffice",
    "mississippi",
    "nightcrawler",
    "sacremento",
    "wilderness",
]

LEGACY_ID = "ret4c844c20"
CURRENT_ID = "retgec420d03ea4b064"


@pytest.mark.parametrize("node_id", [LEGACY_ID, CURRENT_ID])
def test_both_node_id_formats_may_be_acted_on(node_id):
    """The fleet is migrated one node at a time, so both are live at once."""
    tunnel_sync._guard(node_id)


@pytest.mark.parametrize(
    "name",
    HAND_BUILT + ["", None, "retg", "ret000000000", "retgec420d03ea4b06", "Unknown"],
)
def test_guard_refuses_anything_that_is_not_a_node_id(name):
    with pytest.raises(RuntimeError, match="refusing to act"):
        tunnel_sync._guard(name)


@pytest.mark.parametrize("name", HAND_BUILT)
def test_hand_built_tunnels_are_not_ours_to_sweep(name):
    """The property the orphan sweep rests on. Too narrow costs a lingering
    orphan; too wide deletes a production tunnel."""
    assert not tunnel_sync.OWNED_BY_US.match(name)


@pytest.mark.parametrize("node_id", [LEGACY_ID, CURRENT_ID])
def test_node_tunnels_in_either_format_are_ours(node_id):
    assert tunnel_sync.OWNED_BY_US.match(node_id)


def test_reconcile_never_offers_a_hand_built_tunnel_as_an_orphan():
    """End to end, because the constant being right is not the same as it being
    used in all three sweeps."""
    tunnels = [{"name": n, "id": f"id-{n}", "connections": []} for n in HAND_BUILT]
    tunnels.append({"name": CURRENT_ID, "id": "id-node", "connections": []})

    _, orphans, _ = tunnel_sync.reconcile(
        wanted=set(), state={}, tunnels=tunnels, dns_records=[], access_apps=[]
    )

    assert [name for _, name, _ in orphans] == [CURRENT_ID]


def test_the_dns_and_access_sweeps_agree_with_the_tunnel_sweep():
    """All three read OWNED_BY_US. A hand-built hostname in the zone must not
    be swept from any of them."""
    domain = tunnel_sync.REMOTE_ACCESS_DOMAIN
    dns = [
        {"name": f"{n}.{domain}", "type": "CNAME", "id": f"dns-{n}"}
        for n in HAND_BUILT + [CURRENT_ID]
    ]
    apps = [{"domain": f"{n}.{domain}", "id": f"app-{n}"} for n in HAND_BUILT + [CURRENT_ID]]

    _, orphans, _ = tunnel_sync.reconcile(
        wanted=set(), state={}, tunnels=[], dns_records=dns, access_apps=apps
    )

    swept = {name for _, name, _ in orphans}
    assert swept == {f"{CURRENT_ID}.{domain}"}
