from typing import Any
from uuid import UUID

from postgrest.exceptions import APIError

from app.schemas.supplier import (
    CreateSupplierRequest,
    AddSupplierItemsRequest,
    RecordSupplierPaymentRequest,
)


def normalize_rpc_response(data: Any, function_name: str) -> dict:
    """
    Normalize Supabase RPC responses.

    JSONB RPCs generally return a dictionary.
    This also handles list-wrapped responses.
    """

    if isinstance(data, dict):
        # Handle a possible function-name wrapper.
        if function_name in data:
            return data[function_name]

        return data

    if isinstance(data, list) and len(data) == 1:
        result = data[0]

        if isinstance(result, dict):
            if function_name in result:
                return result[function_name]

            return result

    raise ValueError(
        f"Unexpected response from RPC: {function_name}"
    )


def execute_rpc(
    supabase,
    function_name: str,
    params: dict,
) -> dict:
    """
    Execute a PostgreSQL RPC and normalize its result.
    """

    try:
        response = (
            supabase
            .rpc(function_name, params)
            .execute()
        )

        return normalize_rpc_response(
            response.data,
            function_name,
        )

    except APIError:
        # Preserve the original database error.
        raise


# -----------------------------------------
# 1. Create supplier with purchases
# -----------------------------------------

def create_supplier(
    owner_user_id: UUID,
    payload: CreateSupplierRequest,
    supabase,
) -> dict:

    params = {
        "p_owner_user_id": str(owner_user_id),
        "p_supplier_name": payload.supplier_name,
        "p_supplier_phone": payload.phone_number,
        "p_supplier_address": payload.address,
        "p_items": [
            item.model_dump(mode="json")
            for item in payload.items
        ],
        "p_payment_mode": payload.payment_mode,
        "p_total_payment": str(payload.total_payment),
        "p_payment_method": payload.payment_method,
        "p_notes": payload.notes,
    }

    return execute_rpc(
        supabase,
        "create_supplier_with_items_and_payment",
        params,
    )


# -----------------------------------------
# 2. Add purchases to existing supplier
# -----------------------------------------

def add_supplier_items(
    owner_user_id: UUID,
    payload: AddSupplierItemsRequest,
    supabase,
) -> dict:

    params = {
        "p_owner_user_id": str(owner_user_id),
        "p_supplier_id": str(payload.supplier_id),
        "p_items": [
            item.model_dump(mode="json")
            for item in payload.items
        ],
        "p_payment_mode": payload.payment_mode,
        "p_total_payment": str(payload.total_payment),
        "p_payment_method": payload.payment_method,
        "p_notes": payload.notes,
    }

    return execute_rpc(
        supabase,
        "add_items_for_existing_supplier",
        params,
    )


# -----------------------------------------
# 3. Record subsequent supplier payment
# -----------------------------------------

def record_supplier_payment(
    owner_user_id: UUID,
    payload: RecordSupplierPaymentRequest,
    supabase,
) -> dict:

    params = {
        "p_owner_user_id": str(owner_user_id),
        "p_supplier_id": str(payload.supplier_id),
        "p_amount": str(payload.amount),
        "p_payment_method": payload.payment_method,
        "p_allocations": [
            allocation.model_dump(mode="json")
            for allocation in payload.allocations
        ],
        "p_notes": payload.notes,
    }

    return execute_rpc(
        supabase,
        "record_supplier_payment",
        params,
    )


# -----------------------------------------
# 4. List suppliers with financial summary
# -----------------------------------------

def get_suppliers(
    owner_user_id: UUID,
    filter: str,
    search: str | None,
    limit: int,
    offset: int,
    supabase,
) -> dict:

    params = {
        "p_owner_user_id": str(owner_user_id),
        "p_filter": filter,
        "p_search": search,
        "p_limit": limit,
        "p_offset": offset,
    }

    return execute_rpc(
        supabase,
        "get_owner_suppliers_summary",
        params,
    )


# -----------------------------------------
# 5. Get supplier details and purchase history
# -----------------------------------------

def get_supplier_details(
    owner_user_id: UUID,
    supplier_id: UUID,
    supabase,
) -> dict:

    params = {
        "p_owner_user_id": str(owner_user_id),
        "p_supplier_id": str(supplier_id),
    }

    return execute_rpc(
        supabase,
        "get_owner_supplier_details",
        params,
    )


# -----------------------------------------
# 6. List supplier transactions
# -----------------------------------------

def get_supplier_transactions(
    owner_user_id: UUID,
    supplier_id: UUID | None,
    search: str | None,
    start_date,
    end_date,
    limit: int,
    offset: int,
    supabase,
) -> dict:

    params = {
        "p_owner_user_id": str(owner_user_id),
        "p_supplier_id": (
            str(supplier_id) if supplier_id else None
        ),
        "p_search": search,
        "p_start_date": (
            start_date.isoformat() if start_date else None
        ),
        "p_end_date": (
            end_date.isoformat() if end_date else None
        ),
        "p_limit": limit,
        "p_offset": offset,
    }

    return execute_rpc(
        supabase,
        "get_owner_supplier_transactions",
        params,
    )


# -----------------------------------------
# 7. Get individual transaction details
# -----------------------------------------

def get_supplier_transaction_details(
    owner_user_id: UUID,
    transaction_id: UUID,
    supabase,
) -> dict:

    params = {
        "p_owner_user_id": str(owner_user_id),
        "p_transaction_id": str(transaction_id),
    }

    return execute_rpc(
        supabase,
        "get_owner_supplier_transaction_details",
        params,
    )