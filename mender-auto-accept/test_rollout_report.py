"""Tests for the per-node rollout report's verdicts.

The cases are the fleet's real states on 2026-09-24, after the v0.4.6.0 and
owl-os v0.17.0 rollout.
"""

import rollout_report as rr

OS = "owl-os-pi5-v0.17.0"
STACK = "retina-node-v0.4.6.0"


def test_latest_stable_ignores_dev_and_rc_and_orders_numerically():
    names = ["owl-os-pi5-v0.9.0", "owl-os-pi5-v0.17.0", "owl-os-pi5-v0.17.1-dev", "owl-os-pi5-v0.16.1",
             "retina-node-v0.4.6.0", "retina-node-v0.4.10.0", "retina-node-v0.4.7.0-dev2"]
    assert rr.latest_stable("owl-os", names) == "owl-os-pi5-v0.17.0"
    assert rr.latest_stable("retina-node", names) == "retina-node-v0.4.10.0"


def test_on_target_reads_the_per_type_attributes_not_artifact_name():
    # nightcrawler2's old trap: artifact_name showed the stack while the OS lagged.
    attrs = {rr.OS_ATTR: "v0.16.1", rr.STACK_ATTR: STACK, "artifact_name": STACK}
    assert rr.on_target(attrs, OS, STACK) == (False, True)


def test_a_node_offline_since_before_the_rollout_is_pending():
    # Roswell: accepted in June, silent since July, no deployment assigned yet.
    verdict, why = rr.missing_verdict(None, None, False, in_rollout=True, check_in="2026-07-26T02:32")
    assert verdict == "pending"
    assert "2026-07-26" in why


def test_a_node_accepted_after_the_rollout_is_not_targeted():
    assert rr.missing_verdict(None, None, False, in_rollout=False)[0] == "not targeted"


def test_an_assigned_deployment_still_in_progress_is_pending():
    assert rr.missing_verdict("downloading", None, False) == ("pending", "deployment downloading")


def test_a_failure_the_retry_would_fix_is_retryable():
    assert rr.missing_verdict("failure", "the artifact never finished downloading", True) == (
        "retryable", "the artifact never finished downloading")


def test_a_failure_no_retry_fixes_needs_a_person():
    assert rr.missing_verdict("failure", "no disk space on the node", False)[0] == "needs a person"


def test_success_ahead_of_inventory_counts_as_landed():
    assert rr.missing_verdict("success", None, False)[0] == "landed"


def test_the_worst_target_decides_and_every_miss_is_listed():
    verdict, why = rr.node_verdict([("pending", "os: deployment pending"), ("needs a person", "stack: disk")], None)
    assert verdict == "needs a person"
    assert "os: deployment pending" in why and "stack: disk" in why


def test_landed_with_an_unhealthy_stack_is_flagged():
    assert rr.node_verdict([("landed", ""), ("landed", "")], "degraded") == ("landed", "but retina_stack=degraded")


def test_landed_and_healthy_or_unreported_is_plain():
    assert rr.node_verdict([("landed", ""), ("landed", "")], "up") == ("landed", "")
    assert rr.node_verdict([("landed", ""), ("landed", "")], None) == ("landed", "")


def test_dev_builds_are_test_builds():
    assert rr.is_test_build({rr.OS_ATTR: "v0.17.1-dev", rr.STACK_ATTR: "retina-node-v0.4.6.0"})
    assert not rr.is_test_build({rr.OS_ATTR: "v0.17.0", rr.STACK_ATTR: STACK})


def test_rollout_start_counts_only_fleet_wide_deployments():
    deployments = [
        {"artifact_name": STACK, "created": "2026-09-23T11:00:39Z", "filter": {"id": ""}},
        {"artifact_name": STACK, "created": "2026-09-23T14:13:14Z",
         "filter": {"terms": [{"attribute": "status", "value": "accepted"}]}},
        {"artifact_name": OS, "created": "2026-09-23T14:13:22Z",
         "filter": {"terms": [{"attribute": "status", "value": "accepted"}]}},
    ]
    assert rr.rollout_start(deployments, STACK) == "2026-09-23T14:13:14Z"
    assert rr.rollout_start(deployments, "retina-node-v9.9.9.9") is None
