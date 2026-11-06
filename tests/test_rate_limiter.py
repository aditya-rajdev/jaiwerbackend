
import os
from unittest.mock import MagicMock, patch

import pytest
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
    validate_rate_limit_response,
)


client = TestClient(app, client=("127.0.0.1", 50000))


def setup_function():
    clear_rate_limit_store()


def _set_supabase_override(mock_supabase):
    """Install a temporary Supabase dependency override and preserve the old one."""
    previous = app.dependency_overrides.get(get_supabase_client)

    app.dependency_overrides[get_supabase_client] = (
        lambda: mock_supabase
    )

    return previous


def _restore_supabase_override(previous):
    """Restore the original dependency override."""
    if previous is None:
        app.dependency_overrides.pop(get_supabase_client, None)
    else:
        app.dependency_overrides[get_supabase_client] = previous


# ---------------------------------------------------------
# Cooldown tests
# ---------------------------------------------------------

def test_cooldown_schedule_durations():
    assert get_cooldown_duration(1) == 60
    assert get_cooldown_duration(2) == 120
    assert get_cooldown_duration(3) == 180
    assert get_cooldown_duration(4) == 300
    assert get_cooldown_duration(5) == 900
    assert get_cooldown_duration(6) == 3600
    assert get_cooldown_duration(10) == 3600


def test_format_seconds_to_mmss():
    assert format_seconds_to_mmss(60) == "01:00"
    assert format_seconds_to_mmss(125) == "02:05"
    assert format_seconds_to_mmss(300) == "05:00"
    assert format_seconds_to_mmss(900) == "15:00"


# ---------------------------------------------------------
# Client IP and trusted proxy tests
# ---------------------------------------------------------

def test_untrusted_peer_spoofed_forwarded_for_header_ignored():
    mock_request = MagicMock()
    mock_request.client.host = "8.8.8.8"

    mock_request.headers.get.side_effect = (
        lambda h: "1.1.1.1" if h == "x-forwarded-for" else None
    )

    assert get_client_ip(mock_request) == "8.8.8.8"


def test_trusted_peer_forwarded_for_parsed_correctly():
    mock_settings = MagicMock()
    mock_settings.trusted_proxies = "10.0.0.0/8"

    with patch(
        "app.services.rate_limiter.get_settings",
        return_value=mock_settings,
    ):
        mock_request = MagicMock()
        mock_request.client.host = "10.0.0.1"

        mock_request.headers.get.side_effect = (
            lambda h: (
                "198.51.100.25, 10.0.0.1"
                if h == "x-forwarded-for"
                else None
            )
        )

        assert get_client_ip(mock_request) == "198.51.100.25"


def test_trusted_peer_malformed_xff_raises_503():
    mock_settings = MagicMock()
    mock_settings.trusted_proxies = "10.0.0.0/8"

    with patch(
        "app.services.rate_limiter.get_settings",
        return_value=mock_settings,
    ):
        mock_request = MagicMock()
        mock_request.client.host = "10.0.0.1"

        mock_request.headers.get.side_effect = (
            lambda h: (
                "invalid-ip-string"
                if h == "x-forwarded-for"
                else None
            )
        )

        with pytest.raises(HTTPException) as exc_info:
            get_client_ip(mock_request)

        assert exc_info.value.status_code == 503
        assert "invalid forwarded client address" in (
            exc_info.value.detail.lower()
        )


def test_missing_ip_headers_fallback():
    mock_request = MagicMock()
    mock_request.client.host = "198.51.100.77"
    mock_request.headers.get.return_value = None

    assert get_client_ip(mock_request) == "198.51.100.77"


def test_unparseable_peer_ip_raises_503_in_production():
    mock_request = MagicMock()
    mock_request.client.host = "unparseable_invalid_host_string"
    mock_request.headers.get.return_value = None

    with pytest.raises(HTTPException) as exc_info:
        get_client_ip(mock_request)

    assert exc_info.value.status_code == 503
    assert "unable to determine client network address" in (
        exc_info.value.detail.lower()
    )


# ---------------------------------------------------------
# Supabase RPC response validation tests
# ---------------------------------------------------------

def test_validate_rate_limit_response_allowed():
    valid = [
        {
            "is_allowed": True,
            "retry_after_seconds": 0,
            "detail_message": "OK",
        }
    ]

    result = validate_rate_limit_response(valid)

    assert result["is_allowed"] is True
    assert result["retry_after_seconds"] == 0
    assert result["detail_message"] == "OK"


def test_validate_rate_limit_response_rejected():
    rejected = [
        {
            "is_allowed": False,
            "retry_after_seconds": 120,
            "detail_message": "Too many requests.",
        }
    ]

    result = validate_rate_limit_response(rejected)

    assert result["is_allowed"] is False
    assert result["retry_after_seconds"] == 120
    assert result["detail_message"] == "Too many requests."


@pytest.mark.parametrize(
    "bad_data",
    [
        pytest.param(None, id="none"),
        pytest.param([], id="empty-list"),
        pytest.param("not-a-list", id="not-a-list"),
        pytest.param(
            [
                {
                    "is_allowed": "not-a-bool",
                    "retry_after_seconds": 0,
                    "detail_message": "OK",
                }
            ],
            id="bad-bool",
        ),
        pytest.param(
            [
                {
                    "is_allowed": True,
                    "retry_after_seconds": -1,
                    "detail_message": "OK",
                }
            ],
            id="negative-retry",
        ),
        pytest.param(
            [
                {
                    "is_allowed": True,
                    "retry_after_seconds": "not-an-int",
                    "detail_message": "OK",
                }
            ],
            id="non-int-retry",
        ),
        pytest.param(
            [
                {
                    "is_allowed": True,
                    "retry_after_seconds": 0,
                    "detail_message": None,
                }
            ],
            id="null-message",
        ),
        pytest.param(
            [
                {
                    "is_allowed": True,
                    "retry_after_seconds": 0,
                    "detail_message": "OK",
                },
                {
                    "is_allowed": True,
                    "retry_after_seconds": 0,
                    "detail_message": "OK",
                },
            ],
            id="multiple-rows",
        ),
        pytest.param(
            [
                {
                    "is_allowed": True,
                    "retry_after_seconds": True,
                    "detail_message": "OK",
                }
            ],
            id="bool-as-retry-int",
        ),
    ],
)
def test_validate_rate_limit_response_malformed_raises_503(bad_data):
    with pytest.raises(HTTPException) as exc_info:
        validate_rate_limit_response(bad_data)

    assert exc_info.value.status_code == 503
    assert "temporarily unavailable" in (
        exc_info.value.detail.lower()
    )


# ---------------------------------------------------------
# Supabase RPC service tests
# ---------------------------------------------------------

def test_supabase_rpc_rate_limiter_integration():
    mock_supabase = MagicMock()
    mock_rpc = MagicMock()

    mock_rpc.execute.return_value.data = [
        {
            "is_allowed": True,
            "retry_after_seconds": 0,
            "detail_message": "OK",
        }
    ]

    mock_supabase.rpc.return_value = mock_rpc

    allowed, retry_after, message = check_and_record_otp_request(
        "rpc.test@example.com",
        "1.2.3.4",
        mock_supabase,
    )

    assert allowed is True
    assert retry_after == 0
    assert message == "OK"

    mock_supabase.rpc.assert_called_once_with(
        "check_and_record_otp_rate_limit",
        {
            "p_email": "rpc.test@example.com",
            "p_client_ip": "1.2.3.4",
            "p_max_daily": 10,
        },
    )


def test_database_failure_fails_closed():
    mock_supabase = MagicMock()
    mock_rpc = MagicMock()

    mock_rpc.execute.side_effect = APIError(
        {
            "message": "DB connection lost",
            "code": "500",
        }
    )

    mock_supabase.rpc.return_value = mock_rpc

    with pytest.raises(HTTPException) as exc_info:
        check_and_record_otp_request(
            "fail.closed@example.com",
            "1.2.3.4",
            mock_supabase,
        )

    assert exc_info.value.status_code == 503


def test_unexpected_rpc_exception_fails_closed():
    mock_supabase = MagicMock()
    mock_rpc = MagicMock()

    mock_rpc.execute.side_effect = RuntimeError(
        "Unexpected database failure"
    )

    mock_supabase.rpc.return_value = mock_rpc

    with pytest.raises(HTTPException) as exc_info:
        check_and_record_otp_request(
            "unexpected@example.com",
            "1.2.3.4",
            mock_supabase,
        )

    assert exc_info.value.status_code == 503


# ---------------------------------------------------------
# Missing Supabase and test fallback tests
# ---------------------------------------------------------

def test_missing_supabase_client_in_production_fails_closed():
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(HTTPException) as exc_info:
            check_and_record_otp_request(
                "prod.test@example.com",
                "1.2.3.4",
                supabase=None,
                allow_test_memory_fallback=False,
            )

    assert exc_info.value.status_code == 503


def test_testing_env_var_does_not_enable_memory_fallback_without_flag():
    with patch.dict(
        os.environ,
        {"TESTING": "1"},
        clear=True,
    ):
        with pytest.raises(HTTPException) as exc_info:
            check_and_record_otp_request(
                "prod.test@example.com",
                "1.2.3.4",
                supabase=None,
                allow_test_memory_fallback=False,
            )

    assert exc_info.value.status_code == 503


def test_explicit_flag_permits_memory_fallback():
    clear_rate_limit_store()

    allowed, retry_after, message = check_and_record_otp_request(
        "memory.test@example.com",
        "1.2.3.4",
        supabase=None,
        allow_test_memory_fallback=True,
    )

    assert allowed is True
    assert retry_after == 0
    assert message == "OK"


def test_rpc_failure_never_falls_back_to_memory():
    mock_supabase = MagicMock()
    mock_rpc = MagicMock()

    mock_rpc.execute.side_effect = RuntimeError(
        "Database crash"
    )

    mock_supabase.rpc.return_value = mock_rpc

    with pytest.raises(HTTPException) as exc_info:
        check_and_record_otp_request(
            "crash@example.com",
            "1.2.3.4",
            mock_supabase,
            allow_test_memory_fallback=True,
        )

    assert exc_info.value.status_code == 503


# ---------------------------------------------------------
# In-memory test fallback behavior
# ---------------------------------------------------------

def test_progressive_cooldown_enforcement_with_memory_flag():
    clear_rate_limit_store()

    email = "rate.test@example.com"
    ip = "192.168.1.50"

    allowed, retry_after, _ = check_and_record_otp_request(
        email,
        ip,
        allow_test_memory_fallback=True,
    )

    assert allowed is True
    assert retry_after == 0

    allowed, retry_after, message = check_and_record_otp_request(
        email,
        ip,
        allow_test_memory_fallback=True,
    )

    assert allowed is False
    assert 1 <= retry_after <= 60
    assert "wait" in message.lower()


# ---------------------------------------------------------
# Resend OTP endpoint tests
# ---------------------------------------------------------

def test_resend_otp_returns_429_and_never_calls_provider():
    clear_rate_limit_store()

    mock_supabase = MagicMock()
    mock_supabase.auth.resend.return_value = None

    call_count = [0]

    def mock_rpc_func(name, params):
        call_count[0] += 1

        mock_exec = MagicMock()

        if call_count[0] == 1:
            mock_exec.execute.return_value.data = [
                {
                    "is_allowed": True,
                    "retry_after_seconds": 0,
                    "detail_message": "OK",
                }
            ]
        else:
            mock_exec.execute.return_value.data = [
                {
                    "is_allowed": False,
                    "retry_after_seconds": 60,
                    "detail_message": (
                        "Too many requests. "
                        "Please wait 60 seconds before retrying."
                    ),
                }
            ]

        return mock_exec

    mock_supabase.rpc.side_effect = mock_rpc_func

    previous = _set_supabase_override(mock_supabase)

    try:
        payload = {
            "email": "resend.limit@example.com"
        }

        first_response = client.post(
            "/auth/resend-otp",
            json=payload,
        )

        assert first_response.status_code == 200
        mock_supabase.auth.resend.assert_called_once()

        mock_supabase.auth.resend.reset_mock()

        second_response = client.post(
            "/auth/resend-otp",
            json=payload,
        )

        assert second_response.status_code == 429
        assert "retry-after" in second_response.headers
        assert int(second_response.headers["retry-after"]) == 60

        assert "too many requests" in (
            second_response.json()["detail"].lower()
        )

        mock_supabase.auth.resend.assert_not_called()

    finally:
        _restore_supabase_override(previous)


def test_resend_otp_rpc_failure_returns_503_without_provider_call():
    mock_supabase = MagicMock()
    mock_rpc = MagicMock()

    mock_rpc.execute.side_effect = RuntimeError(
        "Database unavailable"
    )

    mock_supabase.rpc.return_value = mock_rpc

    previous = _set_supabase_override(mock_supabase)

    try:
        response = client.post(
            "/auth/resend-otp",
            json={
                "email": "failure.test@example.com"
            },
        )

        assert response.status_code == 503

        mock_supabase.auth.resend.assert_not_called()

    finally:
        _restore_supabase_override(previous)


# ---------------------------------------------------------
# Live integration test
# ---------------------------------------------------------

@pytest.mark.skipif(
    not os.getenv("RUN_LIVE_SUPABASE_INTEGRATION_TESTS"),
    reason="Live Supabase PostgreSQL environment not configured locally",
)
def test_live_supabase_concurrency_and_rpc_permissions():
    """
    Live integration test against a real isolated Supabase/PostgreSQL
    environment. This test must not run against production accidentally.
    """
    pytest.skip(
        "Implement only after an isolated test database is configured."
    )
