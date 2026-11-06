
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from gotrue.types import User
from supabase import Client

from app.core.security import get_current_user
from app.core.supabase import get_supabase_client

router = APIRouter(
    prefix="/market-rates",
    tags=["Market Rates"],
)


def read_snapshot(
    metal: str,
    supabase: Client,
) -> dict[str, Any]:
    result = (
        supabase.table("market_rate_snapshots")
        .select(
            "metal,market_date,source_generated_at,fetched_at,rates"
        )
        .eq("metal", metal)
        .limit(1)
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market rates have not been initialized yet",
        )

    return result.data[0]


def common_response(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "metal": row["metal"],
        "market_date": row["market_date"],
        "source_generated_at": row["source_generated_at"],
        "fetched_at": row["fetched_at"],
    }


@router.get("/gold")
def get_gold_rates(
    current_user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
) -> dict[str, Any]:
    row = read_snapshot("gold", supabase)
    response = common_response(row)

    karats = {
        "XAU.18K": "18K",
        "XAU.22K": "22K",
        "XAU.24K": "24K",
    }

    response["rates"] = [
        {
            "karat": karats[observation["instrument"]],
            "instrument_id": observation["instrument_id"],
            "price_per_gram": observation["value"],
            "previous_close_per_gram": observation["prev_close"],
            "change_pct": observation["change_pct"],
            "status": observation["status"],
            "is_trading_day": observation["is_trading_day"],
            "filled_from": observation["filled_from"],
            "source": observation["source"],
            "revision": observation["revision"],
            "date": observation["date"],
        }
        for observation in row["rates"]
    ]

    return response


@router.get("/silver")
def get_silver_rates(
    current_user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase_client),
) -> dict[str, Any]:
    row = read_snapshot("silver", supabase)
    response = common_response(row)

    observation = row["rates"][0]
    price_per_kg = observation["value"]
    previous_close_per_kg = observation["prev_close"]

    response["rates"] = [
        {
            "instrument_id": observation["instrument_id"],
            "price_per_kg": price_per_kg,
            "price_per_gram": price_per_kg / 1000,
            "previous_close_per_kg": previous_close_per_kg,
            "previous_close_per_gram": previous_close_per_kg / 1000,
            "change_pct": observation["change_pct"],
            "status": observation["status"],
            "is_trading_day": observation["is_trading_day"],
            "filled_from": observation["filled_from"],
            "source": observation["source"],
            "revision": observation["revision"],
            "date": observation["date"],
        }
    ]

    return response
