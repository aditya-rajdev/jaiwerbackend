from fastapi import HTTPException, status
from postgrest.exceptions import APIError
from supabase import Client


def get_accessible_shop_owner_id(
    user_id: str,
    supabase: Client,
) -> str:
    try:
        profile_result = (
            supabase.table("user_profiles")
            .select("user_id, account_type")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )

        profile = profile_result.data

        if not profile:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User profile not found.",
            )

        if profile["account_type"] == "owner":
            return user_id

        if profile["account_type"] != "worker":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account type is not authorized.",
            )

        membership_result = (
            supabase.table("shop_memberships")
            .select("owner_user_id")
            .eq("worker_user_id", user_id)
            .eq("status", "approved")
            .execute()
        )

        memberships = membership_result.data or []

        if not memberships:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have an approved shop membership.",
            )

        if len(memberships) > 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "You belong to multiple shops. "
                    "Select a shop before continuing."
                ),
            )

        return memberships[0]["owner_user_id"]

    except APIError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to verify shop access.",
        ) from None