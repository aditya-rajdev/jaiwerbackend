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

# Progressive cooldown schedule in seconds
COOLDOWN_SCHEDULE = [
    60,   # Attempt 1 -> 60s (1 min)
    120,  # Attempt 2 -> 120s (2 min)
    180,  # Attempt 3 -> 180s (3 min)
    300,  # Attempt 4 -> 300s (5 min)
    900,  # Attempt 5 -> 900s (15 min)
]
MAX_SCHEDULED_COOLDOWN = 3600  # Attempt 6+ -> 3600s (1 hour)

# Isolated in-memory fallback for local unit testing without PostgreSQL
_test_memory_lock = RLock()
_test_memory_store: dict[str, dict[str, Any]] = {}


def parse_ip(ip_str: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Safely parse an IP address string using Python's ipaddress module without throwing exceptions."""
    if not ip_str or not isinstance(ip_str, str):
        return None
    try:
        return ipaddress.ip_address(ip_str.strip())
    except ValueError:
        return None


def get_client_ip(request: Request) -> str:
    """Extract and validate client IP address safely using Python's ipaddress module.

    Ensures untrusted immediate peers cannot spoof X-Forwarded-For headers.
    Forwarded headers are trusted ONLY if the immediate peer socket IP belongs to
    an explicitly configured trusted proxy network in settings.trusted_proxies.
    """
    settings = get_settings()

    # 1. Get raw socket connection peer IP
    socket_ip_str = request.client.host if request.client else "127.0.0.1"
    socket_ip = parse_ip(socket_ip_str) or parse_ip("127.0.0.1")

    # 2. Build list of trusted proxy IPs / networks from explicit configuration
    trusted_proxies_list: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    if settings.trusted_proxies:
        for item in settings.trusted_proxies.split(","):
            item_str = item.strip()
            if item_str:
                try:
                    trusted_proxies_list.append(ipaddress.ip_network(item_str, strict=False))
                except ValueError:
                    pass

    # Fail-safe: If no trusted proxies are configured or peer is not in list,
    # return peer socket IP directly without reading client-supplied headers.
    if not trusted_proxies_list or not any(socket_ip in net for net in trusted_proxies_list):
        return str(socket_ip)

    # 3. Peer is verified as a trusted proxy. Parse X-Forwarded-For header right-to-left
    forwarded_for = request.headers.get("x-forwarded-for")
    if not forwarded_for:
        real_ip_str = request.headers.get("x-real-ip")
        if real_ip_str:
            parsed_real = parse_ip(real_ip_str)
            if parsed_real:
                return str(parsed_real)
        return str(socket_ip)

    # Extract rightmost client IP from proxy chain that is not in trusted proxy networks
    raw_ips = [ip.strip() for ip in forwarded_for.split(",")]
    parsed_ips = []
    for raw_ip in raw_ips:
        parsed = parse_ip(raw_ip)
        if parsed:
            parsed_ips.append(parsed)

    if not parsed_ips:
        return str(socket_ip)

    for ip in reversed(parsed_ips):
        if not any(ip in net for net in trusted_proxies_list):
            return str(ip)

    return str(parsed_ips[0])


def get_cooldown_duration(attempt_count: int) -> int:
    """Calculate progressive cooldown duration based on attempt number."""
    if attempt_count <= 0:
        return COOLDOWN_SCHEDULE[0]
    if attempt_count > len(COOLDOWN_SCHEDULE):
        return MAX_SCHEDULED_COOLDOWN
    return COOLDOWN_SCHEDULE[attempt_count - 1]


def format_seconds_to_mmss(total_seconds: int) -> str:
    """Format total seconds into MM:SS format."""
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes:02d}:{seconds:02d}"


def check_and_record_otp_request(
    email: str,
    client_ip: str,
    supabase: Client | None = None,
) -> tuple[bool, int, str]:
    """Atomic progressive rate limiter for OTP requests.

    Queries PostgreSQL database via check_and_record_otp_rate_limit RPC for
    atomic row locking (FOR UPDATE) and persistent storage.
    Fails closed if the database service is unreachable in production.

    Returns:
        (is_allowed: bool, retry_after_seconds: int, detail_message: str)
    """
    clean_email = email.strip().lower()
    clean_ip = client_ip.strip()
    settings = get_settings()
    max_daily = getattr(settings, "max_daily_otp_requests", 10)

    # 1. Primary Production Path: Supabase PostgreSQL Atomic RPC
    if supabase is not None:
        try:
            rpc_response = supabase.rpc(
                "check_and_record_otp_rate_limit",
                {
                    "p_email": clean_email,
                    "p_client_ip": clean_ip,
                    "p_max_daily": max_daily,
                },
            ).execute()

            if rpc_response and rpc_response.data:
                row = rpc_response.data[0] if isinstance(rpc_response.data, list) else rpc_response.data
                is_allowed = bool(row.get("is_allowed", False))
                retry_after = int(row.get("retry_after_seconds", 0))
                message = str(row.get("detail_message", "OK"))
                return is_allowed, retry_after, message
        except APIError as exc:
            logger.error("Supabase PostgreSQL rate-limiter RPC failed: %s", exc.message)
            # Fail closed: Do not allow un-rate-limited requests when database fails
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rate limiter service is temporarily unavailable. Please try again.",
            ) from None
        except Exception:
            logger.exception("Unexpected database failure during rate-limiting.")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rate limiter service is temporarily unavailable. Please try again.",
            ) from None

    # 2. Local Unit-Testing Fallback (used only when supabase client dependency is not injected)
    now = datetime.now(timezone.utc)
    dimension_key = f"otp:{clean_email}:{clean_ip}"

    with _test_memory_lock:
        state = _test_memory_store.get(dimension_key)
        if not state:
            state = {
                "attempt_count": 0,
                "daily_count": 0,
                "last_request_at": now,
                "cooldown_until": now,
                "daily_reset_at": now + timedelta(days=1),
            }
            _test_memory_store[dimension_key] = state

        if now >= state["daily_reset_at"]:
            state["daily_count"] = 0
            state["attempt_count"] = 0
            state["daily_reset_at"] = now + timedelta(days=1)

        if state["daily_count"] >= max_daily:
            retry_after = int((state["daily_reset_at"] - now).total_seconds())
            retry_after = max(retry_after, 60)
            return (
                False,
                retry_after,
                f"Daily OTP limit reached. Please wait {format_seconds_to_mmss(retry_after)} before requesting another code.",
            )

        if now < state["cooldown_until"]:
            retry_after = int((state["cooldown_until"] - now).total_seconds())
            retry_after = max(retry_after, 1)
            return (
                False,
                retry_after,
                f"Too many requests. Please wait {format_seconds_to_mmss(retry_after)} before retrying.",
            )

        state["attempt_count"] += 1
        state["daily_count"] += 1
        state["last_request_at"] = now

        cooldown_secs = get_cooldown_duration(state["attempt_count"])
        state["cooldown_until"] = now + timedelta(seconds=cooldown_secs)

        return True, 0, "OK"


def clear_rate_limit_store() -> None:
    """Reset test memory store."""
    with _test_memory_lock:
        _test_memory_store.clear()
