
import ipaddress
import logging
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any

from fastapi import HTTPException, Request, status
from postgrest.exceptions import APIError
from supabase import Client

from app.core.supabase import get_settings


logger = logging.getLogger(__name__)


COOLDOWN_SCHEDULE = [60, 120, 180, 300, 900]
MAX_SCHEDULED_COOLDOWN = 3600

# Local/test fallback only.
# Never use this store for production rate limiting.
_test_memory_lock = RLock()
_test_memory_store: dict[str, dict[str, Any]] = {}


def parse_ip(
    ip_str: str | None,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse a single IP address. CIDR ranges are not accepted."""

    if not ip_str or not isinstance(ip_str, str):
        return None

    value = ip_str.strip()

    if not value or "/" in value:
        return None

    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _trusted_proxy_networks(
    raw_value: str | None,
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse configured trusted proxy CIDRs."""

    networks = []

    if not raw_value:
        return networks

    for item in raw_value.split(","):
        value = item.strip()

        if not value:
            continue

        try:
            networks.append(
                ipaddress.ip_network(value, strict=False)
            )
        except ValueError:
            logger.warning(
                "Ignoring invalid trusted proxy network configuration entry."
            )

    return networks


def _is_trusted(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: list[
        ipaddress.IPv4Network | ipaddress.IPv6Network
    ],
) -> bool:
    """Check whether an IP belongs to a configured trusted network."""

    return any(
        ip.version == network.version and ip in network
        for network in networks
    )


def get_client_ip(request: Request) -> str:
    """
    Extract the client IP safely.

    Forwarding headers are trusted only when the immediate socket
    peer belongs to a configured trusted proxy network.

    Invalid or missing network information fails closed.
    """

    peer_text = (
        request.client.host
        if request.client is not None
        else None
    )

    peer_ip = parse_ip(peer_text)

    if peer_ip is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to determine client network address.",
        )

    settings = get_settings()

    networks = _trusted_proxy_networks(
        settings.trusted_proxies
    )

    # Never trust forwarded headers from an untrusted peer.
    if not _is_trusted(peer_ip, networks):
        return str(peer_ip)

    forwarded_for = request.headers.get("x-forwarded-for")

    if not forwarded_for or not forwarded_for.strip():
        return str(peer_ip)

    # Preserve each hop and reject empty or malformed entries.
    forwarded_parts = [
        part.strip()
        for part in forwarded_for.split(",")
    ]

    forwarded_ips = [
        parse_ip(part)
        for part in forwarded_parts
    ]

    # Correct validation: every forwarded hop must be a valid IP.
    if (
        not forwarded_ips
        or any(ip is None for ip in forwarded_ips)
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Invalid forwarded client address.",
        )

    # At this point, None values have already been rejected.
    hops = forwarded_ips + [peer_ip]

    # Walk from the immediate peer towards the original client.
    index = len(hops) - 1

    while index > 0 and _is_trusted(hops[index], networks):
        index -= 1

    return str(hops[index])


def get_cooldown_duration(attempt_count: int) -> int:
    """Return progressive cooldown duration in seconds."""

    if attempt_count <= 1:
        return COOLDOWN_SCHEDULE[0]

    if attempt_count <= len(COOLDOWN_SCHEDULE):
        return COOLDOWN_SCHEDULE[attempt_count - 1]

    return MAX_SCHEDULED_COOLDOWN


def format_seconds_to_mmss(total_seconds: int) -> str:
    """Format seconds into MM:SS."""

    minutes, seconds = divmod(
        max(0, int(total_seconds)),
        60,
    )

    return f"{minutes:02d}:{seconds:02d}"


def validate_rate_limit_response(
    data: Any,
) -> dict[str, Any]:
    """Validate the Supabase RPC single-row response."""

    unavailable = HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "Rate limiter service is temporarily unavailable. "
            "Please try again."
        ),
    )

    if not isinstance(data, list) or len(data) != 1:
        raise unavailable

    result = data[0]

    if not isinstance(result, dict):
        raise unavailable

    allowed = result.get("is_allowed")
    retry_after = result.get("retry_after_seconds")
    message = result.get("detail_message")

    if not isinstance(allowed, bool):
        raise unavailable

    if (
        not isinstance(retry_after, int)
        or isinstance(retry_after, bool)
        or retry_after < 0
    ):
        raise unavailable

    if not isinstance(message, str):
        raise unavailable

    return result


def check_and_record_otp_request(
    email: str,
    client_ip: str,
    supabase: Client | None = None,
    *,
    allow_test_memory_fallback: bool = False,
) -> tuple[bool, int, str]:
    """
    Check and record an OTP request.

    Production:
        Supabase RPC is mandatory.
        Missing Supabase client returns HTTP 503.
        RPC failures return HTTP 503.
        RPC failures NEVER trigger memory fallback.

    Tests:
        In-memory fallback is allowed only when explicitly enabled
        and no Supabase client is supplied.
    """

    clean_email = email.strip().lower()

    parsed_ip = parse_ip(client_ip)

    if parsed_ip is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A valid client IP address is required.",
        )

    clean_ip = str(parsed_ip)

    max_daily = get_settings().max_daily_otp_requests

    # Persistent Supabase rate limiting.
    if supabase is not None:
        try:
            response = supabase.rpc(
                "check_and_record_otp_rate_limit",
                {
                    "p_email": clean_email,
                    "p_client_ip": clean_ip,
                    "p_max_daily": max_daily,
                },
            ).execute()

            validated = validate_rate_limit_response(
                response.data if response else None
            )

            return (
                validated["is_allowed"],
                validated["retry_after_seconds"],
                validated["detail_message"],
            )

        except HTTPException:
            raise

        except APIError as exc:
            logger.error(
                "Supabase OTP rate-limiter RPC failed (code=%s).",
                getattr(exc, "code", None),
            )

            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Rate limiter service is temporarily unavailable. "
                    "Please try again."
                ),
            ) from None

        except Exception:
            logger.error(
                "Unexpected OTP rate-limiter database failure."
            )

            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Rate limiter service is temporarily unavailable. "
                    "Please try again."
                ),
            ) from None

    # Supabase client missing: production must fail closed.
    if not allow_test_memory_fallback:
        logger.error(
            "OTP rate limiter unavailable: Supabase client missing."
        )

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Rate limiter service is temporarily unavailable. "
                "Please try again."
            ),
        )

    # Explicit local/test-only fallback.
    now = datetime.now(timezone.utc)

    key = f"otp:{clean_email}:{clean_ip}"

    with _test_memory_lock:
        state = _test_memory_store.get(key)

        if state is None:
            state = {
                "attempt_count": 0,
                "daily_count": 0,
                "cooldown_until": now,
                "daily_reset_at": now + timedelta(days=1),
            }

            _test_memory_store[key] = state

        # Reset the rolling 24-hour window.
        if now >= state["daily_reset_at"]:
            state["daily_count"] = 0
            state["attempt_count"] = 0
            state["daily_reset_at"] = now + timedelta(days=1)

        # Daily limit check.
        if state["daily_count"] >= max_daily:
            retry = max(
                1,
                int(
                    (
                        state["daily_reset_at"] - now
                    ).total_seconds()
                ),
            )

            return (
                False,
                retry,
                (
                    "Daily OTP limit reached. Please wait "
                    f"{format_seconds_to_mmss(retry)}."
                ),
            )

        # Progressive cooldown check.
        if now < state["cooldown_until"]:
            retry = max(
                1,
                int(
                    (
                        state["cooldown_until"] - now
                    ).total_seconds()
                ),
            )

            return (
                False,
                retry,
                (
                    "Too many requests. Please wait "
                    f"{format_seconds_to_mmss(retry)}."
                ),
            )

        state["attempt_count"] += 1
        state["daily_count"] += 1

        cooldown = get_cooldown_duration(
            state["attempt_count"]
        )

        state["cooldown_until"] = (
            now + timedelta(seconds=cooldown)
        )

        return True, 0, "OK"


def clear_rate_limit_store() -> None:
    """Clear the local/test-only in-memory rate-limit store."""

    with _test_memory_lock:
        _test_memory_store.clear()
