import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from gotrue.types import User
from postgrest.exceptions import APIError
from supabase import Client
from app.core.security import get_current_user
from app.core.supabase import get_supabase_client
from app.schemas.memberships import AddWorkerRequest, AddWorkerResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/owner", tags=["Owner"])


@router.post(
    "/workers",
    response_model=AddWorkerResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_worker(
    payload: AddWorkerRequest,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    owner_user_id = str(user.id)
    phone_number = payload.phone_number.strip()

    # 1. Confirm that the authenticated user is an owner.
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
            detail="Unable to process the request right now.",
        ) from None

    if not owner_result.data or owner_result.data["account_type"] != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owner accounts can add workers.",
        )

    # 2. Find the registered worker by phone number.
    try:
        worker_result = (
            supabase.table("user_profiles")
            .select("user_id, account_type")
            .eq("phone_number", phone_number)
            .maybe_single()
            .execute()
        )
    except APIError:
        logger.exception("Failed to retrieve worker profile.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to process the request right now.",
        ) from None

    if not worker_result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No registered account found for this mobile number.",
        )

    if worker_result.data["account_type"] != "worker":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This mobile number does not belong to a worker account.",
        )

    worker_user_id = worker_result.data["user_id"]

    if worker_user_id == owner_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot add yourself as a worker.",
        )

    # 3. Create an approved membership.
    try:
        result = (
            supabase.table("shop_memberships")
            .insert(
                {
                    "owner_user_id": owner_user_id,
                    "worker_user_id": worker_user_id,
                    "status": "approved",
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .execute()
        )

        if not result.data:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unable to confirm worker addition.",
            )

        return result.data[0]

    except APIError as exc:
        if getattr(exc, "code", None) == "23505":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This worker has already been added.",
            ) from None

        logger.exception("Failed to add worker.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to add worker right now.",
        ) from None
