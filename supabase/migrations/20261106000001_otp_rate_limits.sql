-- ----------------------------------------------------------------------------
-- Migration: 20261106000001_otp_rate_limits.sql
-- Table, Security Policies & Atomic RPC Function for Persistent OTP Rate Limiting
-- ----------------------------------------------------------------------------

-- 1. Table DDL
CREATE TABLE IF NOT EXISTS public.otp_rate_limits (
    key TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    client_ip TEXT NOT NULL,
    attempt_count INT NOT NULL DEFAULT 0,
    daily_count INT NOT NULL DEFAULT 0,
    last_request_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cooldown_until TIMESTAMPTZ NOT NULL DEFAULT now(),
    daily_reset_at TIMESTAMPTZ NOT NULL DEFAULT (now() + INTERVAL '24 hours'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_otp_rate_limits_email_ip ON public.otp_rate_limits(email, client_ip);

-- 2. Row Level Security & Table Permissions
ALTER TABLE public.otp_rate_limits ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.otp_rate_limits FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.otp_rate_limits TO service_role, postgres;

-- 3. Atomic PostgreSQL RPC Function with Advisory Transaction Lock & Schema Qualification
CREATE OR REPLACE FUNCTION public.check_and_record_otp_rate_limit(
    p_email TEXT,
    p_client_ip TEXT,
    p_max_daily INT DEFAULT 10
)
RETURNS TABLE (
    is_allowed BOOLEAN,
    retry_after_seconds INT,
    detail_message TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_key pg_catalog.text;
    v_now pg_catalog.timestamptz := pg_catalog.now();
    v_rec public.otp_rate_limits%ROWTYPE;
    v_cooldown_schedule pg_catalog.int4[] := ARRAY[60, 120, 180, 300, 900];
    v_cooldown_secs pg_catalog.int4;
    v_new_attempt pg_catalog.int4;
    v_new_daily pg_catalog.int4;
    v_reset_at pg_catalog.timestamptz;
BEGIN
    -- Input Validation: Length and Format Checks
    IF p_email IS NULL OR pg_catalog.length(pg_catalog.trim(p_email)) < 3 OR pg_catalog.length(pg_catalog.trim(p_email)) > 255 OR pg_catalog.trim(p_email) NOT LIKE '%@%' THEN
        RAISE EXCEPTION 'p_email must be a valid email address between 3 and 255 characters' USING ERRCODE = '22000';
    END IF;

    IF p_client_ip IS NULL OR pg_catalog.length(pg_catalog.trim(p_client_ip)) < 3 OR pg_catalog.length(pg_catalog.trim(p_client_ip)) > 45 THEN
        RAISE EXCEPTION 'p_client_ip must be a valid IP address between 3 and 45 characters' USING ERRCODE = '22000';
    END IF;

    IF p_max_daily IS NULL OR p_max_daily <= 0 THEN
        RAISE EXCEPTION 'p_max_daily must be greater than zero' USING ERRCODE = '22000';
    END IF;

    v_key := 'otp:' || pg_catalog.lower(pg_catalog.trim(p_email)) || ':' || pg_catalog.trim(p_client_ip);

    -- Transactional Advisory Lock on string hash guarantees 100% atomic mutual exclusion in PostgreSQL
    -- memory before querying or creating table rows, preventing first-request race conditions.
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext(v_key));

    -- Lock existing row if present
    SELECT * INTO v_rec
    FROM public.otp_rate_limits
    WHERE key = v_key
    FOR UPDATE;

    IF NOT FOUND THEN
        INSERT INTO public.otp_rate_limits (
            key, email, client_ip, attempt_count, daily_count,
            last_request_at, cooldown_until, daily_reset_at
        ) VALUES (
            v_key, pg_catalog.lower(pg_catalog.trim(p_email)), pg_catalog.trim(p_client_ip), 1, 1,
            v_now, v_now + INTERVAL '60 seconds', v_now + INTERVAL '24 hours'
        );

        RETURN QUERY SELECT true, 0, 'OK'::pg_catalog.text;
        RETURN;
    END IF;

    -- Reset daily window if 24 hours have elapsed
    IF v_now >= v_rec.daily_reset_at THEN
        v_new_daily := 0;
        v_new_attempt := 0;
        v_reset_at := v_now + INTERVAL '24 hours';
    ELSE
        v_new_daily := v_rec.daily_count;
        v_new_attempt := v_rec.attempt_count;
        v_reset_at := v_rec.daily_reset_at;
    END IF;

    -- 1. Enforce Daily Request Limit
    IF v_new_daily >= p_max_daily THEN
        v_cooldown_secs := pg_catalog.greatest(pg_catalog.extract(EPOCH FROM (v_reset_at - v_now))::pg_catalog.int4, 60);
        RETURN QUERY SELECT false, v_cooldown_secs, ('Daily OTP limit reached. Please wait ' || v_cooldown_secs || ' seconds before requesting another code.')::pg_catalog.text;
        RETURN;
    END IF;

    -- 2. Enforce Active Cooldown
    IF v_now < v_rec.cooldown_until THEN
        v_cooldown_secs := pg_catalog.greatest(pg_catalog.extract(EPOCH FROM (v_rec.cooldown_until - v_now))::pg_catalog.int4, 1);
        RETURN QUERY SELECT false, v_cooldown_secs, ('Too many requests. Please wait ' || v_cooldown_secs || ' seconds before retrying.')::pg_catalog.text;
        RETURN;
    END IF;

    -- 3. Progressive Cooldown Schedule: Attempt 1=60s, 2=120s, 3=180s, 4=300s, 5=900s, 6+=3600s
    v_new_attempt := v_new_attempt + 1;
    v_new_daily := v_new_daily + 1;

    IF v_new_attempt <= 5 THEN
        v_cooldown_secs := v_cooldown_schedule[v_new_attempt];
    ELSE
        v_cooldown_secs := 3600; -- 1 hour for attempt 6+
    END IF;

    UPDATE public.otp_rate_limits
    SET attempt_count = v_new_attempt,
        daily_count = v_new_daily,
        last_request_at = v_now,
        cooldown_until = v_now + (v_cooldown_secs || ' seconds')::INTERVAL,
        daily_reset_at = v_reset_at,
        updated_at = v_now
    WHERE key = v_key;

    RETURN QUERY SELECT true, 0, 'OK'::pg_catalog.text;
    RETURN;
END;
$$;

-- 4. RPC Function Permissions
REVOKE EXECUTE ON FUNCTION public.check_and_record_otp_rate_limit(TEXT, TEXT, INT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.check_and_record_otp_rate_limit(TEXT, TEXT, INT) TO service_role, postgres;
