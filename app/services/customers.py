from fastapi import HTTPException, status
from postgrest.exceptions import APIError
from supabase import Client
from uuid import UUID
from decimal import Decimal
from app.schemas.customers import (
    CustomerPaymentRequest,
)
from app.schemas.customers import AddCustomerRequest, ExistingCustomerRequest


def create_customer_with_item(
    payload: AddCustomerRequest,
    owner_user_id: str,
    supabase: Client,
) -> dict:

    items = []

    for item in payload.items:
        item_data = {
            "item_name": item.item_name.strip(),
            "weight_grams": str(item.weight_grams),
            "rate_per_gram": (
                str(item.rate_per_gram)
                if item.rate_per_gram is not None
                else None
            ),
            "total_amount": str(item.total_amount),
            "reminder_date": (
                item.reminder_date.isoformat()
                if item.reminder_date is not None
                else None
            ),
            "sms_reminder_enabled": item.sms_reminder_enabled,
        }

        # Item-wise mode: frontend provides payment per item.
        if payload.payment_mode == "item_wise":
            item_data["amount_paid"] = str(item.amount_paid)

        items.append(item_data)

    try:
        result = supabase.rpc(
            "create_customer_with_items_and_payment",
            {
                "p_name": payload.name.strip(),
                "p_phone_number": payload.phone_number,
                "p_address": payload.address,
                "p_owner_user_id": owner_user_id,
                "p_payment_mode": payload.payment_mode,
                "p_payment_method": payload.payment_method,
                "p_total_payment": (
                    str(payload.total_payment)
                    if payload.total_payment is not None
                    else None
                ),
                "p_items": items,
            },
        ).execute()

        if not result.data:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Customer creation returned no data.",
            )

        return result.data

    except APIError as exc:
        error_code = getattr(exc, "code", None)

        if error_code == "23505":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A customer with this phone number already exists. "
                    "Use the existing-customer endpoint."
                ),
            ) from None

        if error_code in ("23503", "23514", "22003", "22P02"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid customer or item data.",
            ) from None

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to create customer and record payment.",
        ) from None



def create_item_for_existing_customer(
    payload: ExistingCustomerRequest,
    owner_user_id: str,
    supabase: Client,
) -> dict:

    items = []

    for item in payload.items:
        item_data = {
            "item_name": item.item_name.strip(),
            "weight_grams": str(item.weight_grams),
            "rate_per_gram": (
                str(item.rate_per_gram)
                if item.rate_per_gram is not None
                else None
            ),
            "total_amount": str(item.total_amount),
            "reminder_date": (
                item.reminder_date.isoformat()
                if item.reminder_date is not None
                else None
            ),
            "sms_reminder_enabled": item.sms_reminder_enabled,
        }

        if payload.payment_mode == "item_wise":
            item_data["amount_paid"] = str(item.amount_paid)

        items.append(item_data)

    try:
        result = supabase.rpc(
            "add_items_for_existing_customer",
            {
                "p_phone_number": payload.phone_number,
                "p_receiver": payload.receiver,
                "p_owner_user_id": owner_user_id,
                "p_payment_mode": payload.payment_mode,
                "p_payment_method": payload.payment_method,
                "p_total_payment": (
                    str(payload.total_payment)
                    if payload.total_payment is not None
                    else None
                ),
                "p_items": items,
            },
        ).execute()

        if not result.data:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Item creation returned no data.",
            )

        return result.data

    except APIError as exc:
        error_code = getattr(exc, "code", None)

        if error_code == "P0002":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "Customer not found. "
                    "Register the customer first."
                ),
            ) from None

        if error_code in ("23503", "23514", "22003", "22P02"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid customer or item data.",
            ) from None

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to record customer items and payment.",
        ) from None


def record_customer_payment(
    customer_id: UUID,
    payload: CustomerPaymentRequest,
    owner_user_id: str,
    supabase: Client,
) -> dict:

    allocations = [
        {
            "item_id": str(allocation.item_id),
            "amount": str(allocation.amount),
        }
        for allocation in payload.allocations
    ]

    try:
        result = supabase.rpc(
            "record_customer_payment",
            {
                "p_customer_id": str(customer_id),
                "p_owner_user_id": owner_user_id,
                "p_payment_method": payload.payment_method,
                "p_allocations": allocations,
            },
        ).execute()

        if not result.data:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Payment recording returned no data.",
            )

        return result.data

    except APIError as exc:
        error_code = getattr(exc, "code", None)

        if error_code == "P0002":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "Customer or item not found for this shop."
                ),
            ) from None

        if error_code == "23514":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Payment exceeds the outstanding balance "
                    "of one or more items."
                ),
            ) from None

        if error_code in ("22P02", "22003", "23503"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid payment or item data.",
            ) from None

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to record customer payment.",
        ) from None

def get_customer_details(
    customer_id: UUID,
    owner_user_id: str,
    supabase: Client,
) -> dict:
    try:
        # 1. Fetch the global customer profile.
        customer_result = (
            supabase.table("customers")
            .select("*")
            .eq("id", str(customer_id))
            .execute()
        )

        if not customer_result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Customer not found.",
            )

        customer = customer_result.data[0]

        # 2. Fetch only items belonging to this shop.
        items_result = (
            supabase.table("customer_items")
            .select("*")
            .eq("customer_id", str(customer_id))
            .eq("owner_user_id", owner_user_id)
            .order("created_at", desc=True)
            .execute()
        )

        items = items_result.data or []

        # No items means this customer has no purchase relationship
        # with the authenticated shop.
        if not items:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Customer not found in this shop.",
            )

        # 3. Calculate financial summary from this shop's items.
        total_purchase = sum(
            (Decimal(str(item["total_amount"])) for item in items),
            Decimal("0"),
        )

        total_paid = sum(
            (Decimal(str(item["amount_paid"])) for item in items),
            Decimal("0"),
        )

        total_due = sum(
            (Decimal(str(item["due_amount"])) for item in items),
            Decimal("0"),
        )

        return {
            "customer": customer,
            "items": items,
            "summary": {
                "total_purchase_amount": total_purchase,
                "total_paid_amount": total_paid,
                "total_due_amount": total_due,
                "items_count": len(items),
            },
        }

    except APIError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to retrieve customer details.",
        )