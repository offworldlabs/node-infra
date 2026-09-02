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
