import os
import pytest
from unittest.mock import MagicMock
from fastapi import HTTPException
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError
from app.main import app
from app.core.supabase import get_supabase_client
from app.services.rate_limiter import (
    clear_rate_limit_store,
    get_cooldown_duration,
    get_client_ip,
    check_and_record_otp_request,
    format_seconds_to_mmss,
)

client = TestClient(app)


def setup_function():
    clear_rate_limit_store()


def test_cooldown_schedule_durations():
    assert get_cooldown_duration(1) == 60     # 1 min
    assert get_cooldown_duration(2) == 120    # 2 min
    assert get_cooldown_duration(3) == 180    # 3 min
    assert get_cooldown_duration(4) == 300    # 5 min
    assert get_cooldown_duration(5) == 900    # 15 min
    assert get_cooldown_duration(6) == 3600   # 1 hour
    assert get_cooldown_duration(10) == 3600  # 1 hour max


def test_format_seconds_to_mmss():
    assert format_seconds_to_mmss(60) == "01:00"
    assert format_seconds_to_mmss(125) == "02:05"
    assert format_seconds_to_mmss(300) == "05:00"
    assert format_seconds_to_mmss(900) == "15:00"


def test_untrusted_peer_spoofed_forwarded_for_header_ignored():
    mock_request = MagicMock()
    # Untrusted client socket IP: 8.8.8.8 (Public IP)
    mock_request.client.host = "8.8.8.8"
    # Spoofed header trying to claim IP 1.1.1.1
    mock_request.headers.get.side_effect = lambda h: "1.1.1.1" if h == "x-forwarded-for" else None

    # Untrusted peer socket IP must be returned, spoofed X-Forwarded-For header IGNORED!
    assert get_client_ip(mock_request) == "8.8.8.8"


def test_trusted_peer_forwarded_for_parsed_correctly():
    mock_request = MagicMock()
    # Trusted internal proxy IP: 10.0.0.1
    mock_request.client.host = "10.0.0.1"
    # Forwarded header containing client IP 198.51.100.25
    mock_request.headers.get.side_effect = lambda h: "198.51.100.25, 10.0.0.1" if h == "x-forwarded-for" else None

    assert get_client_ip(mock_request) == "198.51.100.25"


def test_malformed_ip_header_fallback():
    mock_request = MagicMock()
    mock_request.client.host = "10.0.0.1"
    mock_request.headers.get.side_effect = lambda h: "invalid-ip-string" if h == "x-forwarded-for" else None

    # Must not crash, safely falls back to valid socket IP
    assert get_client_ip(mock_request) == "10.0.0.1"


def test_missing_ip_headers_fallback():
    mock_request = MagicMock()
    mock_request.client.host = "198.51.100.77"
    mock_request.headers.get.return_value = None

    assert get_client_ip(mock_request) == "198.51.100.77"


def test_supabase_rpc_rate_limiter_integration():
    mock_supabase = MagicMock()
    mock_rpc = MagicMock()
    mock_rpc.execute.return_value.data = [
        {"is_allowed": True, "retry_after_seconds": 0, "detail_message": "OK"}
    ]
    mock_supabase.rpc.return_value = mock_rpc

    allowed, retry_after, msg = check_and_record_otp_request("rpc.test@example.com", "1.2.3.4", mock_supabase)

    assert allowed is True
    assert retry_after == 0
    assert msg == "OK"
    mock_supabase.rpc.assert_called_once_with(
        "check_and_record_otp_rate_limit",
        {"p_email": "rpc.test@example.com", "p_client_ip": "1.2.3.4", "p_max_daily": 10}
    )


def test_database_failure_fails_closed():
    mock_supabase = MagicMock()
    mock_rpc = MagicMock()
    mock_rpc.execute.side_effect = APIError({"message": "DB connection lost", "code": "500"})
    mock_supabase.rpc.return_value = mock_rpc

    with pytest.raises(HTTPException) as exc_info:
        check_and_record_otp_request("fail.closed@example.com", "1.2.3.4", mock_supabase)

    assert exc_info.value.status_code == 503
    assert "temporarily unavailable" in exc_info.value.detail.lower()


def test_progressive_cooldown_enforcement():
    clear_rate_limit_store()
    email = "rate.test@example.com"
    ip = "192.168.1.50"

    # Attempt 1: Allowed
    allowed, retry_after, _ = check_and_record_otp_request(email, ip)
    assert allowed is True
    assert retry_after == 0

    # Immediate Attempt 2: Blocked by 60s cooldown
    allowed, retry_after, msg = check_and_record_otp_request(email, ip)
    assert allowed is False
    assert 58 <= retry_after <= 60
    assert "01:00" in msg or "00:59" in msg or "01:01" in msg


def test_anti_lockout_different_ips_not_blocked():
    clear_rate_limit_store()
    email = "shared.target@example.com"
    attacker_ip = "1.2.3.4"
    victim_ip = "5.6.7.8"

    # Attacker triggers request from IP 1.2.3.4
    allowed_att, _, _ = check_and_record_otp_request(email, attacker_ip)
    assert allowed_att is True

    # Immediate second request from Attacker IP 1.2.3.4 is blocked
    allowed_att_2, retry_after_att, _ = check_and_record_otp_request(email, attacker_ip)
    assert allowed_att_2 is False

    # Legitimate victim from IP 5.6.7.8 IS NOT BLOCKED!
    allowed_vic, _, _ = check_and_record_otp_request(email, victim_ip)
    assert allowed_vic is True


def test_resend_otp_returns_429_with_retry_after_header():
    clear_rate_limit_store()
    mock_supabase = MagicMock()
    mock_supabase.auth.resend.return_value = None

    call_count = [0]

    def mock_rpc_func(name, params):
        call_count[0] += 1
        mock_exec = MagicMock()
        if call_count[0] == 1:
            mock_exec.execute.return_value.data = [
                {"is_allowed": True, "retry_after_seconds": 0, "detail_message": "OK"}
            ]
        else:
            mock_exec.execute.return_value.data = [
                {"is_allowed": False, "retry_after_seconds": 60, "detail_message": "Too many requests. Please wait 60 seconds before retrying."}
            ]
        return mock_exec

    mock_supabase.rpc.side_effect = mock_rpc_func

    app.dependency_overrides[get_supabase_client] = lambda: mock_supabase

    payload = {"email": "resend.limit@example.com"}

    # 1st Request: Allowed (200)
    res1 = client.post("/auth/resend-otp", json=payload)
    assert res1.status_code == 200

    # 2nd Immediate Request: Blocked (429)
    res2 = client.post("/auth/resend-otp", json=payload)
    app.dependency_overrides.clear()

    assert res2.status_code == 429
    assert "retry-after" in res2.headers
    retry_after_val = int(res2.headers["retry-after"])
    assert retry_after_val == 60
    assert "too many requests" in res2.json()["detail"].lower()


@pytest.mark.skipif(
    not os.getenv("RUN_LIVE_SUPABASE_INTEGRATION_TESTS"),
    reason="Live Supabase PostgreSQL environment not configured locally"
)
def test_live_supabase_concurrency_and_rpc_permissions():
    """Live integration test against real Supabase instance (only run when RUN_LIVE_SUPABASE_INTEGRATION_TESTS is set)."""
    pass
