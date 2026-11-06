
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.supabase import get_supabase_client

logger = logging.getLogger(__name__)

ENDPOINTS = {
    "gold": "https://snapdata.dev/api/v1/gold/in/latest.json",
    "silver": "https://snapdata.dev/api/v1/silver/in/latest.json",
}

EXPECTED_INSTRUMENTS = {
    "gold": {
        "XAU.18K.INR.G",
        "XAU.22K.INR.G",
        "XAU.24K.INR.G",
    },
    "silver": {"XAG.INR.KG"},
}


def fetch_and_validate(
    client: httpx.Client,
    metal: str,
) -> dict[str, Any]:
    response = client.get(ENDPOINTS[metal])
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict):
        raise ValueError(f"{metal}: response must be a JSON object")

    if payload.get("dataset") != metal:
        raise ValueError(f"{metal}: unexpected dataset")

    if payload.get("scope") != "in":
        raise ValueError(f"{metal}: unexpected scope")

    if payload.get("resolution") != "daily":
        raise ValueError(f"{metal}: unexpected resolution")

    expected_quantity = "gram" if metal == "gold" else "kilogram"
    unit = payload.get("unit", {})

    if (
        unit.get("quantity") != expected_quantity
        or unit.get("currency") != "INR"
    ):
        raise ValueError(f"{metal}: unexpected price unit")

    generated_at = payload.get("generated_at")
    coverage = payload.get("coverage", {})
    observations = payload.get("observations")

    if not generated_at or not coverage.get("to"):
        raise ValueError(f"{metal}: missing timestamp or coverage")

    if not isinstance(observations, list):
        raise ValueError(f"{metal}: observations must be a list")

    instruments = set()
    observation_dates = []

    for observation in observations:
        instrument_id = observation.get("instrument_id")
        value = observation.get("value")
        observation_date = observation.get("date")

        if instrument_id in instruments:
            raise ValueError(f"{metal}: duplicate instrument {instrument_id}")

        instruments.add(instrument_id)

        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
        ):
            raise ValueError(f"{metal}: invalid value for {instrument_id}")

        if not observation_date:
            raise ValueError(f"{metal}: missing observation date")

        observation_dates.append(observation_date)

    if instruments != EXPECTED_INSTRUMENTS[metal]:
        raise ValueError(
            f"{metal}: unexpected instruments: {sorted(instruments)}"
        )

    if len(set(observation_dates)) != 1:
        raise ValueError(f"{metal}: observations have inconsistent dates")

    market_date = observation_dates[0]

    if market_date != coverage.get("to"):
        raise ValueError(f"{metal}: coverage and observation dates differ")

    # Validate the source timestamp and date format.
    datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    datetime.strptime(market_date, "%Y-%m-%d")

    return {
        "metal": metal,
        "market_date": market_date,
        "source_generated_at": generated_at,
        "rates": observations,
    }


def refresh_market_rates() -> None:
    supabase = get_supabase_client()
    run_id = None

    try:
        log_result = (
            supabase.table("market_rate_refresh_runs")
            .insert({"success": False})
            .execute()
        )

        if log_result.data:
            run_id = log_result.data[0]["id"]

        with httpx.Client(timeout=20.0) as client:
            gold = fetch_and_validate(client, "gold")
            silver = fetch_and_validate(client, "silver")

        # One RPC call: both cache rows update in one transaction.
        supabase.rpc(
            "upsert_market_rate_snapshots",
            {"p_snapshots": [gold, silver]},
        ).execute()

        logger.info("Gold and silver market snapshots refreshed")

        if run_id is not None:
            supabase.table("market_rate_refresh_runs").update(
                {
                    "success": True,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "error_message": None,
                }
            ).eq("id", run_id).execute()

    except Exception as exc:
        logger.exception("Market-rate refresh failed")

        if run_id is not None:
            try:
                supabase.table("market_rate_refresh_runs").update(
                    {
                        "success": False,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "error_message": str(exc)[:2000],
                    }
                ).eq("id", run_id).execute()
            except Exception:
                logger.exception("Could not record refresh failure")

        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    refresh_market_rates()
