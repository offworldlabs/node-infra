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


# --- Failures the first version missed (2026-09-24 replay over all 45 logs) ---

# nightcrawler2, deployment dde1802e: three hours of resumed reads on Wi-Fi,
# then the client killed its own download.
NIGHTCRAWLER2_TIMEOUT = """\
2026-09-04 10:07:03.44 +0000 UTC info: Deployment with ID dde1802e started.
2026-09-04 10:07:04.274 +0000 UTC info: Installing artifact...
2026-09-04 11:10:03.08 +0000 UTC info: Resuming download after 60 seconds. Retry 1/10
2026-09-04 14:07:04.337 +0000 UTC info: Sending SIGTERM to PID 2631957
2026-09-04 14:07:04.355 +0000 UTC info: PID 2631957 exited with status 15
2026-09-04 14:07:04.355 +0000 UTC error: Connection timed out: Update Module Download process timed out
2026-09-04 14:07:04.356 +0000 UTC error: Operation canceled: GET https://r2.cloudflarestorage.com/mender-artifacts-us/a4102b9c
"""

# Jonathan 1, deployment f20eee61: the client lost its pipe to the module.
JONATHAN1_STREAM = """\
2026-06-18 17:58:17.342 +0000 UTC info: Deployment with ID f20eee61 started.
2026-06-18 17:58:23.828 +0000 UTC info: Installing artifact...
2026-06-18 18:01:41.358 +0000 UTC error: Cancel::open() returned error: No such file or directory
2026-06-18 18:01:41.38 +0000 UTC error: No such file or directory: Cannot open /var/lib/mender/modules/v3/payloads/0000/tree/stream-next
2026-06-18 18:01:41.454 +0000 UTC error: Operation canceled: GET https://r2.cloudflarestorage.com/mender-artifacts-us/86cea7f7
"""

# retc47d6f72, owl-os v0.17.0, deployment d175ca0d: the site lost power at the
# reboot into the new partition and the node was dark for eight hours. It came
# back on the old partition, so ArtifactVerifyReboot failed: the A/B fallback
# working, not a broken image.
RETC47_POWER_CUT_AT_REBOOT = """\
2026-09-24 02:13:47.371 +0000 UTC info: Deployment with ID d175ca0d started.
2026-09-24 02:15:00.945 +0000 UTC info: Sending status update to server
2026-09-24 02:15:01.235 +0000 UTC info: Calling `reboot` command and waiting for system to restart.
2026-09-24 02:15:01.366 +0000 UTC info: Termination signal received, shutting down gracefully
2026-09-24 10:02:12.702 +0000 UTC info: Running mender-update 5.1.0
2026-09-24 10:02:12.922 +0000 UTC info: The update client daemon is now ready to handle incoming deployments
2026-09-24 10:02:13.228 +0000 UTC error: Process returned non-zero exit status: ArtifactVerifyReboot: Process exited with status 1
2026-09-24 10:02:52.793 +0000 UTC info: Running mender-update 5.1.0
2026-09-24 10:02:53.367 +0000 UTC info: Running State Script: /var/lib/mender/scripts/ArtifactFailure_Enter_00_retina_state
"""

# ret9573ecda, owl-os v0.17.0, deployment d175ca0d: dark for four hours while
# installing.
RET9573_POWER_CUT_MID_INSTALL = """\
2026-09-23 22:21:41.897 +0000 UTC info: Deployment with ID d175ca0d started.
2026-09-23 22:21:42.147 +0000 UTC info: Running State Script: /etc/mender/scripts/Download_Enter_00_retina_state
2026-09-23 22:21:42.451 +0000 UTC info: Installing artifact...
2026-09-24 02:13:35.612 +0000 UTC info: Running mender-update 5.1.0
2026-09-24 02:13:35.763 +0000 UTC info: The update client daemon is now ready to handle incoming deployments
2026-09-24 02:13:35.805 +0000 UTC info: Sending status update to server
"""

# Josh Test Node 2, deployment 71fe1a9b: a deliberate sysrq power cut, back in
# 51 s. Too short to tell apart from a quick reboot, so it is not counted.
SHORT_POWER_CUT = """\
2026-09-24 10:33:01.292 +0000 UTC info: Deployment with ID 71fe1a9b started.
2026-09-24 10:35:55.588 +0000 UTC info: Update Module output (stdout): extracting images
2026-09-24 10:36:46.448 +0000 UTC info: Running mender-update 5.1.0
2026-09-24 10:36:50.041 +0000 UTC info: Update Module output (stdout): Rolling back docker-compose artifact retina-node-v0.4.6.0
"""

# What a broken OS image looks like: it reboots, comes back within a minute on
# the old partition, and fails verification. Retrying it would reboot a live
# node again for nothing.
BROKEN_OS_IMAGE = """\
2026-09-24 12:00:00.000 +0000 UTC info: Deployment with ID bad0s000 started.
2026-09-24 12:03:00.000 +0000 UTC info: Calling `reboot` command and waiting for system to restart.
2026-09-24 12:03:41.000 +0000 UTC info: Running mender-update 5.1.0
2026-09-24 12:03:41.500 +0000 UTC error: Process returned non-zero exit status: ArtifactVerifyReboot: Process exited with status 1
"""

# A failed install step, then a power cut. The failure came first and is the
# cause; the power cut does not make it retryable.
FAILED_THEN_POWER_CUT = """\
2026-09-24 12:00:00.000 +0000 UTC info: Deployment with ID fail0000 started.
2026-09-24 12:04:00.000 +0000 UTC error: Process returned non-zero exit status: ArtifactInstall: Process exited with status 1
2026-09-24 16:00:00.000 +0000 UTC info: Running mender-update 5.1.0
"""


def test_a_download_that_timed_out_is_retried():
    assert deploy_retry.classify(NIGHTCRAWLER2_TIMEOUT) == (True, "the artifact never finished downloading")


def test_a_lost_stream_to_the_module_is_retried():
    assert deploy_retry.classify(JONATHAN1_STREAM) == (True, "the artifact never finished downloading")


@pytest.mark.parametrize("log,hours", [(RETC47_POWER_CUT_AT_REBOOT, "7.8"), (RET9573_POWER_CUT_MID_INSTALL, "3.9")])
def test_a_long_power_cut_is_reported_but_not_retried_by_default(log, hours, monkeypatch):
    monkeypatch.setattr(deploy_retry, "RETRY_INTERRUPTED", False)
    retry, reason = deploy_retry.classify(log)
    assert retry is False
    assert f"power lost mid-deployment (node dark {hours} h)" in reason
    assert "not enabled" in reason


@pytest.mark.parametrize("log", [RETC47_POWER_CUT_AT_REBOOT, RET9573_POWER_CUT_MID_INSTALL])
def test_a_long_power_cut_is_retried_once_enabled(log, monkeypatch):
    monkeypatch.setattr(deploy_retry, "RETRY_INTERRUPTED", True)
    retry, reason = deploy_retry.classify(log)
    assert retry is True
    assert reason.startswith("power lost mid-deployment")


@pytest.mark.parametrize("log", [SHORT_POWER_CUT, BROKEN_OS_IMAGE, FAILED_THEN_POWER_CUT])
def test_a_short_gap_or_an_earlier_failure_is_never_read_as_a_power_cut(log, monkeypatch):
    monkeypatch.setattr(deploy_retry, "RETRY_INTERRUPTED", True)
    retry, reason = deploy_retry.classify(log)
    assert retry is False
    assert "power lost" not in reason


def test_disk_full_still_vetoes_a_power_cut(monkeypatch):
    monkeypatch.setattr(deploy_retry, "RETRY_INTERRUPTED", True)
    log = RET9573_POWER_CUT_MID_INSTALL + "2026-09-24 02:14:00.000 +0000 UTC error: No space left on device\n"
    assert deploy_retry.classify(log) == (False, "no disk space on the node")


# nightcrawler1, deployment d0c9144c: the download gave up, and the node was
# then off for 44 hours. The failure is the download, not the power cut.
NIGHTCRAWLER1_GAVE_UP_THEN_DARK = """\
2026-08-23 12:52:58.618 +0000 UTC info: Deployment with ID d0c9144c started.
2026-08-23 12:53:08.624 +0000 UTC info: Installing artifact...
2026-08-23 16:50:37.434 +0000 UTC info: Resuming download after 60 seconds. Retry 10/10
2026-08-23 17:29:04.684 +0000 UTC error: Resume download error: Giving up on resuming the download: Tried maximum number of times: Exponential backoff
2026-08-25 13:41:04.317 +0000 UTC info: Running mender-update 5.1.0
"""


@pytest.mark.parametrize("enabled", [False, True])
def test_a_download_that_gave_up_before_a_power_cut_is_still_a_download_failure(enabled, monkeypatch):
    monkeypatch.setattr(deploy_retry, "RETRY_INTERRUPTED", enabled)
    assert deploy_retry.classify(NIGHTCRAWLER1_GAVE_UP_THEN_DARK) == (True, "the artifact never finished downloading")
