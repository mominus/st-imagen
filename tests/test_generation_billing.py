from app.services.generation_billing import (
    contains_node_error,
    is_free_workflow_timeout,
    upstream_error_consumes_quota,
    upstream_event_consumes_quota,
)


def test_node_error_is_billable():
    message = (
        "Error in Node **Text to Image** (`action-0`): Invalid configuration: "
        "No image data found in Gemini API response"
    )
    assert contains_node_error(message)
    assert upstream_error_consumes_quota(message)
    assert upstream_event_consumes_quota({"progress_data": {"error": message}})


def test_progress_or_output_is_billable():
    assert upstream_event_consumes_quota({"progress_data": {"started_nodes": 1}})
    assert upstream_event_consumes_quota({"progress_data": {"current_node": "action-0"}})
    assert upstream_event_consumes_quota({"outputs": {"image": "https://example/image.png"}})


def test_admission_and_zero_progress_are_free():
    assert not upstream_event_consumes_quota(None)
    assert not upstream_event_consumes_quota({"run_id": "accepted"})
    assert not upstream_event_consumes_quota(
        {"progress_data": {"total_nodes": 4, "started_nodes": 0, "completed_nodes": 0}}
    )
    assert not upstream_error_consumes_quota("upstream connection error", {"status": 502})


def test_idle_and_total_timeouts_are_free_even_after_waiting():
    for reason in ("idle_timeout", "total_timeout"):
        payload = {"reason": reason}
        assert is_free_workflow_timeout(payload)
        assert not upstream_error_consumes_quota("workflow timeout", payload)
