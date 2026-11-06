
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor


DATABASE_URL = os.getenv("OTP_TEST_DATABASE_URL")
ALLOW_DATABASE_RESET = os.getenv("OTP_TEST_ALLOW_DATABASE_RESET") == "1"

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20261106000001_otp_rate_limits.sql"
)

FUNCTION_NAME = "public.check_and_record_otp_rate_limit"

pytestmark = pytest.mark.skipif(
    not DATABASE_URL or not ALLOW_DATABASE_RESET,
    reason=(
        "Set OTP_TEST_DATABASE_URL and "
        "OTP_TEST_ALLOW_DATABASE_RESET=1 for isolated PostgreSQL tests."
    ),
)


def connect():
    """Create a new PostgreSQL connection."""

    if not DATABASE_URL:
        pytest.skip("OTP_TEST_DATABASE_URL is not configured.")

    return psycopg2.connect(DATABASE_URL)


def assert_isolated_test_database(conn):
    """
    Prevent accidental execution against a production database.

    The database name must contain 'test' and must not contain
    common production identifiers.
    """

    with conn.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        database_name = cursor.fetchone()[0].lower()

    forbidden = ("prod", "production", "live")

    if "test" not in database_name:
        raise RuntimeError(
            "Refusing to run integration tests: "
            "database name must contain 'test'."
        )

    if any(word in database_name for word in forbidden):
        raise RuntimeError(
            "Refusing to run integration tests against a production-like database."
        )

    if not ALLOW_DATABASE_RESET:
        raise RuntimeError(
            "Database reset permission was not explicitly enabled."
        )


@pytest.fixture(scope="session", autouse=True)
def prepare_database():
    """
    Apply the migration to an explicitly designated test database.

    This fixture may create database objects. It must never be
    pointed at production.
    """

    if not DATABASE_URL or not ALLOW_DATABASE_RESET:
        pytest.skip("Isolated PostgreSQL test database not configured.")

    if not MIGRATION_PATH.exists():
        raise FileNotFoundError(
            f"Migration file not found: {MIGRATION_PATH}"
        )

    conn = connect()

    try:
        assert_isolated_test_database(conn)

        with conn.cursor() as cursor:
            cursor.execute(MIGRATION_PATH.read_text(encoding="utf-8"))

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@pytest.fixture(autouse=True)
def clean_rate_limit_events(prepare_database):
    """
    Clean the dedicated test table before each test.

    This is destructive, which is why the explicit test-database
    guard is mandatory.
    """

    conn = connect()

    try:
        assert_isolated_test_database(conn)

        with conn.cursor() as cursor:
            cursor.execute(
                """
                TRUNCATE TABLE public.otp_rate_limit_events
                RESTART IDENTITY
                """
            )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def call_rate_limit(
    email,
    client_ip="198.51.100.10",
    max_daily=10,
):
    """Call the real PostgreSQL RPC function."""

    conn = connect()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                f"""
                SELECT *
                FROM {FUNCTION_NAME}(%s, %s, %s)
                """,
                (email, client_ip, max_daily),
            )

            result = cursor.fetchone()

        conn.commit()

        if result is None:
            raise AssertionError("RPC returned no result.")

        return dict(result)

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def seed_event(
    email,
    client_ip,
    requested_at,
    cooldown_seconds=60,
):
    """Insert a historical event for boundary testing."""

    key = f"otp:{email.strip().lower()}:{client_ip}"

    conn = connect()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.otp_rate_limit_events (
                    rate_limit_key,
                    email,
                    client_ip,
                    requested_at,
                    cooldown_seconds
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    key,
                    email.strip().lower(),
                    client_ip,
                    requested_at,
                    cooldown_seconds,
                ),
            )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def get_event_count(email, client_ip="198.51.100.10"):
    """Return the number of events for a particular email + IP."""

    key = f"otp:{email.strip().lower()}:{client_ip}"

    conn = connect()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM public.otp_rate_limit_events
                WHERE rate_limit_key = %s
                """,
                (key,),
            )

            return cursor.fetchone()[0]

    finally:
        conn.close()


# =========================================================
# 1. Basic RPC behavior
# =========================================================

def test_first_otp_request_is_allowed():
    result = call_rate_limit("first@example.com")

    assert result["is_allowed"] is True
    assert result["retry_after_seconds"] == 0
    assert result["detail_message"]


def test_immediate_second_request_is_rate_limited():
    email = "cooldown@example.com"

    first = call_rate_limit(email)
    second = call_rate_limit(email)

    assert first["is_allowed"] is True

    assert second["is_allowed"] is False
    assert 1 <= second["retry_after_seconds"] <= 60


# =========================================================
# 2. Progressive cooldown
# =========================================================

def test_progressive_cooldown_schedule():
    email = "progressive@example.com"

    expected_cooldowns = [60, 120, 180, 300, 900, 3600]

    for expected in expected_cooldowns:
        result = call_rate_limit(email)

        assert result["is_allowed"] is True

        conn = connect()

        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT cooldown_seconds
                    FROM public.otp_rate_limit_events
                    WHERE rate_limit_key = %s
                    ORDER BY requested_at DESC
                    LIMIT 1
                    """,
                    (f"otp:{email}:198.51.100.10",),
                )

                stored_cooldown = cursor.fetchone()[0]

                assert stored_cooldown == expected

                # Move the last accepted event into the past so
                # the next request can pass the cooldown check.
                cursor.execute(
                    """
                    UPDATE public.otp_rate_limit_events
                    SET requested_at =
                        pg_catalog.clock_timestamp()
                        - pg_catalog.make_interval(
                            secs => cooldown_seconds + 1
                        )
                    WHERE rate_limit_key = %s
                    """,
                    (f"otp:{email}:198.51.100.10",),
                )

            conn.commit()

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()


# =========================================================
# 3. Strict rolling 24-hour limit
# =========================================================

def test_tenth_request_allowed_but_eleventh_rejected():
    email = "dailycap@example.com"

    now = datetime.now(timezone.utc)

    # Seed 9 historical requests, all inside the last 24 hours.
    # The newest is older than the maximum 3600-second cooldown.
    for hours_ago in range(2, 11):
        seed_event(
            email,
            "198.51.100.10",
            now - timedelta(hours=hours_ago),
            cooldown_seconds=3600,
        )

    tenth = call_rate_limit(email)

    assert tenth["is_allowed"] is True
    assert get_event_count(email) == 10

    # Make the latest event older than its cooldown.
    conn = connect()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE public.otp_rate_limit_events
                SET requested_at =
                    pg_catalog.clock_timestamp() - INTERVAL '2 hours'
                WHERE rate_limit_key = %s
                """,
                (f"otp:{email}:198.51.100.10",),
            )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    eleventh = call_rate_limit(email)

    assert eleventh["is_allowed"] is False
    assert eleventh["retry_after_seconds"] > 0
    assert get_event_count(email) == 10


def test_expired_oldest_event_frees_one_rolling_window_slot():
    email = "rolling-boundary@example.com"

    now = datetime.now(timezone.utc)

    # One event has expired.
    seed_event(
        email,
        "198.51.100.10",
        now - timedelta(hours=24, seconds=10),
        cooldown_seconds=60,
    )

    # Nine events remain within the rolling 24-hour window.
    for hours_ago in range(2, 11):
        seed_event(
            email,
            "198.51.100.10",
            now - timedelta(hours=hours_ago),
            cooldown_seconds=60,
        )

    result = call_rate_limit(email)

    assert result["is_allowed"] is True

    # Expired event is removed and a new event is inserted.
    assert get_event_count(email) == 10


def test_daily_limit_retry_is_based_on_oldest_event():
    email = "retry-window@example.com"

    now = datetime.now(timezone.utc)

    for hours_ago in range(2, 12):
        seed_event(
            email,
            "198.51.100.10",
            now - timedelta(hours=hours_ago),
            cooldown_seconds=60,
        )

    result = call_rate_limit(email)

    assert result["is_allowed"] is False
    assert result["retry_after_seconds"] > 0

    # Oldest event is approximately 11 hours old.
    assert result["retry_after_seconds"] <= (13 * 60 * 60)


# =========================================================
# 4. Email + IP isolation
# =========================================================

def test_different_emails_have_separate_limits():
    first = call_rate_limit("first-user@example.com")
    second = call_rate_limit("second-user@example.com")

    assert first["is_allowed"] is True
    assert second["is_allowed"] is True


def test_same_email_different_ips_have_separate_limits():
    first = call_rate_limit(
        "same-user@example.com",
        "198.51.100.10",
    )

    second = call_rate_limit(
        "same-user@example.com",
        "198.51.100.20",
    )

    assert first["is_allowed"] is True
    assert second["is_allowed"] is True


# =========================================================
# 5. Concurrent request safety
# =========================================================

def test_concurrent_requests_are_serialized():
    email = "concurrent@example.com"
    client_ip = "198.51.100.30"

    def make_request(_):
        return call_rate_limit(email, client_ip)

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(make_request, range(10)))

    allowed_count = sum(
        1 for result in results if result["is_allowed"]
    )

    rejected_count = sum(
        1 for result in results if not result["is_allowed"]
    )

    assert allowed_count == 1
    assert rejected_count == 9

    assert get_event_count(email, client_ip) == 1


# =========================================================
# 6. Database function permissions
# =========================================================

def test_rpc_function_permissions():
    conn = connect()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    has_function_privilege(
                        'service_role',
                        %s,
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        'anon',
                        %s,
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        'authenticated',
                        %s,
                        'EXECUTE'
                    )
                """,
                (
                    FUNCTION_NAME,
                    FUNCTION_NAME,
                    FUNCTION_NAME,
                ),
            )

            service_allowed, anon_allowed, authenticated_allowed = (
                cursor.fetchone()
            )

        assert service_allowed is True
        assert anon_allowed is False
        assert authenticated_allowed is False

    finally:
        conn.close()


def test_direct_table_access_is_not_granted_to_api_roles():
    conn = connect()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    has_table_privilege(
                        'anon',
                        'public.otp_rate_limit_events',
                        'SELECT'
                    ),
                    has_table_privilege(
                        'authenticated',
                        'public.otp_rate_limit_events',
                        'SELECT'
                    ),
                    has_table_privilege(
                        'service_role',
                        'public.otp_rate_limit_events',
                        'SELECT'
                    )
                """
            )

            anon_select, authenticated_select, service_select = (
                cursor.fetchone()
            )

        assert anon_select is False
        assert authenticated_select is False
        assert service_select is False

    finally:
        conn.close()
