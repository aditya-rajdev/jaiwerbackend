import logging
from datetime import datetime
from uuid import UUID
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    status,
)

from postgrest.exceptions import APIError

from app.core.security import get_current_user
from app.core.supabase import get_supabase_client

from app.schemas.supplier import (
    CreateSupplierRequest,
    AddSupplierItemsRequest,
    RecordSupplierPaymentRequest,
)

from app.services.supplier import (
    create_supplier,
    add_supplier_items,
    record_supplier_payment,
    get_suppliers,
    get_supplier_details,
    get_supplier_transactions,
    get_supplier_transaction_details,
)

from app.services.shop_access import get_accessible_shop_owner_id


logger = logging.getLogger(__name__)

router = APIRouter(tags=["Suppliers"])


# -----------------------------------------
# Shared helpers
# -----------------------------------------

def get_user_id(current_user) -> UUID:
    """
    Extract authenticated user ID.

    Adjust this if your get_current_user dependency
    returns a different structure.
    """

    if isinstance(current_user, dict):
        user_id = current_user.get("id")
    else:
        user_id = getattr(current_user, "id", None)

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )

    return UUID(str(user_id))


def resolve_shop_owner(current_user, supabase) -> UUID:
    """
    Resolve the shop owner associated with the authenticated user.
    Owners resolve to themselves; approved workers resolve to
    their associated shop owner.
    """

    user_id = get_user_id(current_user)

    try:
        return get_accessible_shop_owner_id(
            user_id=user_id,
            supabase=supabase,
        )

    except HTTPException:
        raise

    except Exception:
        logger.exception("Failed to resolve accessible shop")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to resolve shop access",
        )


def handle_supplier_error(error: Exception):
    """
    Translate known database errors into HTTP responses.
    Do not expose raw database errors to clients.
    """

    if isinstance(error, APIError):

        message = str(
            getattr(error, "message", None) or error
        ).lower()

        # Invalid payment, amount or allocation.
        if any(term in message for term in [
            "payment",
            "allocation",
            "outstanding balance",
            "exceed",
            "invalid",
            "positive",
            "required",
            "duplicate",
        ]):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid supplier operation or payment data",
            )

        if any(term in message for term in [
            "supplier not found",
            "supplier does not exist",
            "purchase items do not belong",
        ]):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Supplier or purchase records not found",
            )

        logger.exception("Supplier database operation failed")

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Supplier operation failed",
        )

    logger.exception("Unexpected supplier service error")

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An unexpected error occurred",
    )


# -----------------------------------------
# 1. Create supplier
# -----------------------------------------

@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
)
def create_supplier_endpoint(
    payload: CreateSupplierRequest,
    current_user=Depends(get_current_user),
    supabase=Depends(get_supabase_client),
):

    owner_user_id = resolve_shop_owner(
        current_user,
        supabase,
    )

    try:
        return create_supplier(
            owner_user_id=owner_user_id,
            payload=payload,
            supabase=supabase,
        )

    except Exception as error:
        handle_supplier_error(error)


# -----------------------------------------
# 2. Add purchases to existing supplier
# -----------------------------------------

@router.post("/existing")
def add_supplier_items_endpoint(
    payload: AddSupplierItemsRequest,
    current_user=Depends(get_current_user),
    supabase=Depends(get_supabase_client),
):

    owner_user_id = resolve_shop_owner(
        current_user,
        supabase,
    )

    try:
        return add_supplier_items(
            owner_user_id=owner_user_id,
            payload=payload,
            supabase=supabase,
        )

    except Exception as error:
        handle_supplier_error(error)


# -----------------------------------------
# 3. Record subsequent supplier payment
# -----------------------------------------

@router.post("/payment")
def record_supplier_payment_endpoint(
    payload: RecordSupplierPaymentRequest,
    current_user=Depends(get_current_user),
    supabase=Depends(get_supabase_client),
):

    owner_user_id = resolve_shop_owner(
        current_user,
        supabase,
    )

    try:
        return record_supplier_payment(
            owner_user_id=owner_user_id,
            payload=payload,
            supabase=supabase,
        )

    except Exception as error:
        handle_supplier_error(error)


# -----------------------------------------
# 4. List suppliers
# -----------------------------------------

@router.get("")
def get_suppliers_endpoint(
    filter: str = Query(
        default="all",
        pattern="^(all|baki|paid)$",
    ),
    search: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),

    current_user=Depends(get_current_user),
    supabase=Depends(get_supabase_client),
):

    owner_user_id = resolve_shop_owner(
        current_user,
        supabase,
    )

    try:
        return get_suppliers(
            owner_user_id=owner_user_id,
            filter=filter,
            search=search,
            limit=limit,
            offset=offset,
            supabase=supabase,
        )

    except Exception as error:
        handle_supplier_error(error)


# -----------------------------------------
# 5. Supplier details
# -----------------------------------------

@router.get("/{supplier_id}")
def get_supplier_details_endpoint(
    supplier_id: UUID,

    current_user=Depends(get_current_user),
    supabase=Depends(get_supabase_client),
):

    owner_user_id = resolve_shop_owner(
        current_user,
        supabase,
    )

    try:
        return get_supplier_details(
            owner_user_id=owner_user_id,
            supplier_id=supplier_id,
            supabase=supabase,
        )

    except Exception as error:
        handle_supplier_error(error)


# -----------------------------------------
# 6. Supplier transaction history
# -----------------------------------------

transactions_router = APIRouter(
    prefix="/supplier-transactions",
    tags=["Supplier Transactions"],
)


@transactions_router.get("")
def get_supplier_transactions_endpoint(
    supplier_id: UUID | None = Query(default=None),
    search: str | None = Query(default=None),
    start_date: datetime | None = Query(default=None),
    end_date: datetime | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),

    current_user=Depends(get_current_user),
    supabase=Depends(get_supabase_client),
):

    owner_user_id = resolve_shop_owner(
        current_user,
        supabase,
    )

    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=400,
            detail="Start date cannot be after end date",
        )

    try:
        return get_supplier_transactions(
            owner_user_id=owner_user_id,
            supplier_id=supplier_id,
            search=search,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
            supabase=supabase,
        )

    except Exception as error:
        handle_supplier_error(error)


# -----------------------------------------
# 7. Supplier transaction details
# -----------------------------------------

@transactions_router.get("/{transaction_id}")
def get_supplier_transaction_details_endpoint(
    transaction_id: UUID,

    current_user=Depends(get_current_user),
    supabase=Depends(get_supabase_client),
):

    owner_user_id = resolve_shop_owner(
        current_user,
        supabase,
    )

    try:
        return get_supplier_transaction_details(
            owner_user_id=owner_user_id,
            transaction_id=transaction_id,
            supabase=supabase,
        )

    except Exception as error:
        handle_supplier_error(error)