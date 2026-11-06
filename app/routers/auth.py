import logging
from fastapi import APIRouter, Depends, HTTPException, status
from gotrue.types import User
from postgrest.exceptions import APIError
from supabase import Client
from supabase_auth.errors import AuthApiError

from app.core.security import get_current_user
from app.core.supabase import get_supabase_client
from app.schemas.auth import (
    LoginRequest,
    MessageResponse,
    RegistrationRequest,
    UserMeResponse,
    VerifyOTPRequest,
    VerifyOTPResponse,
    RefreshSessionRequest,
    RefreshSessionResponse,
)
from app.services.profiles import create_user_profile

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"])


def handle_auth_provider_error(exc: AuthApiError, operation: str) -> None:
    """Convert Supabase Auth errors into safe, controlled API responses."""
    provider_status = getattr(exc, "status", None)

    if provider_status == 429:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please wait before retrying.",
        ) from None

    if isinstance(provider_status, int) and provider_status >= 500:
        logger.error("Supabase Auth unavailable during %s.", operation)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is temporarily unavailable. Please try again.",
        ) from None

    logger.warning(
        "Supabase Auth rejected or failed operation=%s status=%s",
        operation,
        provider_status,
    )


@router.post(
    "/register",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def register(
    payload: RegistrationRequest,
    supabase: Client = Depends(get_supabase_client),
):
    target_email = str(payload.email).strip().lower()

    # 1. Fail-closed pre-flight check
    try:
        existing = (
            supabase.table("user_profiles")
            .select("user_id")
            .eq("email", target_email)
            .maybe_single()
            .execute()
        )
    except APIError:
        logger.exception("Database check failed during registration.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Registration is temporarily unavailable. Please try again.",
        ) from None

    if existing and existing.data:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists. Please log in.",
        )

    # 2. Dispatch sign-up OTP
    try:
        supabase.auth.sign_in_with_otp({
            "email": target_email,
            "options": {
                "should_create_user": True,
                "data": {
                    "name": payload.name.strip(),
                    "phone_number": payload.phone_number,
                    "account_type": payload.account_type,
                },
            },
        })
        return MessageResponse(
            message="If the address can be registered, a verification email will be sent."
        )
    except AuthApiError as exc:
        handle_auth_provider_error(exc, "registration")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to initiate registration.",
        ) from None
    except Exception:
        logger.exception("Unexpected failure during registration.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Registration is temporarily unavailable. Please try again.",
        ) from None


@router.post(
    "/verify-otp",
    response_model=VerifyOTPResponse,
    status_code=status.HTTP_200_OK,
)
def verify_otp(
    payload: VerifyOTPRequest,
    supabase: Client = Depends(get_supabase_client),
):
    target_email = str(payload.email).strip().lower()

    try:
        response = supabase.auth.verify_otp({
            "email": target_email,
            "token": payload.token,
            "type": "email",
        })
    except AuthApiError as exc:
        handle_auth_provider_error(exc, "OTP verification")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired verification code.",
        ) from None
    except Exception:
        logger.exception("Unexpected failure during OTP verification.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Verification is temporarily unavailable. Please try again.",
        ) from None

    if not response.user or not response.session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed. Please request a new code.",
        )

    user_metadata = response.user.user_metadata or {}
    persisted_profile = create_user_profile(
        user_id=str(response.user.id),
        email=target_email,
        metadata=user_metadata,
        supabase=supabase,
    )

    return VerifyOTPResponse(
        message="Email verified successfully.",
        user_id=str(response.user.id),
        profile=persisted_profile,
        access_token=response.session.access_token,
        refresh_token=response.session.refresh_token,
        token_type=response.session.token_type,
        expires_in=getattr(response.session, "expires_in", None),
        expires_at=getattr(response.session, "expires_at", None),
    )


@router.post(
    "/login",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
def login(
    payload: LoginRequest,
    supabase: Client = Depends(get_supabase_client),
):
    try:
        supabase.auth.sign_in_with_otp({
            "email": str(payload.email).strip().lower(),
            "options": {
                "should_create_user": False,
            },
        })
    except AuthApiError as exc:
        handle_auth_provider_error(exc, "login")
        # Standard provider rejections fall through to the generic response below
    except Exception:
        logger.exception("Unexpected failure during login.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Login is temporarily unavailable. Please try again.",
        ) from None

    return MessageResponse(
        message="If an account exists for this address, a login code has been sent."
    )


@router.get(
    "/me",
    response_model=UserMeResponse,
    status_code=status.HTTP_200_OK,
)
def get_me(user: User = Depends(get_current_user)):
    return UserMeResponse(
        user_id=str(user.id),
        email=user.email,
        email_verified=getattr(user, "email_confirmed_at", None) is not None,
    )

@router.post(
    "/refresh",
    response_model=RefreshSessionResponse,
    status_code=status.HTTP_200_OK,
)
def refresh_session(
    payload: RefreshSessionRequest,
    supabase: Client = Depends(get_supabase_client),
):
    try:
        response = supabase.auth.refresh_session(
            payload.refresh_token
        )

        if not response.session:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session refresh failed. Please log in again.",
            )

        session = response.session

        return RefreshSessionResponse(
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            token_type=session.token_type,
            expires_in=getattr(session, "expires_in", None),
            expires_at=getattr(session, "expires_at", None),
        )

    except AuthApiError as exc:
        handle_auth_provider_error(exc, "session refresh")

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token is invalid or expired. Please log in again.",
        ) from None

    except HTTPException:
        raise

    except Exception:
        logger.exception("Unexpected failure during session refresh.")

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to refresh session right now. Please try again.",
        ) from None