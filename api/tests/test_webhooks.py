import time

import pytest

from courtside_api import webhooks

SECRET = "test-webhook-secret"
BODY = b'{"event":"job.completed","job_id":"abc"}'


def test_signature_roundtrips():
    header = webhooks.sign(BODY, SECRET)
    assert header.startswith("t=") and ",v1=" in header
    assert webhooks.verify(BODY, header, SECRET)


def test_a_tampered_body_fails_verification():
    header = webhooks.sign(BODY, SECRET)
    assert not webhooks.verify(BODY + b" ", header, SECRET)


def test_the_wrong_secret_fails_verification():
    assert not webhooks.verify(BODY, webhooks.sign(BODY, SECRET), "other-secret")


def test_old_signatures_are_rejected_as_replays():
    stale = webhooks.sign(BODY, SECRET, timestamp=int(time.time()) - 10_000)
    assert not webhooks.verify(BODY, stale, SECRET)
    # ...but the same signature verifies with a wide enough tolerance, proving
    # the rejection is about age and not a broken MAC.
    assert webhooks.verify(BODY, stale, SECRET, tolerance_s=100_000)


@pytest.mark.parametrize("header", ["", "garbage", "t=abc,v1=x", "v1=deadbeef", "t=1"])
def test_malformed_signature_headers_are_rejected(header):
    assert not webhooks.verify(BODY, header, SECRET)


def test_payload_carries_what_a_mobile_client_needs_to_react():
    import uuid
    from datetime import datetime, timezone

    job = {
        "id": uuid.uuid4(), "status": "succeeded", "phase": "done", "progress": 100,
        "error": None, "error_code": None, "cost_usd": 5.17,
        "clips_total": 146, "clips_done": 146,
        "finished_at": datetime(2026, 8, 30, tzinfo=timezone.utc),
    }
    payload = webhooks.build_payload(job, "job.completed")
    assert payload["status"] == "succeeded"
    assert payload["cost_usd"] == 5.17
    assert payload["finished_at"].startswith("2026-08-30")
    assert isinstance(payload["job_id"], str)
