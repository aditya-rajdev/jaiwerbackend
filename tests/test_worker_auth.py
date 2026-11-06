import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from app.main import app
from app.core.supabase import get_supabase_client

from app.services.rate_limiter import clear_rate_limit_store

client = TestClient(app)


@pytest.fixture(autouse=True)
def auto_clear_rate_limits():
    clear_rate_limit_store()
    yield
    clear_rate_limit_store()


def test_worker_registration_success():
    mock_supabase = MagicMock()
    # Simulate pre-flight check: no existing profile for email or phone
    mock_supabase.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = None
    mock_supabase.auth.sign_in_with_otp.return_value = None

    app.dependency_overrides[get_supabase_client] = lambda: mock_supabase

    payload = {
        "name": "Ramesh Worker",
        "email": "worker.ramesh@example.com",
        "phone_number": "+919876543299",
        "account_type": "worker"
    }

    response = client.post("/auth/register", json=payload)
    app.dependency_overrides.clear()

    assert response.status_code == 202
    assert "message" in response.json()
    # Ensure sign_in_with_otp was called with account_type = 'worker'
    mock_supabase.auth.sign_in_with_otp.assert_called_once()
    call_args = mock_supabase.auth.sign_in_with_otp.call_args[0][0]
    assert call_args["options"]["data"]["account_type"] == "worker"


def test_worker_duplicate_email_registration_rejected():
    mock_supabase = MagicMock()
    # Simulate existing profile found for email
    mock_supabase.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = {"user_id": "existing-uuid"}

    app.dependency_overrides[get_supabase_client] = lambda: mock_supabase

    payload = {
        "name": "Ramesh Duplicate",
        "email": "existing.worker@example.com",
        "phone_number": "+919876543298",
        "account_type": "worker"
    }

    response = client.post("/auth/register", json=payload)
    app.dependency_overrides.clear()

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"].lower()


def test_worker_otp_verification_success():
    mock_supabase = MagicMock()
    # Mock successful Supabase OTP verification
    mock_user = MagicMock()
    mock_user.id = "worker-uuid-123"
    mock_user.user_metadata = {
        "name": "Ramesh Worker",
        "phone_number": "+919876543299",
        "account_type": "worker"
    }
    mock_session = MagicMock()
    mock_session.access_token = "mock_access_token_worker"
    mock_session.refresh_token = "mock_refresh_token_worker"
    mock_session.token_type = "bearer"
    mock_session.expires_in = 3600
    mock_session.expires_at = 1700000000

    mock_response = MagicMock()
    mock_response.user = mock_user
    mock_response.session = mock_session

    mock_supabase.auth.verify_otp.return_value = mock_response

    # Mock user_profiles database table upsert result
    profile_row = {
        "user_id": "worker-uuid-123",
        "email": "worker.ramesh@example.com",
        "name": "Ramesh Worker",
        "phone_number": "+919876543299",
        "account_type": "worker",
        "owner_reference_id": None
    }
    mock_supabase.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = profile_row
    mock_supabase.table.return_value.upsert.return_value.execute.return_value.data = [profile_row]

    app.dependency_overrides[get_supabase_client] = lambda: mock_supabase

    payload = {
        "email": "worker.ramesh@example.com",
        "token": "123456"
    }

    response = client.post("/auth/verify-otp", json=payload)
    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == "worker-uuid-123"
    assert data["access_token"] == "mock_access_token_worker"
    assert data["profile"]["account_type"] == "worker"
    assert data["profile"]["owner_reference_id"] is None


def test_worker_login_request_success():
    mock_supabase = MagicMock()
    mock_supabase.auth.sign_in_with_otp.return_value = None

    app.dependency_overrides[get_supabase_client] = lambda: mock_supabase

    payload = {
        "email": "worker.ramesh@example.com"
    }

    response = client.post("/auth/login", json=payload)
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "message" in response.json()
    mock_supabase.auth.sign_in_with_otp.assert_called_once_with({
        "email": "worker.ramesh@example.com",
        "options": {"should_create_user": False}
    })


def test_resend_otp_success():
    mock_supabase = MagicMock()
    mock_supabase.auth.resend.return_value = None

    app.dependency_overrides[get_supabase_client] = lambda: mock_supabase

    payload = {
        "email": "worker.ramesh@example.com"
    }

    response = client.post("/auth/resend-otp", json=payload)
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "message" in response.json()
    assert response.json()["message"] == "If an account exists for this address, a new verification code has been sent."
    mock_supabase.auth.resend.assert_called_once_with({
        "type": "signup",
        "email": "worker.ramesh@example.com"
    })
    # Ensure sign_in_with_otp is NOT called (no fallback bypass)
    mock_supabase.auth.sign_in_with_otp.assert_not_called()


def test_resend_otp_rate_limit_preserves_429_and_no_fallback():
    from supabase_auth.errors import AuthApiError

    mock_supabase = MagicMock()
    err = AuthApiError("Rate limit exceeded", 429, "over_email_send_rate_limit")
    mock_supabase.auth.resend.side_effect = err

    app.dependency_overrides[get_supabase_client] = lambda: mock_supabase

    payload = {
        "email": "worker.ramesh@example.com"
    }

    response = client.post("/auth/resend-otp", json=payload)
    app.dependency_overrides.clear()

    assert response.status_code == 429
    assert "too many requests" in response.json()["detail"].lower()
    # Ensure fallback sign_in_with_otp was NEVER called to bypass rate limit
    mock_supabase.auth.sign_in_with_otp.assert_not_called()


def test_resend_otp_server_error_returns_503():
    from supabase_auth.errors import AuthApiError

    mock_supabase = MagicMock()
    err = AuthApiError("Supabase server unavailable", 503, "unexpected_failure")
    mock_supabase.auth.resend.side_effect = err

    app.dependency_overrides[get_supabase_client] = lambda: mock_supabase

    payload = {
        "email": "worker.ramesh@example.com"
    }

    response = client.post("/auth/resend-otp", json=payload)
    app.dependency_overrides.clear()

    assert response.status_code == 503
    assert "temporarily unavailable" in response.json()["detail"].lower()


def test_resend_otp_generic_response_hides_account_existence():
    from supabase_auth.errors import AuthApiError

    mock_supabase = MagicMock()
    err = AuthApiError("User not found or unconfirmed", 400, "user_not_found")
    mock_supabase.auth.resend.side_effect = err

    app.dependency_overrides[get_supabase_client] = lambda: mock_supabase

    payload = {
        "email": "unregistered@example.com"
    }

    response = client.post("/auth/resend-otp", json=payload)
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["message"] == "If an account exists for this address, a new verification code has been sent."


def test_resend_otp_invalid_email_returns_422():
    payload = {
        "email": "invalid-email-format"
    }

    response = client.post("/auth/resend-otp", json=payload)
    assert response.status_code == 422
