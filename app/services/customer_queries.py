from fastapi import HTTPException, status
from postgrest.exceptions import APIError
from supabase import Client
from uuid import UUID

def get_owner_customers_summary(
    owner_user_id: str,
    supabase: Client,
    filter: str = "all",
    search: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    try:
        result = (
            supabase.rpc(
                "get_owner_customers_summary",
                {
                    "p_owner_user_id": owner_user_id,
                    "p_filter": filter,
                    "p_search": search,
                    "p_limit": limit,
                    "p_offset": offset,
                },
            )
            .execute()
        )

        rows = result.data or []

        # The SQL RPC currently returns total_count on each row.
        total = rows[0]["total_count"] if rows else 0

        customers = [
            {
                "customer_id": row["customer_id"],
                "name": row["name"],
                "phone_number": row["phone_number"],
                "total_paid_amount": row["total_paid_amount"],
                "total_due_amount": row["total_due_amount"],
            }
            for row in rows
            if row["customer_id"] is not None
        ]

        return {
            "customers": customers,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    except APIError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to retrieve customers.",
        ) from None


def get_customer_items_for_owner(
    customer_id: str,
    owner_user_id: str,
    supabase: Client,
):
    try:
        result = (
            supabase.table("customer_items")
            .select("*")
            .eq("customer_id", customer_id)
            .eq("owner_user_id", owner_user_id)
            .order("created_at", desc=True)
            .execute()
        )

        if not result.data:
            return []

        return result.data

    except APIError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to fetch customer items.",
        ) from None

def get_owner_transactions(
    owner_user_id: str,
    supabase: Client,
    search: str | None = None,
    payment_method: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    try:
        result = (
            supabase.rpc(
                "get_owner_transactions",
                {
                    "p_owner_user_id": owner_user_id,
                    "p_search": search,
                    "p_payment_method": payment_method,
                    "p_start_date": start_date,
                    "p_end_date": end_date,
                    "p_limit": limit,
                    "p_offset": offset,
                },
            )
            .execute()
        )

        rows = result.data or []

        # Read aggregate information before removing
        # the empty-page placeholder row.
        total = int(rows[0]["total_count"]) if rows else 0

        total_collected = (
            rows[0]["total_collected_amount"]
            if rows
            else "0"
        )

        transactions = [
            {
                "transaction_id": row["transaction_id"],
                "customer_id": row["customer_id"],
                "customer_name": row["customer_name"],
                "customer_phone_number": row["customer_phone_number"],
                "amount": row["amount"],
                "payment_method": row["payment_method"],
                "transaction_date": row["transaction_date"],
                "notes": row["notes"],
            }
            for row in rows
            if row["transaction_id"] is not None
        ]

        return {
            "transactions": transactions,
            "total": total,
            "total_collected_amount": total_collected,
            "limit": limit,
            "offset": offset,
        }

    except APIError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to retrieve transactions.",
        ) from None

def get_owner_transaction_details(
    transaction_id: UUID,
    owner_user_id: str,
    supabase: Client,
) -> dict:
    try:
        result = (
            supabase.rpc(
                "get_owner_transaction_details",
                {
                    "p_transaction_id": str(transaction_id),
                    "p_owner_user_id": owner_user_id,
                },
            )
            .execute()
        )

        if not result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transaction not found.",
            )

        # Supabase returns the JSONB result under the function name.
        transaction_data = result.data

        transaction_data = result.data

        if isinstance(transaction_data, list):
            transaction_data = transaction_data[0] if transaction_data else None

        if isinstance(transaction_data, dict):
            transaction_data = transaction_data.get(
                "get_owner_transaction_details",
                transaction_data,
            )

        if not isinstance(transaction_data, dict):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transaction not found.",
            )

        return transaction_data

    except APIError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to retrieve transaction details.",
        ) from None