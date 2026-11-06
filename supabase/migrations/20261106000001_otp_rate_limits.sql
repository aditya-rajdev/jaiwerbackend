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
GRANT ALL ON public.otp_rate_limits TO service_role, postgres;

-- 3. Atomic PostgreSQL RPC Function with Advisory Transaction Lock
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
SET search_path = public, pg_temp
AS $$
DECLARE
    v_key TEXT;
    v_now TIMESTAMPTZ := now();
    v_rec public.otp_rate_limits%ROWTYPE;
    v_cooldown_schedule INT[] := ARRAY[60, 120, 180, 300, 900];
    v_cooldown_secs INT;
    v_new_attempt INT;
    v_new_daily INT;
    v_reset_at TIMESTAMPTZ;
BEGIN
    v_key := 'otp:' || lower(trim(p_email)) || ':' || trim(p_client_ip);

    -- Transactional Advisory Lock on string hash guarantees 100% atomic mutual exclusion
    -- even for first requests before the row exists in database.
    PERFORM pg_advisory_xact_lock(hashtext(v_key));

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
            v_key, lower(trim(p_email)), trim(p_client_ip), 1, 1,
            v_now, v_now + INTERVAL '60 seconds', v_now + INTERVAL '24 hours'
        );

        RETURN QUERY SELECT true, 0, 'OK'::TEXT;
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
        v_cooldown_secs := GREATEST(EXTRACT(EPOCH FROM (v_reset_at - v_now))::INT, 60);
        RETURN QUERY SELECT false, v_cooldown_secs, ('Daily OTP limit reached. Please wait ' || v_cooldown_secs || ' seconds before requesting another code.')::TEXT;
        RETURN;
    END IF;

    -- 2. Enforce Active Cooldown
    IF v_now < v_rec.cooldown_until THEN
        v_cooldown_secs := GREATEST(EXTRACT(EPOCH FROM (v_rec.cooldown_until - v_now))::INT, 1);
        RETURN QUERY SELECT false, v_cooldown_secs, ('Too many requests. Please wait ' || v_cooldown_secs || ' seconds before retrying.')::TEXT;
        RETURN;
    END IF;

    -- 3. Progressive Cooldown
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

    RETURN QUERY SELECT true, 0, 'OK'::TEXT;
    RETURN;
END;
$$;

-- 4. RPC Function Permissions
REVOKE EXECUTE ON FUNCTION public.check_and_record_otp_rate_limit(TEXT, TEXT, INT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.check_and_record_otp_rate_limit(TEXT, TEXT, INT) TO service_role, postgres;
