from fastapi import APIRouter, Depends, HTTPException, status
from gotrue.types import User
from postgrest.exceptions import APIError
from supabase import Client

from app.core.security import get_current_user
from app.core.supabase import get_supabase_client

from app.schemas.shop_profile import (
    ShopProfileUpdateRequest,
    ShopProfileResponse,
)
from app.services.shop_profile import get_shop_profile
from app.services.shop_profile import update_shop_profile
from app.schemas.shop_profile import ShopProfileCompletionResponse
from app.services.shop_profile import check_receipt_profile_completion
from app.services.shop_access import get_accessible_shop_owner_id

import logging

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Shop Profile"])


@router.patch(
    "/shop-profile",
    response_model=ShopProfileResponse,
)
def update_shop_profile_endpoint(
    payload: ShopProfileUpdateRequest,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    owner_user_id = str(user.id)

    # 1. Verify that the authenticated user is an owner.
    try:
        owner_result = (
            supabase.table("user_profiles")
            .select("user_id, account_type")
            .eq("user_id", owner_user_id)
            .maybe_single()
            .execute()
        )

    except APIError:
        logger.exception("Failed to retrieve owner profile.")

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to verify owner account.",
        ) from None

    if (
        not owner_result.data
        or owner_result.data["account_type"] != "owner"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only shop owners can update shop details.",
        )

    # 2. Update the owner's shop profile.
    return update_shop_profile(
        owner_user_id=owner_user_id,
        payload=payload,
        supabase=supabase,
    )

@router.get(
    "/shop-profile",
    response_model=ShopProfileResponse,
)
def get_shop_profile_endpoint(
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    owner_user_id = str(user.id)

    # Verify owner account.
    try:
        owner_result = (
            supabase.table("user_profiles")
            .select("user_id, account_type")
            .eq("user_id", owner_user_id)
            .maybe_single()
            .execute()
        )

    except APIError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to verify owner account.",
        ) from None

    if (
        not owner_result.data
        or owner_result.data["account_type"] != "owner"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only shop owners can access profile management.",
        )

    return get_shop_profile(
        owner_user_id=owner_user_id,
        supabase=supabase,
    )

@router.get(
    "/shop-profile/completion",
    response_model=ShopProfileCompletionResponse,
)
def get_shop_profile_completion(
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    owner_user_id = get_accessible_shop_owner_id(
        user_id=str(user.id),
        supabase=supabase,
    )

    return check_receipt_profile_completion(
        owner_user_id=owner_user_id,
        supabase=supabase,
    )