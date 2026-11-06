from fastapi import APIRouter, Depends, HTTPException, status
from gotrue.types import User
from supabase import Client
from datetime import date
from uuid import UUID
from app.services.customer_queries import (
    get_owner_customers_summary,
    get_customer_items_for_owner,
)
from app.services.customers import get_customer_details
from app.core.security import get_current_user
from app.core.supabase import get_supabase_client
from fastapi.responses import Response

from app.services.receipt_generator import generate_payment_receipt
from app.services.shop_profile import (
    check_receipt_profile_completion,
    get_shop_profile,
)
from app.schemas.customers import (
    AddCustomerRequest,
    AddCustomerResponse,
    ExistingCustomerRequest,
    ExistingCustomerResponse,
    CustomerItemResponse,
    CustomerListResponse,
    CustomerDetailsResponse,
    TransactionListResponse,
    TransactionDetailResponse
)
from typing import Literal
from fastapi import Query
from app.services.shop_access import get_accessible_shop_owner_id
from app.services.customers import (
    create_customer_with_item,
    create_item_for_existing_customer,
)
from app.services.customer_queries import get_owner_customers_summary, get_owner_transactions, get_owner_transaction_details
from app.schemas.customers import CustomerPaymentRequest, CustomerPaymentResponse

from app.services.customers import record_customer_payment

router = APIRouter(prefix="", tags=["Customers"])


@router.post(
    "/add-customer",
    response_model=AddCustomerResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_customer(
    payload: AddCustomerRequest,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    shop_owner_id = get_accessible_shop_owner_id(
        user_id=str(user.id),
        supabase=supabase,
    )

    return create_customer_with_item(
        payload=payload,
        owner_user_id=shop_owner_id,
        supabase=supabase,
    )


@router.post(
    "/existing-customer",
    response_model=ExistingCustomerResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_item_for_existing_customer(
    payload: ExistingCustomerRequest,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    shop_owner_id = get_accessible_shop_owner_id(
        user_id=str(user.id),
        supabase=supabase,
    )

    return create_item_for_existing_customer(
        payload=payload,
        owner_user_id=shop_owner_id,
        supabase=supabase,
    )


@router.get(
    "/customers",
    response_model=CustomerListResponse,
)
def list_customers(
    filter: Literal["all", "jama", "baki"] = "all",
    search: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    shop_owner_id = get_accessible_shop_owner_id(
        user_id=str(user.id),
        supabase=supabase,
    )

    return get_owner_customers_summary(
        owner_user_id=shop_owner_id,
        supabase=supabase,
        filter=filter,
        search=search,
        limit=limit,
        offset=offset,
    )

@router.get(
    "/customers/{customer_id}/items",
    response_model=list[CustomerItemResponse],
)
def get_customer_items(
    customer_id: UUID,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    shop_owner_id = get_accessible_shop_owner_id(
        user_id=str(user.id),
        supabase=supabase,
    )

    return get_customer_items_for_owner(
        customer_id=str(customer_id),
        owner_user_id=shop_owner_id,
        supabase=supabase,
    )


@router.post(
    "/customers/{customer_id}/payments",
    response_model=CustomerPaymentResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_customer_payment(
    customer_id: UUID,
    payload: CustomerPaymentRequest,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    shop_owner_id = get_accessible_shop_owner_id(
        user_id=str(user.id),
        supabase=supabase,
    )

    return record_customer_payment(
        customer_id=customer_id,
        payload=payload,
        owner_user_id=shop_owner_id,
        supabase=supabase,
    )

@router.get(
    "/customers/{customer_id}",
    response_model=CustomerDetailsResponse,
)
def get_customer_details_endpoint(
    customer_id: UUID,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    shop_owner_id = get_accessible_shop_owner_id(
        user_id=str(user.id),
        supabase=supabase,
    )

    return get_customer_details(
        customer_id=customer_id,
        owner_user_id=shop_owner_id,
        supabase=supabase,
    )

@router.get(
    "/transactions",
    response_model=TransactionListResponse,
)
def list_transactions(
    search: str | None = None,
    payment_method: Literal[
        "cash", "upi", "card", "bank_transfer"
    ] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    shop_owner_id = get_accessible_shop_owner_id(
        user_id=str(user.id),
        supabase=supabase,
    )

    return get_owner_transactions(
        owner_user_id=shop_owner_id,
        supabase=supabase,
        search=search,
        payment_method=payment_method,
        start_date=start_date.isoformat() if start_date else None,
        end_date=end_date.isoformat() if end_date else None,
        limit=limit,
        offset=offset,
    )

@router.get(
    "/transactions/{transaction_id}",
    response_model=TransactionDetailResponse,
)
def get_transaction_details(
    transaction_id: UUID,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    shop_owner_id = get_accessible_shop_owner_id(
        user_id=str(user.id),
        supabase=supabase,
    )

    return get_owner_transaction_details(
        transaction_id=transaction_id,
        owner_user_id=shop_owner_id,
        supabase=supabase,
    )

@router.get("/transactions/{transaction_id}/receipt")
def download_payment_receipt(
    transaction_id: UUID,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
):
    # 1. Resolve the shop owner.
    owner_user_id = get_accessible_shop_owner_id(
        user_id=str(user.id),
        supabase=supabase,
    )

    # 2. Verify receipt profile completion.
    completion = check_receipt_profile_completion(
        owner_user_id=owner_user_id,
        supabase=supabase,
    )

    if not completion["is_complete"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PROFILE_INCOMPLETE",
                "message": "Complete your shop profile to generate receipts.",
                "missing_fields": completion["missing_fields"],
            },
        )

    # 3. Fetch transaction details.
    transaction_data = get_owner_transaction_details(
        transaction_id=transaction_id,
        owner_user_id=owner_user_id,
        supabase=supabase,
    )

    # 4. Fetch shop profile.
    shop_profile = get_shop_profile(
        owner_user_id=owner_user_id,
        supabase=supabase,
    )

    # 5. Generate PDF.
    pdf_bytes = generate_payment_receipt(
        transaction_data=transaction_data,
        shop_profile=shop_profile,
    )

    # 6. Return downloadable PDF.
    receipt_number = transaction_data["transaction"]["receipt_number"]

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{receipt_number}.pdf"'
            )
        },
    )