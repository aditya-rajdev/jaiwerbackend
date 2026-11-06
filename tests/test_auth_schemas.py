import pytest
from pydantic import ValidationError
from app.schemas.auth import RegistrationRequest, LoginRequest, VerifyOTPRequest

def test_valid_registration_request():
    payload = RegistrationRequest(
        name="Aditya Owner",
        email="owner@example.com",
        phone_number="+919876543210",
        account_type="owner"
    )
    assert payload.name == "Aditya Owner"
    assert payload.phone_number == "+919876543210"

def test_invalid_phone_number_format():
    with pytest.raises(ValidationError):
        RegistrationRequest(
            name="Invalid Phone",
            email="owner@example.com",
            phone_number="9876543210",  # Missing + and country code
            account_type="owner"
        )

def test_valid_otp_request():
    payload = VerifyOTPRequest(
        email="user@example.com",
        token="123456"
    )
    assert payload.token == "123456"

def test_invalid_otp_token_format():
    with pytest.raises(ValidationError):
        VerifyOTPRequest(
            email="user@example.com",
            token="abc12"  # Non-digit / invalid length
        )
