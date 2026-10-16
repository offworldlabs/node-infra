"""Tests for the Mender snapshot pushed to retina-server.

The receiver rejects unknown keys and marks every device missing from a push
as absent. So what matters most is the exact shape of each entry, the same
auth reduction retina-server applies to a live lookup, and never pushing a
partial fleet. Every HTTP call here is a fake. Tests that could reach the
network call isolate() first, after which an unexpected request fails the test.
"""

import os
import re
import runpy
from datetime import datetime, timedelta, timezone

import mender_snapshot
import pytest

CONTRACT_KEYS = {
    "mender_device_id", "node_id", "auth_status", "auth_set_id", "auth_set_pubkey",
    "device_type", "artifact_name", "inventory_updated_at",
}
PAT = "mender-pat-stays-on-this-host"
MENDER = "https://mender.test"
DEVAUTH = "https://mender.test/api/management/v2/devauth/devices"
INVENTORY_URL = "https://mender.test/api/management/v1/inventory/devices"
GENERATED_AT = "2026-10-16T14:02:11Z"


def isolate(monkeypatch, targets="", pat=PAT):
    """Known settings, and no request reaches a real server. A call not faked
    after this raises, and is also recorded in the returned list, because
    run() swallows every exception and would hide the raise on its own."""
    unexpected = []

    def no_network(url, **kwargs):
        unexpected.append(url)
        raise AssertionError(f"unexpected request to {url}")

    monkeypatch.setattr(mender_snapshot, "MENDER_SERVER", MENDER)
    monkeypatch.setattr(mender_snapshot, "MENDER_PAT", pat)
    monkeypatch.setattr(mender_snapshot, "RETINA_SYNC_TARGETS", targets)
    monkeypatch.setattr(mender_snapshot.requests, "get", no_network)
    monkeypatch.setattr(mender_snapshot.requests, "post", no_network)
    monkeypatch.setattr(mender_snapshot.socket, "gethostname", lambda: "central.ops.example")
    return unexpected


def build_then_refuse(monkeypatch):
    """Fake get and post that build each request the way requests does, so a
    header value requests refuses raises its own error, which quotes the value.
    A request that builds is recorded and refused, so none reaches a server."""
    attempted = []

    def fake(method):
        def send(url, **kwargs):
            attempted.append(url)
            mender_snapshot.requests.Request(method, url, headers=kwargs.get("headers"),
                                             params=kwargs.get("params"), json=kwargs.get("json")).prepare()
            raise AssertionError(f"unexpected request to {url}")
        return send

    monkeypatch.setattr(mender_snapshot.requests, "get", fake("GET"))
    monkeypatch.setattr(mender_snapshot.requests, "post", fake("POST"))
    return attempted


class _Resp:
    def __init__(self, status_code, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise mender_snapshot.requests.HTTPError(f"{self.status_code}")


def key(name):
    return f"-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEA{name}\n-----END PUBLIC KEY-----\n"


def auth_set(set_id, status):
    """One auth set as devauth v2 returns it."""
    return {"id": set_id, "pubkey": key(set_id), "status": status, "ts": "2026-10-01T09:00:00.000Z"}


def inventory(device_id, artifact, updated_ts):
    """One device as inventory v1 returns it, identity attributes included."""
    return {"id": device_id, "updated_ts": updated_ts, "attributes": [
        {"name": "status", "value": "accepted", "scope": "identity"},
        {"name": "device_type", "value": "pi5-v3-arm64", "scope": "inventory"},
        {"name": "artifact_name", "value": artifact, "scope": "inventory"},
        {"name": "kernel", "value": "Linux version 6.6.31", "scope": "inventory"},
    ]}


# ── the reduction ────────────────────────────────────────────────

DEVICES = [
    # A new board, enrolled and waiting.
    {"id": "dev-pending", "identity_data": {"node_id": "ret00000001"}, "status": "pending",
     "auth_sets": [auth_set("set-p", "pending")]},
    # An ordinary node.
    {"id": "dev-accepted", "identity_data": {"node_id": "ret00000002"}, "status": "accepted",
     "auth_sets": [auth_set("set-a", "accepted")]},
    # Reflashed, and both keys ended up accepted: two live identities.
    {"id": "dev-two-keys", "identity_data": {"node_id": "ret00000003"}, "status": "accepted",
     "auth_sets": [auth_set("set-old", "accepted"), auth_set("set-new", "accepted")]},
    # Reflashed, new key still pending: the accepted key is the one in use.
    {"id": "dev-reflashing", "identity_data": {"node_id": "ret00000004"}, "status": "accepted",
     "auth_sets": [auth_set("set-r-old", "accepted"), auth_set("set-r-new", "pending")]},
    # Says accepted, but no auth set agrees.
    {"id": "dev-accepted-no-set", "identity_data": {"node_id": "ret00000005"}, "status": "accepted",
     "auth_sets": [auth_set("set-x", "rejected")]},
    # No identity data at all.
    {"id": "dev-no-identity", "status": "rejected", "auth_sets": [auth_set("set-n", "rejected")]},
    # An empty node_id, which must not collide with other empties in a unique column.
    {"id": "dev-empty-node-id", "identity_data": {"node_id": "", "mac": "dc:a6:32:00:00:07"},
     "status": "preauthorized", "auth_sets": [auth_set("set-e", "preauthorized")]},
    # A non-ret board, accepted, that inventory has never heard of.
    {"id": "dev-no-inventory", "identity_data": {"node_id": "lab-bench-7"}, "status": "accepted",
     "auth_sets": [auth_set("set-i", "accepted")]},
]

INVENTORY = [
    inventory("dev-pending", "owl-os-pi5-v0.16.1", "2026-10-16T14:01:40.123Z"),
    inventory("dev-accepted", "owl-os-pi5-v0.16.2", "2026-10-16T14:01:40.123456789Z"),
    inventory("dev-two-keys", "owl-os-pi5-v0.16.2", "2026-10-16T14:01:41Z"),
    inventory("dev-reflashing", "owl-os-pi5-v0.16.0", "2026-10-16T16:01:42+02:00"),
    inventory("dev-accepted-no-set", "owl-os-pi5-v0.16.2", "2026-10-16T14:01:43.5Z"),
    inventory("dev-no-identity", "owl-os-pi5-v0.15.9", "2026-10-15T23:59:59.999999999Z"),
    {"id": "dev-empty-node-id", "attributes": [], "updated_ts": "not a time"},
    # Gone from devauth: inventory alone does not put a device in the snapshot.
    inventory("dev-decommissioned", "owl-os-pi5-v0.14.0", "2026-09-01T00:00:00Z"),
]


def entry(device_id, node_id, auth_status, set_id=None, device_type=None, artifact=None, updated=None):
    return {
        "mender_device_id": device_id,
        "node_id": node_id,
        "auth_status": auth_status,
        "auth_set_id": set_id,
        "auth_set_pubkey": key(set_id) if set_id else None,
        "device_type": device_type,
        "artifact_name": artifact,
        "inventory_updated_at": updated,
    }


EXPECTED = [
    entry("dev-pending", "ret00000001", "pending", None, "pi5-v3-arm64", "owl-os-pi5-v0.16.1", "2026-10-16T14:01:40Z"),
    entry("dev-accepted", "ret00000002", "accepted", "set-a", "pi5-v3-arm64", "owl-os-pi5-v0.16.2",
          "2026-10-16T14:01:40Z"),
    entry("dev-two-keys", "ret00000003", "ambiguous", None, "pi5-v3-arm64", "owl-os-pi5-v0.16.2",
          "2026-10-16T14:01:41Z"),
    entry("dev-reflashing", "ret00000004", "accepted", "set-r-old", "pi5-v3-arm64", "owl-os-pi5-v0.16.0",
          "2026-10-16T14:01:42Z"),
    entry("dev-accepted-no-set", "ret00000005", "ambiguous", None, "pi5-v3-arm64", "owl-os-pi5-v0.16.2",
          "2026-10-16T14:01:43Z"),
    entry("dev-no-identity", None, "rejected", None, "pi5-v3-arm64", "owl-os-pi5-v0.15.9", "2026-10-15T23:59:59Z"),
    entry("dev-empty-node-id", None, "preauthorized"),
    entry("dev-no-inventory", "lab-bench-7", "accepted", "set-i"),
]


@pytest.mark.parametrize("expected", EXPECTED, ids=[e["mender_device_id"] for e in EXPECTED])
def test_each_device_reduces_to_its_contract_entry(expected):
    devices = mender_snapshot.build_snapshot(GENERATED_AT, DEVICES, INVENTORY)["devices"]
    got = {d["mender_device_id"]: d for d in devices}
    assert got[expected["mender_device_id"]] == expected


def test_the_body_is_exactly_the_contract(monkeypatch):
    """extra="forbid" on the receiver: one stray key and every push is a 422."""
    isolate(monkeypatch)
    body = mender_snapshot.build_snapshot(GENERATED_AT, DEVICES, INVENTORY)
    assert set(body) == {"generated_at", "source", "devices"}
    assert body["generated_at"] == GENERATED_AT
    assert body["source"] == "mender-auto-accept@central"
    assert all(set(d) == CONTRACT_KEYS for d in body["devices"])
    assert body["devices"] == EXPECTED


ABSENT = object()  # the device has no "status" key at all


@pytest.mark.parametrize("status,auth_sets,expected", [
    # Mender left the device status out: the auth sets decide.
    (ABSENT, [auth_set("s1", "accepted")], ("accepted", "s1", key("s1"))),
    (ABSENT, [auth_set("s1", "pending")], ("pending", None, None)),
    # A status that is there but unreadable is never taken as "accepted".
    (None, [auth_set("s1", "accepted")], ("ambiguous", None, None)),
    ("", [auth_set("s1", "accepted")], ("ambiguous", None, None)),
    (7, [auth_set("s1", "accepted")], ("ambiguous", None, None)),
    (7, [], ("pending", None, None)),
    # An accepted set with a device status that disagrees is passed through, as _record does.
    ("pending", [auth_set("s1", "accepted")], ("pending", "s1", key("s1"))),
    # Unreadable auth_sets count as none, so an "accepted" device is ambiguous.
    ("accepted", {"s1": "accepted"}, ("ambiguous", None, None)),
    ("accepted", [auth_set("s1", "accepted"), "s2"], ("ambiguous", None, None)),
    ("accepted", "accepted", ("ambiguous", None, None)),
    ("accepted", None, ("ambiguous", None, None)),
    ("pending", [auth_set("s1", "accepted"), None], ("pending", None, None)),
    # Statuses the contract names go through as they are.
    ("rejected", [], ("rejected", None, None)),
    ("noauth", [], ("noauth", None, None)),
])
def test_auth_state_edges(status, auth_sets, expected):
    device = {"id": "d1", "auth_sets": auth_sets}
    if status is not ABSENT:
        device["status"] = status
    assert mender_snapshot.auth_state(device) == expected


UNUSABLE_SETS = {
    "no-id": {"pubkey": key("s1"), "status": "accepted"},
    "empty-id": {"id": "", "pubkey": key("s1"), "status": "accepted"},
    "int-id": {"id": 42, "pubkey": key("s1"), "status": "accepted"},
    "no-pubkey": {"id": "s1", "status": "accepted"},
    "empty-pubkey": {"id": "s1", "pubkey": "", "status": "accepted"},
    "list-pubkey": {"id": "s1", "pubkey": ["x"], "status": "accepted"},
}


@pytest.mark.parametrize("status", [ABSENT, "accepted"], ids=["status-absent", "status-accepted"])
@pytest.mark.parametrize("accepted_set", UNUSABLE_SETS.values(), ids=UNUSABLE_SETS.keys())
def test_an_accepted_set_without_its_id_or_pubkey_is_ambiguous(status, accepted_set):
    """The contract wants both for an accepted device, and the receiver would
    store it with an issue if either is null."""
    device = {"id": "d1", "auth_sets": [accepted_set, auth_set("s2", "pending")]}
    if status is not ABSENT:
        device["status"] = status
    assert mender_snapshot.auth_state(device) == ("ambiguous", None, None)


def test_a_disagreeing_status_still_sends_a_non_string_set_id_or_pubkey_as_null():
    device = {"id": "d1", "status": "pending", "auth_sets": [{"id": 42, "pubkey": ["x"], "status": "accepted"}]}
    assert mender_snapshot.auth_state(device) == ("pending", None, None)


def test_every_accepted_entry_carries_its_set_id_and_pubkey():
    """Null is allowed only when not accepted, whatever Mender sends."""
    junk = [{"id": f"junk-{name}", "auth_sets": [accepted_set]} for name, accepted_set in UNUSABLE_SETS.items()]
    junk += [{"id": f"junk-status-{n}", "status": status, "auth_sets": [auth_set("s1", "accepted")]}
             for n, status in enumerate([None, "", 7, ["accepted"]])]
    devices = mender_snapshot.build_snapshot(GENERATED_AT, DEVICES + junk, INVENTORY)["devices"]
    accepted = [d for d in devices if d["auth_status"] == "accepted"]
    assert [d["mender_device_id"] for d in accepted] == ["dev-accepted", "dev-reflashing", "dev-no-inventory"]
    assert all(isinstance(d[k], str) and d[k] for d in accepted for k in ("auth_set_id", "auth_set_pubkey"))


@pytest.mark.parametrize("identity", [None, "node_id=ret1", ["ret1"], {}, {"node_id": ""}, {"node_id": 5}])
def test_an_unusable_node_id_goes_out_as_null(identity):
    device = {"id": "d1", "status": "pending", "auth_sets": []}
    if identity is not None:
        device["identity_data"] = identity
    assert mender_snapshot.device_entry(device, None)["node_id"] is None


def test_devices_without_an_id_are_left_out_with_a_warning(capsys):
    devices = [{"status": "pending"}, "garbage", {"id": ""}, {"id": 5}, None, DEVICES[0]]
    body = mender_snapshot.build_snapshot(GENERATED_AT, devices, [])
    assert [d["mender_device_id"] for d in body["devices"]] == ["dev-pending"]
    assert capsys.readouterr().err.count("Warning: Mender device with no id") == 5


def test_a_node_id_on_two_devices_is_warned_about_but_both_are_sent(capsys):
    twin = {**DEVICES[1], "id": "dev-accepted-twin"}
    body = mender_snapshot.build_snapshot(GENERATED_AT, [DEVICES[1], twin, DEVICES[0]], [])
    assert [d["mender_device_id"] for d in body["devices"]] == ["dev-accepted", "dev-accepted-twin", "dev-pending"]
    err = capsys.readouterr().err
    assert "node_id ret00000002 is on 2 Mender devices (dev-accepted, dev-accepted-twin)" in err
    assert "ret00000001" not in err


@pytest.mark.parametrize("attributes,expected", [
    ([{"name": "device_type", "value": "from-identity", "scope": "identity"},
      {"name": "device_type", "value": "pi5-v3-arm64", "scope": "inventory"}], "pi5-v3-arm64"),
    ([{"name": "device_type", "value": "first"}, {"name": "device_type", "value": "second"}], "first"),
    ([{"name": "device_type", "value": ["pi5"], "scope": "inventory"}], None),
    ([{"name": "device_type", "value": 5, "scope": "inventory"}], None),
    ([{"name": "artifact_name", "value": "owl-os", "scope": "inventory"}], None),
    (["device_type", None], None),
    ({"device_type": "pi5-v3-arm64"}, None),
    (None, None),
])
def test_inventory_attributes_prefer_the_inventory_scope(attributes, expected):
    inv = {"id": "d1", "attributes": attributes}
    assert mender_snapshot.device_entry({"id": "d1"}, inv)["device_type"] == expected


@pytest.mark.parametrize("value,expected", [
    ("2026-10-16T14:01:40.123456789Z", "2026-10-16T14:01:40Z"),
    ("2026-10-16T14:01:40.999999999Z", "2026-10-16T14:01:40Z"),
    ("2026-10-16T14:01:40.1Z", "2026-10-16T14:01:40Z"),
    ("2026-10-16T14:01:40Z", "2026-10-16T14:01:40Z"),
    ("2026-10-16T16:01:40.5+02:00", "2026-10-16T14:01:40Z"),
    ("2026-10-16T16:01:40.123+02:00", "2026-10-16T14:01:40Z"),
    ("2026-10-16T19:31:40.123456789+05:30", "2026-10-16T14:01:40Z"),
    ("2026-10-15T23:30:00-01:00", "2026-10-16T00:30:00Z"),
    ("2026-10-16t14:01:40z", "2026-10-16T14:01:40Z"),
    ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z"),
    (None, None),
    (1760623300, None),
    ("", None),
    ("not a time", None),
    ("2026-10-16T14:01:40", None),
    ("2026-10-16 14:01:40Z", None),
    ("2026-10-16T14:01:40.1234567890Z", None),
    ("2026-13-01T00:00:00Z", None),
    ("2026-10-16T14:01:40+24:00", None),
    ("0001-01-01T00:00:00+01:00", None),
    ("2026-10-16T14:01:40Z\n", None),
    ("２０２６-10-16T14:01:40Z", None),
])
def test_timestamps_normalise_to_utc_seconds(value, expected):
    assert mender_snapshot.normalise_ts(value) == expected


# ── fetching ─────────────────────────────────────────────────────

def fake_mender(monkeypatch, devauth_pages, inventory_pages, fail=None, on_call=None):
    """Serve each list one page at a time; any page past the end is empty.

    fail is (url, page, response or exception) for the one request that goes wrong.
    """
    calls = []

    def fake_get(url, **kwargs):
        page = kwargs["params"]["page"]
        calls.append((url, kwargs))
        if on_call:
            on_call()
        if fail and (url, page) == fail[:2]:
            if isinstance(fail[2], Exception):
                raise fail[2]
            return fail[2]
        pages = {DEVAUTH: devauth_pages, INVENTORY_URL: inventory_pages}[url]
        return _Resp(200, pages[page - 1] if page <= len(pages) else [])

    monkeypatch.setattr(mender_snapshot.requests, "get", fake_get)
    return calls


def test_pages_are_read_until_an_empty_one_and_deduplicated(monkeypatch):
    isolate(monkeypatch)
    a, b, c = {"id": "a"}, {"id": "b"}, {"id": "c"}
    calls = fake_mender(monkeypatch, [[a, b], [b, c]], [])
    assert mender_snapshot.fetch_all(mender_snapshot.DEVAUTH_PATH) == [a, b, c]
    assert calls == [
        (DEVAUTH, {"params": {"page": n, "per_page": 500}, "headers": {"Authorization": f"Bearer {PAT}"},
                   "timeout": 30})
        for n in (1, 2, 3)
    ]


def test_a_server_ignoring_page_does_not_loop(monkeypatch):
    """The same first page every time adds no new id, so the second ends it."""
    isolate(monkeypatch)
    calls = []
    monkeypatch.setattr(mender_snapshot.requests, "get",
                        lambda url, **kwargs: calls.append(kwargs) or _Resp(200, [{"id": "a"}, {"id": "b"}]))
    assert mender_snapshot.fetch_all(mender_snapshot.DEVAUTH_PATH) == [{"id": "a"}, {"id": "b"}]
    assert len(calls) == 2


def full_page(size=None):
    return [{"id": f"dev-{n}"} for n in range(mender_snapshot.PER_PAGE if size is None else size)]


@pytest.mark.parametrize("size", [mender_snapshot.PER_PAGE, mender_snapshot.PER_PAGE + 1], ids=["full", "over-full"])
def test_a_server_ignoring_page_after_a_full_page_is_refused(monkeypatch, size):
    """The same full page again means the rest of the list may never have been read."""
    isolate(monkeypatch)
    calls = []
    page = full_page(size)
    monkeypatch.setattr(mender_snapshot.requests, "get", lambda url, **kwargs: calls.append(kwargs) or _Resp(200, page))
    with pytest.raises(RuntimeError, match=re.escape(f"{mender_snapshot.DEVAUTH_PATH} page 2")):
        mender_snapshot.fetch_all(mender_snapshot.DEVAUTH_PATH)
    assert len(calls) == 2


def test_a_first_page_with_entries_but_no_ids_is_refused(monkeypatch):
    """Returning nothing for it would push an empty fleet and mark every device absent."""
    isolate(monkeypatch)
    fake_mender(monkeypatch, [[{"status": "pending"}, "junk"]], [])
    with pytest.raises(RuntimeError, match=re.escape(f"{mender_snapshot.DEVAUTH_PATH} page 1")):
        mender_snapshot.fetch_all(mender_snapshot.DEVAUTH_PATH)


def test_a_list_that_never_ends_is_refused(monkeypatch):
    isolate(monkeypatch)
    calls = []

    def endless(url, **kwargs):
        calls.append(kwargs["params"]["page"])
        return _Resp(200, [{"id": f"dev-{kwargs['params']['page']}"}])

    monkeypatch.setattr(mender_snapshot.requests, "get", endless)
    with pytest.raises(RuntimeError, match="50 pages"):
        mender_snapshot.fetch_all(mender_snapshot.DEVAUTH_PATH)
    assert calls == list(range(1, 51))


def test_entries_without_an_id_survive_the_fetch_for_the_warning(monkeypatch):
    isolate(monkeypatch)
    fake_mender(monkeypatch, [[{"id": "a"}, {"status": "pending"}, "junk"]], [])
    assert mender_snapshot.fetch_all(mender_snapshot.DEVAUTH_PATH) == [{"id": "a"}, {"status": "pending"}, "junk"]


@pytest.mark.parametrize("payload", [{"error": "nope"}, "devices", None])
def test_a_page_that_is_not_a_list_is_a_failure(monkeypatch, payload):
    isolate(monkeypatch)
    monkeypatch.setattr(mender_snapshot.requests, "get", lambda url, **kwargs: _Resp(200, payload))
    with pytest.raises(ValueError, match="not a list"):
        mender_snapshot.fetch_all(mender_snapshot.DEVAUTH_PATH)


def test_generated_at_is_stamped_before_the_devauth_fetch_starts(monkeypatch):
    """The server measures staleness from generated_at, so it must not claim
    data fresher than the first request. Each request here takes 7 s."""
    isolate(monkeypatch)
    clock = [datetime(2026, 10, 16, 14, 2, 11, 750000, tzinfo=timezone.utc)]

    def tick():
        clock[0] += timedelta(seconds=7)

    monkeypatch.setattr(mender_snapshot, "_now", lambda: clock[0])
    calls = fake_mender(monkeypatch, [DEVICES], [INVENTORY], on_call=tick)
    body = mender_snapshot.collect()
    assert body["generated_at"] == "2026-10-16T14:02:11Z"
    assert [url for url, _ in calls] == [DEVAUTH, DEVAUTH, INVENTORY_URL, INVENTORY_URL]
    assert body["devices"] == EXPECTED


# ── pushing ──────────────────────────────────────────────────────

BODY = {"generated_at": GENERATED_AT, "source": "mender-auto-accept@central", "devices": EXPECTED}
TARGETS = [
    ("https://prod.example/api/internal/mender/snapshot", "prod-token"),
    ("https://staging.example/api/internal/mender/snapshot", "staging-token"),
    ("http://localhost:8000/api/internal/mender/snapshot", "test-token"),
]


def fake_targets(monkeypatch, responses):
    """responses maps a url to the _Resp it answers or the exception it raises."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if isinstance(responses[url], Exception):
            raise responses[url]
        return responses[url]

    monkeypatch.setattr(mender_snapshot.requests, "post", fake_post)
    return calls


def test_one_failing_target_does_not_stop_the_others(monkeypatch, capsys):
    isolate(monkeypatch)
    calls = fake_targets(monkeypatch, {
        TARGETS[0][0]: _Resp(200, {"accepted": 7, "rejected": 1, "absent": 1, "generated_at": GENERATED_AT}),
        TARGETS[1][0]: mender_snapshot.requests.ConnectionError("connection refused"),
        TARGETS[2][0]: _Resp(200, {"accepted": 8, "rejected": 0, "absent": 0, "generated_at": GENERATED_AT}),
    })
    assert mender_snapshot.push(BODY, TARGETS) == 2
    assert calls == [
        (url, {"json": BODY, "headers": {"Authorization": f"Bearer {token}", "User-Agent": mender_snapshot.USER_AGENT},
               "timeout": 10,
               "allow_redirects": False})
        for url, token in TARGETS
    ]
    assert PAT not in repr(calls)
    out, err = capsys.readouterr()
    assert "Pushed snapshot to prod.example/api/internal/mender/snapshot: 8 devices, accepted 7, rejected 1, absent 1" in out
    assert "Pushed snapshot to localhost:8000/api/internal/mender/snapshot: 8 devices, accepted 8, rejected 0, absent 0" in out
    assert "Error pushing snapshot to staging.example/api/internal/mender/snapshot: connection refused" in err
    assert out.rstrip().endswith("Mender snapshot: pushed to 2 of 3 targets")


def test_the_push_sends_a_browser_user_agent_for_cloudflare(monkeypatch):
    """A script's default User-Agent gets 403 error 1010 from Cloudflare's browser integrity check."""
    isolate(monkeypatch)
    calls = fake_targets(monkeypatch, {TARGETS[0][0]: _Resp(200, {})})
    mender_snapshot.push(BODY, TARGETS[:1])
    headers = calls[0][1]["headers"]
    assert headers["User-Agent"] == mender_snapshot.USER_AGENT
    assert headers["User-Agent"].startswith("Mozilla/5.0 ")
    assert "python-requests" not in headers["User-Agent"]


def test_a_200_without_a_rejected_count_still_logs_the_others(monkeypatch, capsys):
    """Older receivers answer accepted and absent only."""
    isolate(monkeypatch)
    fake_targets(monkeypatch, {TARGETS[0][0]: _Resp(200, {"accepted": 8, "absent": 2})})
    assert mender_snapshot.push(BODY, TARGETS[:1]) == 1
    out, err = capsys.readouterr()
    assert "8 devices, accepted 8, rejected ?, absent 2" in out
    assert err == ""


def test_a_409_counts_as_delivered_and_is_not_an_error(monkeypatch, capsys):
    """A newer snapshot already landed there, from a retry or a second sender."""
    isolate(monkeypatch)
    fake_targets(monkeypatch, {TARGETS[0][0]: _Resp(409, {"detail": "stale generated_at"})})
    assert mender_snapshot.push(BODY, TARGETS[:1]) == 1
    out, err = capsys.readouterr()
    assert "a newer one already landed (409)" in out
    assert "pushed to 1 of 1 targets" in out
    assert err == ""


@pytest.mark.parametrize("resp,shown", [
    (_Resp(401, text="bad token"), "HTTP 401: bad token"),
    (_Resp(422, text="x" * 500), "HTTP 422: " + "x" * 200 + "\n"),
    (_Resp(500, text="<html>oops</html>"), "HTTP 500: <html>oops</html>"),
    (_Resp(301, headers={"Location": "https://www.prod.example/"}),
     "HTTP 301 (redirect to https://www.prod.example/, not followed)\n"),
    (_Resp(204, text=""), None),
])
def test_other_statuses_are_errors_and_bodies_are_cut(monkeypatch, capsys, resp, shown):
    isolate(monkeypatch)
    fake_targets(monkeypatch, {TARGETS[0][0]: resp})
    pushed = mender_snapshot.push(BODY, TARGETS[:1])
    out, err = capsys.readouterr()
    if shown is None:
        assert pushed == 1 and err == ""
    else:
        assert pushed == 0
        assert shown in err
        assert "x" * 201 not in err
        assert "pushed to 0 of 1 targets" in out


@pytest.mark.parametrize("payload", [mender_snapshot.requests.JSONDecodeError("Expecting value", "<html>", 0),
                                     ["not", "a", "dict"]])
def test_an_unreadable_2xx_body_still_counts(monkeypatch, capsys, payload):
    isolate(monkeypatch)
    fake_targets(monkeypatch, {TARGETS[0][0]: _Resp(200, payload)})
    assert mender_snapshot.push(BODY, TARGETS[:1]) == 1
    out, err = capsys.readouterr()
    assert "8 devices, accepted ?, rejected ?, absent ?" in out
    assert err == ""


# ── run(): when it pushes, and when it must not ──────────────────

def test_run_pushes_the_whole_fleet_to_every_target(monkeypatch):
    isolate(monkeypatch, targets=",".join(f"{u}={t}" for u, t in TARGETS))
    monkeypatch.setattr(mender_snapshot, "_now", lambda: datetime(2026, 10, 16, 14, 2, 11, tzinfo=timezone.utc))
    gets = fake_mender(monkeypatch, [DEVICES[:5], DEVICES[3:]], [INVENTORY[:4], INVENTORY[4:]])
    posts = fake_targets(monkeypatch, {url: _Resp(200, {"accepted": 8, "absent": 0}) for url, _ in TARGETS})
    mender_snapshot.run()
    assert len(gets) == 6
    assert all(kwargs["headers"] == {"Authorization": f"Bearer {PAT}"} for _, kwargs in gets)
    assert [url for url, _ in posts] == [url for url, _ in TARGETS]
    assert all(kwargs["json"] == BODY for _, kwargs in posts)
    assert PAT not in repr(posts)


@pytest.mark.parametrize("fail", [
    (DEVAUTH, 1, _Resp(500)),
    (DEVAUTH, 2, mender_snapshot.requests.ConnectionError("reset")),
    (DEVAUTH, 1, _Resp(200, {"devices": []})),
    (INVENTORY_URL, 1, _Resp(401)),
    (INVENTORY_URL, 2, mender_snapshot.requests.Timeout("read timed out")),
    (INVENTORY_URL, 2, _Resp(200, mender_snapshot.requests.JSONDecodeError("Expecting value", "<html>", 0))),
], ids=["devauth-500", "devauth-page2-reset", "devauth-not-a-list", "inventory-401", "inventory-page2-timeout",
        "inventory-page2-not-json"])
def test_a_failed_fetch_pushes_nothing(monkeypatch, capsys, fail):
    """A partial fleet would mark the missing devices absent on every server."""
    unexpected = isolate(monkeypatch, targets=f"{TARGETS[0][0]}={TARGETS[0][1]}")
    fake_mender(monkeypatch, [DEVICES[:4], DEVICES[4:]], [INVENTORY[:4], INVENTORY[4:]], fail=fail)
    posts = fake_targets(monkeypatch, {})
    mender_snapshot.run()
    assert posts == []
    assert unexpected == []
    assert "Error: Mender snapshot not pushed" in capsys.readouterr().err


def test_a_repeated_full_page_pushes_nothing(monkeypatch, capsys):
    """A server ignoring `page` with more than one page of devices: the rest would be marked absent."""
    unexpected = isolate(monkeypatch, targets=f"{TARGETS[0][0]}={TARGETS[0][1]}")
    page = full_page()
    fake_mender(monkeypatch, [page, page], [INVENTORY])
    posts = fake_targets(monkeypatch, {})
    mender_snapshot.run()
    assert posts == []
    assert unexpected == []
    assert f"Error: Mender snapshot not pushed: {mender_snapshot.DEVAUTH_PATH} page 2" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["\n", "\r", "\t", " ", "é", "\x00", "\x7f"],
                         ids=["lf", "cr", "tab", "space", "non-ascii", "nul", "del"])
@pytest.mark.parametrize("where", ["inner", "trailing"])
def test_a_pat_that_cannot_be_a_header_means_no_mender_call_and_is_never_printed(monkeypatch, capsys, bad, where):
    pat = f"PATHEAD{bad}PATTAIL" if where == "inner" else f"PATHEAD-PATTAIL{bad}"
    isolate(monkeypatch, targets=f"{TARGETS[0][0]}={TARGETS[0][1]}", pat=pat)
    attempted = build_then_refuse(monkeypatch)
    mender_snapshot.run()
    out, err = capsys.readouterr()
    assert attempted == []
    assert "Error: Mender snapshot not pushed: MENDER_PAT has a space, control or non-ASCII character" in err
    assert "PATHEAD" not in out + err
    assert "PATTAIL" not in out + err


@pytest.mark.parametrize("targets,pat", [
    ("", PAT),
    ("   ", PAT),
    (" , ,", PAT),
    ("http://remote.example/api/internal/mender/snapshot=t", PAT),
    ("no-equals-sign", PAT),
    ("https://prod.example/api/internal/mender/snapshot=t", None),
    ("https://prod.example/api/internal/mender/snapshot=t", ""),
], ids=["unset", "blank", "only-commas", "http-to-remote", "no-equals", "pat-unset", "pat-empty"])
def test_no_targets_or_no_pat_means_no_mender_call(monkeypatch, capsys, targets, pat):
    unexpected = isolate(monkeypatch, targets=targets, pat=pat)
    mender_snapshot.run()
    assert unexpected == []
    assert "not pushed" not in capsys.readouterr().err


def test_unset_targets_print_nothing(monkeypatch, capsys):
    unexpected = isolate(monkeypatch)
    mender_snapshot.run()
    assert unexpected == []
    assert capsys.readouterr() == ("", "")


def test_run_never_raises(monkeypatch, capsys):
    def boom():
        raise KeyError("devices")

    isolate(monkeypatch, targets=f"{TARGETS[0][0]}={TARGETS[0][1]}")
    monkeypatch.setattr(mender_snapshot, "collect", boom)
    assert mender_snapshot.run() is None
    assert "Error: Mender snapshot not pushed: 'devices'" in capsys.readouterr().err


# ── RETINA_SYNC_TARGETS ──────────────────────────────────────────

def test_targets_parse_and_bad_entries_never_print_their_token(capsys):
    raw = ", ".join([
        " https://prod.example/api/internal/mender/snapshot = tok==en= ",
        "",
        "secret-no-equals",
        "http://remote.example/x=secret-http",
        "http://localhost:8000/api/internal/mender/snapshot=local-tok",
        "https://test.example/x=",
        "=secret-no-url",
        "http://[::1]:8000/x=v6-tok",
        "https:///x=secret-no-host",
        "ftp://files.example/x=secret-ftp",
        "secret-swapped=https://prod.example/x",
        "HTTPS://Staging.Example/api/internal/mender/snapshot=st=g",
    ])
    assert mender_snapshot.parse_targets(raw) == [
        ("https://prod.example/api/internal/mender/snapshot", "tok==en="),
        ("http://localhost:8000/api/internal/mender/snapshot", "local-tok"),
        ("http://[::1]:8000/x", "v6-tok"),
        ("HTTPS://Staging.Example/api/internal/mender/snapshot", "st=g"),
    ]
    out, err = capsys.readouterr()
    assert out == ""
    assert err.count("Error: RETINA_SYNC_TARGETS entry") == 7
    assert "entry 2 has no" in err
    for leaked in ("secret", "tok==en=", "local-tok", "v6-tok", "st=g"):
        assert leaked not in err


@pytest.mark.parametrize("bad", ["\n", "\r", "\t", " ", "é", "’", "\x00", "\x7f"],
                         ids=["lf", "cr", "tab", "space", "latin-1", "non-latin-1", "nul", "del"])
def test_a_token_that_cannot_be_a_header_is_skipped_unprinted(capsys, bad):
    """requests would refuse some of these with an error quoting the token, which push() prints."""
    raw = f"https://prod.example/x=good-tok, http://localhost:9/api/internal/mender/snapshot=SECRETHEAD{bad}SECRETTAIL"
    assert mender_snapshot.parse_targets(raw) == [("https://prod.example/x", "good-tok")]
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ("Error: RETINA_SYNC_TARGETS entry 2 has a token with a space, control or non-ASCII character, "
                   "skipping it\n")


def test_a_token_with_a_newline_never_reaches_push(monkeypatch, capsys):
    """The reported case: requests refused the header and push() printed "Bearer <token>"."""
    isolate(monkeypatch)
    attempted = build_then_refuse(monkeypatch)
    targets = mender_snapshot.parse_targets("http://localhost:9/api/internal/mender/snapshot=SECRETTOKEN123\nSECONDLINE")
    mender_snapshot.push(BODY, targets)
    out, err = capsys.readouterr()
    assert attempted == []
    assert "SECRETTOKEN123" not in out + err
    assert "SECONDLINE" not in out + err


@pytest.mark.parametrize("raw", [None, "", " ", ",", " , ,"])
def test_empty_targets_parse_to_nothing_silently(capsys, raw):
    assert mender_snapshot.parse_targets(raw) == []
    assert capsys.readouterr() == ("", "")


# ── the hook in auto_accept.py ───────────────────────────────────

AUTO_ACCEPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_accept.py")


def test_auto_accept_pushes_after_its_pass_and_keeps_its_exit_status(monkeypatch):
    isolate(monkeypatch)
    calls = []
    monkeypatch.delenv("MENDER_PAT", raising=False)
    monkeypatch.setattr(mender_snapshot, "run", lambda: calls.append("snapshot"))
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(AUTO_ACCEPT, run_name="__main__")
    assert exit_info.value.code == 1
    assert calls == ["snapshot"]


def test_auto_accept_pushes_even_when_its_pass_raises(monkeypatch, tmp_path):
    """The traceback is main's, not swallowed, and the snapshot still goes."""
    def broken(url, **kwargs):
        raise RuntimeError("mender exploded")

    isolate(monkeypatch)
    calls = []
    monkeypatch.setenv("MENDER_PAT", "fake-pat")
    monkeypatch.setenv("PENDING_DEPLOY_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setattr(mender_snapshot.requests, "get", broken)
    monkeypatch.setattr(mender_snapshot, "run", lambda: calls.append("snapshot"))
    with pytest.raises(RuntimeError, match="mender exploded"):
        runpy.run_path(AUTO_ACCEPT, run_name="__main__")
    assert calls == ["snapshot"]
