
BEGIN;

-- =========================================================
-- 1. OTP REQUEST EVENT TABLE
-- One row = one OTP request admitted by the rate limiter.
-- Events older than 24 hours are excluded from the limit.
-- =========================================================

CREATE TABLE IF NOT EXISTS public.otp_rate_limit_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rate_limit_key TEXT NOT NULL,
    email TEXT NOT NULL,
    client_ip TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL
        DEFAULT pg_catalog.clock_timestamp(),
    cooldown_seconds INTEGER NOT NULL
        CHECK (cooldown_seconds BETWEEN 60 AND 3600)
);

CREATE INDEX IF NOT EXISTS idx_otp_rate_events_key_time
    ON public.otp_rate_limit_events
    (rate_limit_key, requested_at DESC);

CREATE INDEX IF NOT EXISTS idx_otp_rate_events_requested_at
    ON public.otp_rate_limit_events (requested_at);

ALTER TABLE public.otp_rate_limit_events OWNER TO postgres;

ALTER TABLE public.otp_rate_limit_events
    ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.otp_rate_limit_events
    FROM PUBLIC, anon, authenticated, service_role;

GRANT ALL ON TABLE public.otp_rate_limit_events TO postgres;


-- =========================================================
-- 2. ATOMIC OTP RATE-LIMIT FUNCTION
-- =========================================================

CREATE OR REPLACE FUNCTION public.check_and_record_otp_rate_limit(
    p_email TEXT,
    p_client_ip TEXT,
    p_max_daily INTEGER DEFAULT 10
)
RETURNS TABLE (
    is_allowed BOOLEAN,
    retry_after_seconds INTEGER,
    detail_message TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$

DECLARE
    v_email TEXT;
    v_ip TEXT;
    v_key TEXT;

    v_now TIMESTAMPTZ;

    v_request_count BIGINT;
    v_oldest_request TIMESTAMPTZ;

    v_last_request TIMESTAMPTZ;
    v_last_cooldown INTEGER;
    v_cooldown_until TIMESTAMPTZ;

    v_next_attempt BIGINT;
    v_new_cooldown INTEGER;
    v_retry_after INTEGER;

BEGIN

    -- -----------------------------------------------------
    -- INPUT VALIDATION
    -- -----------------------------------------------------

    IF p_email IS NULL OR p_client_ip IS NULL THEN
        RAISE EXCEPTION 'Invalid rate-limit input';
    END IF;

    v_email := pg_catalog.lower(
        pg_catalog.btrim(p_email)
    );

    IF v_email = ''
       OR pg_catalog.length(v_email) > 320
       OR v_email !~ '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}$'
    THEN
        RAISE EXCEPTION 'Invalid rate-limit email';
    END IF;

    IF pg_catalog.btrim(p_client_ip) = ''
       OR pg_catalog.length(pg_catalog.btrim(p_client_ip)) > 45
       OR pg_catalog.strpos(pg_catalog.btrim(p_client_ip), '/') > 0
    THEN
        RAISE EXCEPTION 'Invalid rate-limit IP';
    END IF;

    BEGIN

        v_ip := pg_catalog.host(
            pg_catalog.btrim(p_client_ip)::pg_catalog.inet
        );

    EXCEPTION
        WHEN OTHERS THEN
            RAISE EXCEPTION 'Invalid rate-limit IP';
    END;

    IF p_max_daily IS NULL
       OR p_max_daily < 1
       OR p_max_daily > 1000
    THEN
        RAISE EXCEPTION 'Invalid daily OTP limit';
    END IF;

    v_key := 'otp:' || v_email || ':' || v_ip;


    -- -----------------------------------------------------
    -- CONCURRENCY PROTECTION
    -- Serialize requests for the same email + IP.
    -- -----------------------------------------------------

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(v_key, 0)
    );

    -- Get actual time AFTER acquiring the lock.

    v_now := pg_catalog.clock_timestamp();


    -- -----------------------------------------------------
    -- REMOVE EXPIRED EVENTS FOR THIS RATE-LIMIT KEY
    -- -----------------------------------------------------

    DELETE FROM public.otp_rate_limit_events AS e
    WHERE e.rate_limit_key = v_key
      AND e.requested_at <=
          v_now - INTERVAL '24 hours';


    -- -----------------------------------------------------
    -- COUNT REQUESTS IN ROLLING 24-HOUR WINDOW
    -- -----------------------------------------------------

    SELECT
        pg_catalog.count(*),
        pg_catalog.min(e.requested_at)

    INTO
        v_request_count,
        v_oldest_request

    FROM public.otp_rate_limit_events AS e

    WHERE e.rate_limit_key = v_key
      AND e.requested_at > v_now - INTERVAL '24 hours';


    -- -----------------------------------------------------
    -- DAILY LIMIT CHECK
    -- Maximum 10 requests in rolling 24 hours.
    -- -----------------------------------------------------

    IF v_request_count >= p_max_daily THEN

        v_retry_after := GREATEST(
            1,
            pg_catalog.ceil(
                EXTRACT(
                    EPOCH FROM (
                        v_oldest_request
                        + INTERVAL '24 hours'
                        - v_now
                    )
                )
            )::INTEGER
        );

        RETURN QUERY
        SELECT
            FALSE,
            v_retry_after,
            'Daily OTP request limit reached. Try again later.'::TEXT;

        RETURN;

    END IF;


    -- -----------------------------------------------------
    -- CHECK COOLDOWN FROM MOST RECENT ACCEPTED REQUEST
    -- -----------------------------------------------------

    SELECT
        e.requested_at,
        e.cooldown_seconds

    INTO
        v_last_request,
        v_last_cooldown

    FROM public.otp_rate_limit_events AS e

    WHERE e.rate_limit_key = v_key

    ORDER BY
        e.requested_at DESC,
        e.id DESC

    LIMIT 1;


    IF v_last_request IS NOT NULL THEN

        v_cooldown_until :=
            v_last_request
            + pg_catalog.make_interval(
                secs => v_last_cooldown
            );

        IF v_now < v_cooldown_until THEN

            v_retry_after := GREATEST(
                1,
                pg_catalog.ceil(
                    EXTRACT(
                        EPOCH FROM (
                            v_cooldown_until - v_now
                        )
                    )
                )::INTEGER
            );

            RETURN QUERY
            SELECT
                FALSE,
                v_retry_after,
                'OTP request cooldown active. Try again later.'::TEXT;

            RETURN;

        END IF;

    END IF;


    -- -----------------------------------------------------
    -- PROGRESSIVE COOLDOWN
    --
    -- Request 1  = 60 seconds
    -- Request 2  = 120 seconds
    -- Request 3  = 180 seconds
    -- Request 4  = 300 seconds
    -- Request 5  = 900 seconds
    -- Request 6+ = 3600 seconds
    --
    -- Count is based on accepted requests still present
    -- in the rolling 24-hour window.
    -- -----------------------------------------------------

    v_next_attempt := v_request_count + 1;

    v_new_cooldown := CASE

        WHEN v_next_attempt = 1 THEN 60
        WHEN v_next_attempt = 2 THEN 120
        WHEN v_next_attempt = 3 THEN 180
        WHEN v_next_attempt = 4 THEN 300
        WHEN v_next_attempt = 5 THEN 900

        ELSE 3600

    END;


    -- -----------------------------------------------------
    -- RECORD THE ACCEPTED RATE-LIMIT REQUEST
    -- -----------------------------------------------------

    INSERT INTO public.otp_rate_limit_events (
        rate_limit_key,
        email,
        client_ip,
        requested_at,
        cooldown_seconds
    )

    VALUES (
        v_key,
        v_email,
        v_ip,
        v_now,
        v_new_cooldown
    );


    -- -----------------------------------------------------
    -- REQUEST ALLOWED
    -- -----------------------------------------------------

    RETURN QUERY
    SELECT
        TRUE,
        0,
        'OTP request allowed.'::TEXT;

    RETURN;

END;

$function$;


-- =========================================================
-- 3. FUNCTION OWNERSHIP AND PERMISSIONS
-- =========================================================

ALTER FUNCTION public.check_and_record_otp_rate_limit(
    TEXT,
    TEXT,
    INTEGER
) OWNER TO postgres;

REVOKE EXECUTE ON FUNCTION public.check_and_record_otp_rate_limit(
    TEXT,
    TEXT,
    INTEGER
)
FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.check_and_record_otp_rate_limit(
    TEXT,
    TEXT,
    INTEGER
)
TO service_role, postgres;


-- =========================================================
-- 4. EXPIRED EVENT CLEANUP FUNCTION
-- This function does NOT run automatically.
-- It must be called by a trusted scheduler if desired.
-- =========================================================

CREATE OR REPLACE FUNCTION public.purge_expired_otp_rate_limit_events()

RETURNS BIGINT

LANGUAGE plpgsql

SECURITY DEFINER

SET search_path = ''

AS $cleanup$

DECLARE
    v_deleted BIGINT;

BEGIN

    DELETE FROM public.otp_rate_limit_events

    WHERE requested_at <=
        pg_catalog.clock_timestamp() - INTERVAL '24 hours';

    GET DIAGNOSTICS v_deleted = ROW_COUNT;

    RETURN v_deleted;

END;

$cleanup$;


ALTER FUNCTION public.purge_expired_otp_rate_limit_events()
    OWNER TO postgres;

REVOKE EXECUTE ON FUNCTION public.purge_expired_otp_rate_limit_events()
    FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.purge_expired_otp_rate_limit_events()
    TO service_role, postgres;


COMMIT;
