import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


# ============================================================================
# Request Models
# ============================================================================


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    phone_number: str
    account_type: Literal["owner", "worker"] = "owner"

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, value: str) -> str:
        if not re.fullmatch(r"\+[1-9]\d{7,14}", value):
            raise ValueError(
                "Phone number must use international E.164 format, e.g. +919876543210"
            )
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr


class ResendOTPRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr


class VerifyOTPRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr
    token: str = Field(min_length=6, max_length=10)

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        if not re.fullmatch(r"\d{6,10}", value):
            raise ValueError("OTP must contain between 6 and 10 digits")
        return value


# ============================================================================
# Response Models
# ============================================================================


class MessageResponse(BaseModel):
    message: str


class ProfileResponse(BaseModel):
    user_id: str
    email: EmailStr
    name: str
    phone_number: str
    account_type: str
    owner_reference_id: str | None = None


class VerifyOTPResponse(BaseModel):
    message: str
    user_id: str
    profile: ProfileResponse
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int | None = None
    expires_at: int | None = None


class UserMeResponse(BaseModel):
    user_id: str
    email: EmailStr
    email_verified: bool
    account_type: str | None = None
    name: str | None = None
    phone_number: str | None = None
    profile: ProfileResponse | None = None

class RefreshSessionRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class RefreshSessionResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int | None = None
    expires_at: int | None = None