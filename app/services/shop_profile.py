from fastapi import HTTPException, status
from postgrest.exceptions import APIError
from supabase import Client

from app.schemas.shop_profile import (
    ShopProfileUpdateRequest,
)

RECEIPT_REQUIRED_FIELDS = [
    "name",
    "phone_number",
    "shop_name",
    "shop_address",
    "city",
    "state",
    "pincode",
]


def update_shop_profile(
    owner_user_id: str,
    payload: ShopProfileUpdateRequest,
    supabase: Client,
) -> dict:
    update_data = payload.model_dump(exclude_unset=True)

    try:
        result = (
            supabase.table("user_profiles")
            .update(update_data)
            .eq("user_id", owner_user_id)
            .select(
                """
                shop_name,
                shop_address,
                city,
                state,
                pincode,
                gstin,
                shop_logo_url,
                receipt_footer
                """
            )
            .execute()
        )

        if not result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Owner profile not found.",
            )

        return result.data[0]

    except APIError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to update shop profile.",
        ) from None

def get_shop_profile(
    owner_user_id: str,
    supabase: Client,
) -> dict:
    try:
        result = (
            supabase.table("user_profiles")
            .select(
                """
                name,
                phone_number,
                email,
                shop_name,
                shop_address,
                city,
                state,
                pincode,
                gstin,
                shop_logo_url,
                receipt_footer
                """
            )
            .eq("user_id", owner_user_id)
            .maybe_single()
            .execute()
        )

        if not result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Owner profile not found.",
            )

        return result.data

    except APIError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to retrieve shop profile.",
        ) from None


def check_receipt_profile_completion(
    owner_user_id: str,
    supabase: Client,
) -> dict:
    try:
        result = (
            supabase.table("user_profiles")
            .select(",".join(RECEIPT_REQUIRED_FIELDS))
            .eq("user_id", owner_user_id)
            .maybe_single()
            .execute()
        )

        profile = result.data

        if not profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Owner profile not found.",
            )

        missing_fields = [
            field
            for field in RECEIPT_REQUIRED_FIELDS
            if not isinstance(profile.get(field), str)
            or not profile[field].strip()
        ]

        return {
            "is_complete": len(missing_fields) == 0,
            "missing_fields": missing_fields,
        }

    except APIError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to check receipt profile completion.",
        ) from None