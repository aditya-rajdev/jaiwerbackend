import logging
import re
from typing import Any
from fastapi import HTTPException, status
from postgrest.exceptions import APIError
from supabase import Client

logger = logging.getLogger(__name__)


def create_user_profile(
    user_id: str,
    email: str,
    metadata: dict[str, Any],
    supabase: Client,
) -> dict[str, Any]:
    """Idempotently fetch or create a user profile with role-aware owner reference IDs."""
    clean_email = email.strip().lower()

    # 1. Return existing profile untouched if already present
    try:
        existing = (
            supabase.table("user_profiles")
            .select("user_id, email, name, phone_number, account_type, owner_reference_id")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        if existing and existing.data:
            return existing.data
    except APIError:
        logger.exception("Failed to query existing user_profile.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Profile service is temporarily unavailable. Please try again.",
        ) from None

    # 2. Validate metadata fields
    name = metadata.get("name")
    phone_number = metadata.get("phone_number")
    account_type = metadata.get("account_type", "owner")

    if not isinstance(name, str) or not 2 <= len(name.strip()) <= 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration name is missing or invalid.",
        )

    if not isinstance(phone_number, str) or not re.fullmatch(r"^\+[1-9]\d{7,14}$", phone_number):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration phone number must use E.164 format.",
        )

    if account_type not in {"owner", "worker"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid account type.",
        )

    clean_name = name.strip()

    profile_data: dict[str, Any] = {
        "user_id": user_id,
        "email": clean_email,
        "name": clean_name,
        "phone_number": phone_number,
        "account_type": account_type,
    }

    # Workers must not hold a reference ID; owners rely on the database UUID default
    if account_type == "worker":
        profile_data["owner_reference_id"] = None

    # 3. Upsert row into user_profiles
    try:
        result = (
            supabase.table("user_profiles")
            .upsert(
                profile_data,
                on_conflict="user_id",
                ignore_duplicates=True,
            )
            .execute()
        )

        if result.data:
            return result.data[0]

        # In case of concurrent insert resolution, fetch the winning row
        existing = (
            supabase.table("user_profiles")
            .select("user_id, email, name, phone_number, account_type, owner_reference_id")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        if existing and existing.data:
            return existing.data

        logger.error("Profile write returned no row for user_id=%s", user_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to confirm profile creation. Please retry.",
        )

    except APIError as exc:
        if getattr(exc, "code", None) == "23505":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A profile with these unique details already exists.",
            ) from None

        logger.exception("Failed to save user profile.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to save your profile. Please try again.",
        ) from None


def get_user_profile_by_id(
    user_id: str,
    supabase: Client,
) -> dict[str, Any] | None:
    try:
        response = (
            supabase.table("user_profiles")
            .select(
                "user_id, email, name, phone_number, account_type, owner_reference_id"
            )
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        return response.data
    except APIError as exc:
        logger.error("Failed to fetch user profile for user_id=%s: %s", user_id, exc.message)
        return None
