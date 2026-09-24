"""Tests for the failed-deployment retry.

The fixtures are trimmed from real device logs pulled from hosted Mender on
2026-09-04, because the two mistakes that matter here were both invisible in
made-up logs:

  * the log is cumulative across attempts, so nightcrawler1's carries three
    attempts and lines dated April from a clock-skewed node, and
  * "Installing artifact..." is printed a second after the deployment starts,
    before the download, so it does not mark the install phase.

An earlier version of this classifier failed both, and the fleet logs caught it.
"""

import os
from datetime import datetime, timedelta, timezone

import deploy_retry
import pytest

FAILED_AT = datetime(2026, 9, 3, 21, 40, 57, tzinfo=timezone.utc)
ARTIFACT = "owl-os-pi5-v0.16.1"

# nightcrawler2, deployment 71904a03. Two truncated streams, then R2 answered
# 400 to a resumed range request and the client stopped at retry 2 of 10.
NIGHTCRAWLER2 = """\
info: Deployment with ID 71904a03 started.
info: Running State Script: /etc/mender/scripts/Download_Enter_00_retina_state
warning: end of stream: GET https://r2.cloudflarestorage.com/mender-artifacts-us/a4102b9c
info: Resuming download after 60 seconds. Retry 1/10
warning: stream truncated: GET https://r2.cloudflarestorage.com/mender-artifacts-us/a4102b9c
info: Resuming download after 60 seconds. Retry 2/10
error: Unexpected status code while fetching artifact: Bad Request
error: HTTP stream contains a body, but a reader has not been created for it: GET https://r2
"""

# Wilderness A, same rollout. Note the cancelled GET: aborting an install aborts
# the download too, so a fetch error appears in a failure a retry cannot fix.
WILDERNESS_DISK = """\
info: Deployment with ID 71904a03 started.
info: Running State Script: /etc/mender/scripts/Download_Enter_00_retina_state
info: Installing artifact...
error: No space left on device: Failed to create directory: '/var/lib/mender/modules/v3/payloads/0000/tree/tmp'
error: Operation canceled: GET https://r2.cloudflarestorage.com/mender-artifacts-us/a4102b9c: HTTP request cancelled
"""

# d7e24fb9, retina-node-v0.4.5.0. The update module ran and exited non-zero,
# and the rollback failed after it.
INSTALL_FAILED = """\
info: Deployment with ID d02677b0 started.
info: Installing artifact...
error: Process returned non-zero exit status: ArtifactInstall: Process exited with status 1
error: Process returned non-zero exit status: ArtifactRollback: Process exited with status 1
"""

# nightcrawler1, deployment d0c9144c, trimmed to two of its three attempts.
# "Installing artifact..." is one second in; the download gives up hours later.
CUMULATIVE = """\
info: Deployment with ID d0c9144c started.
info: Installing artifact...
error: No space left on device: Failed to create directory
info: Deployment with ID d0c9144c started.
info: Installing artifact...
warning: Reading error, a new request will be re-scheduled. Connection reset by peer: Could not read body
error: Resume download error: Giving up on resuming the download: Tried maximum number of times: Exponential backoff
"""


def entry(artifact, status, created):
    """One record as the per-device deployments endpoint returns it."""
    return {"deployment": {"artifact_name": artifact, "created": created.isoformat().replace("+00:00", "Z")},
            "device": {"status": status}}


# ── what a new deployment can fix ────────────────────────────────

def test_a_download_that_died_on_a_bad_status_is_retried():
    retry, reason = deploy_retry.classify(NIGHTCRAWLER2)
    assert retry
    assert reason == "the artifact never finished downloading"


def test_a_download_that_exhausted_its_backoff_is_retried():
    log = "info: Deployment with ID d0c9144c started.\nerror: Giving up on resuming the download: Tried maximum number of times"
    assert deploy_retry.classify(log)[0]


def test_a_full_disk_is_not_retried():
    retry, reason = deploy_retry.classify(WILDERNESS_DISK)
    assert not retry
    assert reason == "no disk space on the node"


def test_a_cancelled_download_alongside_a_disk_failure_is_not_read_as_transport():
    """Aborting an install cancels the GET, so the fetch error is a symptom of
    the real failure. Reading it as transport would retry a full disk forever."""
    assert "Operation canceled: GET" in WILDERNESS_DISK
    assert not deploy_retry.classify(WILDERNESS_DISK)[0]


def test_an_update_step_that_ran_and_failed_is_not_retried():
    retry, reason = deploy_retry.classify(INSTALL_FAILED)
    assert not retry
    assert reason == "an update step ran and failed on the node"


def test_a_failed_rollback_is_never_retried():
    """The node is in a state nobody has inspected. It needs a person."""
    assert not deploy_retry.classify("started.\nerror: ArtifactRollback: Process exited with status 1")[0]


# ── default deny ─────────────────────────────────────────────────

@pytest.mark.parametrize("log,expected_reason", [
    (None, "could not read the deployment log"),
    ("", "no deployment log to read"),
    ("   \n \n", "no deployment log to read"),
    ("error: something nobody has seen before", "failed for an unrecognised reason"),
])
def test_anything_we_cannot_positively_identify_is_refused(log, expected_reason):
    """A missed retry costs one hand deployment. A wrong retry reboots a live
    node on a timer, so silence must never authorise one."""
    retry, reason = deploy_retry.classify(log)
    assert not retry
    assert reason == expected_reason


def test_an_api_failure_reading_the_log_is_distinct_from_an_empty_log():
    """device_log returns None on error rather than "", or a transient API
    problem would look like a clean log and fall into the same bucket."""
    assert deploy_retry.classify(None)[0] is False
    assert deploy_retry.classify("")[1] != deploy_retry.classify(None)[1]


# ── the cumulative log ───────────────────────────────────────────

def test_only_the_final_attempt_is_classified():
    """nightcrawler1's log holds a disk failure from one attempt and a download
    failure from a later one. Matching the whole log would let the older line
    veto a retry the newer failure has earned."""
    assert "No space left on device" in CUMULATIVE
    assert deploy_retry.classify(CUMULATIVE)[0]


def test_a_newer_disk_failure_still_vetoes_an_older_download_failure():
    log = ("started.\nerror: Giving up on resuming the download\n"
           "info: Deployment with ID x started.\nerror: No space left on device\n")
    assert not deploy_retry.classify(log)[0]


def test_installing_artifact_is_not_treated_as_reaching_the_install_phase():
    """It is printed about a second after the deployment starts, before the
    download. Wilderness A's disk log shows it one second in; nightcrawler1's
    download gave up five hours after the same line."""
    assert "Installing artifact..." in CUMULATIVE
    assert deploy_retry.classify(CUMULATIVE)[0]


def test_a_log_with_no_attempt_marker_is_still_read():
    assert deploy_retry.last_attempt("error: Giving up on resuming the download").strip()


# ── the artifact_name trap ───────────────────────────────────────

def test_a_newer_unrelated_artifact_does_not_count_as_landed():
    """nightcrawler2 installed retina-node-v0.4.5.0 minutes before failing the OS
    update, so its most recent successful deployment is for a different artifact.
    Treating any later success as recovery would abandon the node on v0.15.0."""
    history = [entry("retina-node-v0.4.5.0", "success", FAILED_AT + timedelta(hours=1))]
    assert not deploy_retry.has_landed(history, ARTIFACT, FAILED_AT)


def test_a_success_for_the_same_artifact_after_the_failure_counts():
    history = [entry(ARTIFACT, "success", FAILED_AT + timedelta(hours=1))]
    assert deploy_retry.has_landed(history, ARTIFACT, FAILED_AT)


def test_already_installed_counts_as_landed():
    """Mender reports already-installed when the device turns out to have it."""
    history = [entry(ARTIFACT, "already-installed", FAILED_AT + timedelta(hours=1))]
    assert deploy_retry.has_landed(history, ARTIFACT, FAILED_AT)


def test_a_success_from_before_the_failure_does_not_count():
    history = [entry(ARTIFACT, "success", FAILED_AT - timedelta(days=14))]
    assert not deploy_retry.has_landed(history, ARTIFACT, FAILED_AT)


# ── do not stack deployments on a working node ───────────────────

@pytest.mark.parametrize("status", ["pending", "downloading", "installing", "rebooting"])
def test_a_device_mid_update_is_busy(status):
    assert deploy_retry.is_busy([entry(ARTIFACT, status, FAILED_AT)])


def test_a_device_with_only_finished_deployments_is_not_busy():
    assert not deploy_retry.is_busy([
        entry(ARTIFACT, "failure", FAILED_AT),
        entry("retina-node-v0.4.5.0", "success", FAILED_AT),
    ])


# ── state, which is what bounds the whole thing ──────────────────

@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits this test relies on")
def test_unwritable_state_stops_the_pass_before_anything_is_created(tmp_path, monkeypatch):
    """Under the packaged unit ProtectSystem=strict leaves the checkout
    read-only. Creating deployments we then cannot count is how a retry becomes
    a storm, so an unwritable state file must abort before any API write."""
    readonly = tmp_path / "ro"
    readonly.mkdir()
    readonly.chmod(0o500)
    monkeypatch.setattr(deploy_retry, "STATE_FILE", str(readonly / "state.json"))
    try:
        assert not deploy_retry.check_state_writable()
    finally:
        readonly.chmod(0o700)


def test_writable_state_passes_the_preflight(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_retry, "STATE_FILE", str(tmp_path / "sub" / "state.json"))
    assert deploy_retry.check_state_writable()


def test_prune_drops_pairs_with_no_live_failure():
    """An exhausted count must not outlive the failure that earned it, or the
    node is refused a retry the next time it genuinely needs one."""
    state = {"dev1|art": {"attempts": 2}, "dev2|art": {"attempts": 1}}
    assert deploy_retry.prune_state(state, {"dev1|art"}) == {"dev1|art": {"attempts": 2}}


def test_corrupt_state_reads_as_empty_rather_than_crashing(tmp_path, monkeypatch):
    bad = tmp_path / "state.json"
    bad.write_text("{not json")
    monkeypatch.setattr(deploy_retry, "STATE_FILE", str(bad))
    assert deploy_retry.load_state() == {}


def test_parse_ts_handles_menders_format():
    assert deploy_retry.parse_ts("2026-09-03T21:40:57.313Z") == datetime(
        2026, 9, 3, 21, 40, 57, 313000, tzinfo=timezone.utc)


def test_parse_ts_survives_a_missing_or_broken_timestamp():
    assert deploy_retry.parse_ts(None) is None
    assert deploy_retry.parse_ts("not a date") is None


# ── the accepted check, which 404s the whole fleet if it reads the wrong API ──

class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise deploy_retry.requests.HTTPError(f"{self.status_code}")


def test_accepted_check_uses_the_v2_devauth_api(monkeypatch):
    """devauth is v2 while deployments are v1. Asking v1 returns 404, which
    reads as a decommissioned device and silently skips every retry."""
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        return _Resp(200, {"status": "accepted"})

    monkeypatch.setattr(deploy_retry.requests, "get", fake_get)
    assert deploy_retry.is_accepted("dev1")
    assert "/api/management/v2/devauth/devices/dev1" in seen["url"]


def test_a_missing_device_is_not_accepted(monkeypatch):
    monkeypatch.setattr(deploy_retry.requests, "get", lambda url, **kw: _Resp(404))
    assert not deploy_retry.is_accepted("gone")


def test_a_network_error_does_not_condemn_the_device(monkeypatch):
    """Assume accepted and ask again next pass, matching auto_accept."""
    def boom(url, **kwargs):
        raise deploy_retry.requests.ConnectionError("unreachable")

    monkeypatch.setattr(deploy_retry.requests, "get", boom)
    assert deploy_retry.is_accepted("dev1")


def test_device_log_returns_none_when_the_api_fails(monkeypatch):
    def boom(url, **kwargs):
        raise deploy_retry.requests.ConnectionError("unreachable")

    monkeypatch.setattr(deploy_retry.requests, "get", boom)
    assert deploy_retry.device_log("dep", "dev") is None
