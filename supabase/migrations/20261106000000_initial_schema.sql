-- ============================================================================
-- JAIWER BACKEND COMPLETE DATABASE MIGRATION SCRIPT (HARDENED v4.6.2 RE-RUNNABLE)
-- Target Database: Supabase PostgreSQL (Fresh / Independent Instance)
-- ============================================================================

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================================
-- 1. REUSABLE TRIGGER FUNCTIONS
-- ============================================================================

-- Trigger function to update 'updated_at' column automatically
CREATE OR REPLACE FUNCTION public.handle_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

-- ============================================================================
-- 2. TABLES DEFINITIONS WITH COMPOSITE RELATIONAL INTEGRITY CONSTRAINTS
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Table 1: user_profiles
-- Stores owner and worker profiles linked to auth.users
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.user_profiles (
    user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    account_type TEXT NOT NULL CHECK (account_type IN ('owner', 'worker')),
    owner_reference_id UUID DEFAULT gen_random_uuid(),
    shop_name TEXT NULL,
    shop_address TEXT NULL,
    city TEXT NULL,
    state TEXT NULL,
    pincode VARCHAR(6) NULL,
    gstin VARCHAR(15) NULL,
    shop_logo_url TEXT NULL,
    receipt_footer TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Prevent non-service_role updates to privileged profile columns
CREATE OR REPLACE FUNCTION public.enforce_user_profile_security()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    -- Force owner_reference_id to NULL for worker accounts
    IF NEW.account_type = 'worker' THEN
        NEW.owner_reference_id := NULL;
    END IF;

    -- On UPDATE, prevent non-service_role users from altering privileged fields
    IF TG_OP = 'UPDATE' AND COALESCE(auth.role(), '') <> 'service_role' THEN
        IF NEW.user_id <> OLD.user_id THEN
            RAISE EXCEPTION 'Cannot modify user_id.' USING ERRCODE = '42501';
        END IF;

        IF NEW.account_type <> OLD.account_type THEN
            RAISE EXCEPTION 'Cannot change account_type after profile creation.' USING ERRCODE = '42501';
        END IF;

        IF NEW.owner_reference_id IS DISTINCT FROM OLD.owner_reference_id THEN
            RAISE EXCEPTION 'Cannot modify owner_reference_id.' USING ERRCODE = '42501';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_user_profiles_updated_at ON public.user_profiles;
CREATE TRIGGER trg_user_profiles_updated_at
    BEFORE UPDATE ON public.user_profiles
    FOR EACH ROW EXECUTE FUNCTION public.handle_updated_at();

DROP TRIGGER IF EXISTS trg_user_profiles_security ON public.user_profiles;
CREATE TRIGGER trg_user_profiles_security
    BEFORE INSERT OR UPDATE ON public.user_profiles
    FOR EACH ROW EXECUTE FUNCTION public.enforce_user_profile_security();

-- ----------------------------------------------------------------------------
-- Table 2: shop_memberships
-- Associates workers with shop owners (One worker = One assigned shop)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.shop_memberships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL REFERENCES public.user_profiles(user_id) ON DELETE CASCADE,
    worker_user_id UUID NOT NULL UNIQUE REFERENCES public.user_profiles(user_id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'approved' CHECK (status IN ('pending', 'approved', 'rejected')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ NULL,
    CONSTRAINT check_different_users CHECK (owner_user_id <> worker_user_id)
);

-- Validation trigger for shop membership roles and self-assignment
CREATE OR REPLACE FUNCTION public.validate_shop_membership()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_owner_account_type TEXT;
    v_worker_account_type TEXT;
BEGIN
    IF NEW.owner_user_id = NEW.worker_user_id THEN
        RAISE EXCEPTION 'An owner cannot add themselves as a worker.' USING ERRCODE = '22000';
    END IF;

    SELECT account_type INTO v_owner_account_type
    FROM public.user_profiles WHERE user_id = NEW.owner_user_id;

    IF v_owner_account_type IS NULL OR v_owner_account_type <> 'owner' THEN
        RAISE EXCEPTION 'The specified owner_user_id is not an owner account.' USING ERRCODE = '22000';
    END IF;

    SELECT account_type INTO v_worker_account_type
    FROM public.user_profiles WHERE user_id = NEW.worker_user_id;

    IF v_worker_account_type IS NULL OR v_worker_account_type <> 'worker' THEN
        RAISE EXCEPTION 'The specified worker_user_id is not a worker account.' USING ERRCODE = '22000';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_shop_membership ON public.shop_memberships;
CREATE TRIGGER trg_validate_shop_membership
    BEFORE INSERT OR UPDATE ON public.shop_memberships
    FOR EACH ROW EXECUTE FUNCTION public.validate_shop_membership();

-- ----------------------------------------------------------------------------
-- Table 3: customers
-- Global customer profiles
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.customers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    phone_number TEXT NOT NULL UNIQUE,
    address TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_customers_updated_at ON public.customers;
CREATE TRIGGER trg_customers_updated_at
    BEFORE UPDATE ON public.customers
    FOR EACH ROW EXECUTE FUNCTION public.handle_updated_at();

-- ----------------------------------------------------------------------------
-- Table 4: customer_items
-- Purchased items per customer for a specific shop owner
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.customer_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES public.customers(id) ON DELETE CASCADE,
    owner_user_id UUID NOT NULL REFERENCES public.user_profiles(user_id) ON DELETE CASCADE,
    item_name TEXT NOT NULL,
    weight_grams NUMERIC(10, 3) NOT NULL CHECK (weight_grams > 0),
    rate_per_gram NUMERIC(12, 2) NULL CHECK (rate_per_gram IS NULL OR rate_per_gram >= 0),
    total_amount NUMERIC(12, 2) NOT NULL CHECK (total_amount > 0),
    amount_paid NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (amount_paid >= 0 AND amount_paid <= total_amount),
    due_amount NUMERIC(12, 2) GENERATED ALWAYS AS (total_amount - amount_paid) STORED,
    receiver TEXT NULL,
    reminder_date DATE NULL,
    sms_reminder_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_customer_item_owner UNIQUE (id, owner_user_id)
);

-- ----------------------------------------------------------------------------
-- Table 5: customer_transactions
-- Customer payment transactions
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.customer_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    receipt_number TEXT NOT NULL UNIQUE,
    customer_id UUID NOT NULL REFERENCES public.customers(id) ON DELETE CASCADE,
    owner_user_id UUID NOT NULL REFERENCES public.user_profiles(user_id) ON DELETE CASCADE,
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    payment_method TEXT NOT NULL CHECK (payment_method IN ('cash', 'upi', 'card', 'bank_transfer')),
    transaction_date TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_customer_tx_owner UNIQUE (id, owner_user_id)
);

-- ----------------------------------------------------------------------------
-- Table 6: customer_payment_allocations (COMPOSITE FOREIGN KEYS)
-- Breakdown of customer payment per item with strict shop owner matching
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.customer_payment_allocations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id UUID NOT NULL,
    item_id UUID NOT NULL,
    owner_user_id UUID NOT NULL REFERENCES public.user_profiles(user_id) ON DELETE CASCADE,
    allocated_amount NUMERIC(12, 2) NOT NULL CHECK (allocated_amount > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_customer_alloc_tx FOREIGN KEY (transaction_id, owner_user_id) REFERENCES public.customer_transactions(id, owner_user_id) ON DELETE CASCADE,
    CONSTRAINT fk_customer_alloc_item FOREIGN KEY (item_id, owner_user_id) REFERENCES public.customer_items(id, owner_user_id) ON DELETE CASCADE
);

-- ----------------------------------------------------------------------------
-- Table 7: suppliers
-- Supplier profiles per shop
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.suppliers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL REFERENCES public.user_profiles(user_id) ON DELETE CASCADE,
    supplier_name TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    address TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT unique_owner_supplier_phone UNIQUE (owner_user_id, phone_number),
    CONSTRAINT uq_supplier_owner UNIQUE (id, owner_user_id)
);

-- ----------------------------------------------------------------------------
-- Table 8: supplier_items
-- Purchased items from suppliers per shop
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.supplier_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id UUID NOT NULL,
    owner_user_id UUID NOT NULL REFERENCES public.user_profiles(user_id) ON DELETE CASCADE,
    item_name TEXT NOT NULL,
    weight_grams NUMERIC(10, 3) NULL CHECK (weight_grams IS NULL OR weight_grams > 0),
    rate_per_gram NUMERIC(12, 2) NULL CHECK (rate_per_gram IS NULL OR rate_per_gram >= 0),
    total_amount NUMERIC(12, 2) NOT NULL CHECK (total_amount > 0),
    amount_paid NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (amount_paid >= 0 AND amount_paid <= total_amount),
    due_amount NUMERIC(12, 2) GENERATED ALWAYS AS (total_amount - amount_paid) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_supplier_item_owner UNIQUE (id, owner_user_id),
    CONSTRAINT fk_supplier_items_supplier FOREIGN KEY (supplier_id, owner_user_id) REFERENCES public.suppliers(id, owner_user_id) ON DELETE CASCADE
);

-- ----------------------------------------------------------------------------
-- Table 9: supplier_transactions
-- Supplier payment records
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.supplier_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id UUID NOT NULL,
    owner_user_id UUID NOT NULL REFERENCES public.user_profiles(user_id) ON DELETE CASCADE,
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    payment_method TEXT NOT NULL CHECK (payment_method IN ('cash', 'upi', 'card', 'bank_transfer')),
    transaction_date TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_supplier_tx_owner UNIQUE (id, owner_user_id),
    CONSTRAINT fk_supplier_tx_supplier FOREIGN KEY (supplier_id, owner_user_id) REFERENCES public.suppliers(id, owner_user_id) ON DELETE CASCADE
);

-- ----------------------------------------------------------------------------
-- Table 10: supplier_payment_allocations (COMPOSITE FOREIGN KEYS)
-- Breakdown of supplier payments per item with strict shop owner matching
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.supplier_payment_allocations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id UUID NOT NULL,
    item_id UUID NOT NULL,
    owner_user_id UUID NOT NULL REFERENCES public.user_profiles(user_id) ON DELETE CASCADE,
    allocated_amount NUMERIC(12, 2) NOT NULL CHECK (allocated_amount > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_supplier_alloc_tx FOREIGN KEY (transaction_id, owner_user_id) REFERENCES public.supplier_transactions(id, owner_user_id) ON DELETE CASCADE,
    CONSTRAINT fk_supplier_alloc_item FOREIGN KEY (item_id, owner_user_id) REFERENCES public.supplier_items(id, owner_user_id) ON DELETE CASCADE
);

-- ----------------------------------------------------------------------------
-- Table 11: market_rate_snapshots
-- Gold and silver live rate snapshots
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.market_rate_snapshots (
    metal TEXT PRIMARY KEY CHECK (metal IN ('gold', 'silver')),
    market_date DATE NOT NULL,
    source_generated_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    rates JSONB NOT NULL
);

-- ----------------------------------------------------------------------------
-- Table 12: market_rate_refresh_runs
-- Logs for background rate refresh execution
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.market_rate_refresh_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    success BOOLEAN NOT NULL DEFAULT FALSE,
    error_message TEXT NULL
);

-- ----------------------------------------------------------------------------
-- Table 13: idempotency_logs
-- Atomic logs for payment request deduplication and concurrency safety
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.idempotency_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL REFERENCES public.user_profiles(user_id) ON DELETE CASCADE,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed')),
    response_payload JSONB NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT unique_owner_operation_key UNIQUE (owner_user_id, operation, idempotency_key)
);

-- ============================================================================
-- 3. INDEXES
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_user_profiles_phone ON public.user_profiles(phone_number);
CREATE INDEX IF NOT EXISTS idx_user_profiles_email ON public.user_profiles(email);

CREATE INDEX IF NOT EXISTS idx_shop_memberships_owner ON public.shop_memberships(owner_user_id);

CREATE INDEX IF NOT EXISTS idx_customers_phone ON public.customers(phone_number);

CREATE INDEX IF NOT EXISTS idx_customer_items_customer_owner ON public.customer_items(customer_id, owner_user_id);
CREATE INDEX IF NOT EXISTS idx_customer_items_owner ON public.customer_items(owner_user_id);

CREATE INDEX IF NOT EXISTS idx_customer_tx_customer_owner ON public.customer_transactions(customer_id, owner_user_id);
CREATE INDEX IF NOT EXISTS idx_customer_tx_date ON public.customer_transactions(transaction_date DESC);

CREATE INDEX IF NOT EXISTS idx_customer_allocations_tx ON public.customer_payment_allocations(transaction_id);

CREATE INDEX IF NOT EXISTS idx_suppliers_owner ON public.suppliers(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_supplier_items_supplier_owner ON public.supplier_items(supplier_id, owner_user_id);
CREATE INDEX IF NOT EXISTS idx_supplier_tx_supplier_owner ON public.supplier_transactions(supplier_id, owner_user_id);
CREATE INDEX IF NOT EXISTS idx_supplier_allocations_tx ON public.supplier_payment_allocations(transaction_id);

CREATE INDEX IF NOT EXISTS idx_idempotency_logs_owner_op ON public.idempotency_logs(owner_user_id, operation, idempotency_key);

-- ============================================================================
-- 4. SECURITY HELPERS & RLS POLICIES (NON-RECURSIVE)
-- ============================================================================

-- Non-recursive helper to check if a user is an owner
CREATE OR REPLACE FUNCTION public.is_owner(p_user_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.user_profiles
        WHERE user_id = p_user_id AND account_type = 'owner'
    );
$$;

-- Security helper function to check shop authorization
CREATE OR REPLACE FUNCTION public.is_authorized_for_shop(p_owner_user_id UUID)
RETURNS BOOLEAN
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_owner_account_type TEXT;
    v_caller_account_type TEXT;
BEGIN
    -- Only allow explicit service_role requests
    IF auth.role() = 'service_role' THEN
        RETURN TRUE;
    END IF;

    -- Reject anonymous or unauthenticated requests
    IF auth.uid() IS NULL OR auth.role() IS NULL OR auth.role() = 'anon' THEN
        RETURN FALSE;
    END IF;

    -- Verify target owner_user_id is an owner account
    SELECT account_type INTO v_owner_account_type
    FROM public.user_profiles WHERE user_id = p_owner_user_id;

    IF v_owner_account_type IS NULL OR v_owner_account_type <> 'owner' THEN
        RETURN FALSE;
    END IF;

    -- Direct shop owner access
    IF auth.uid() = p_owner_user_id THEN
        RETURN TRUE;
    END IF;

    -- Approved worker access
    SELECT account_type INTO v_caller_account_type
    FROM public.user_profiles WHERE user_id = auth.uid();

    IF v_caller_account_type = 'worker' AND EXISTS (
        SELECT 1 FROM public.shop_memberships
        WHERE owner_user_id = p_owner_user_id
          AND worker_user_id = auth.uid()
          AND status = 'approved'
    ) THEN
        RETURN TRUE;
    END IF;

    RETURN FALSE;
END;
$$;

ALTER TABLE public.user_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.shop_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.customer_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.customer_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.customer_payment_allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.suppliers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.supplier_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.supplier_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.supplier_payment_allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.market_rate_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.market_rate_refresh_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.idempotency_logs ENABLE ROW LEVEL SECURITY;

-- User Profiles: Self access, or owner/worker reading each other's profile via approved shop_memberships
DROP POLICY IF EXISTS user_profiles_select ON public.user_profiles;
CREATE POLICY user_profiles_select ON public.user_profiles
    FOR SELECT USING (
        auth.role() = 'service_role' OR
        auth.uid() = user_id OR
        EXISTS (
            SELECT 1 FROM public.shop_memberships
            WHERE (owner_user_id = auth.uid() AND worker_user_id = user_id AND status = 'approved')
               OR (worker_user_id = auth.uid() AND owner_user_id = user_id AND status = 'approved')
        )
    );

DROP POLICY IF EXISTS user_profiles_update ON public.user_profiles;
CREATE POLICY user_profiles_update ON public.user_profiles
    FOR UPDATE USING (auth.uid() = user_id);

DROP POLICY IF EXISTS user_profiles_insert ON public.user_profiles;
CREATE POLICY user_profiles_insert ON public.user_profiles
    FOR INSERT WITH CHECK (auth.uid() = user_id);

-- Shop Memberships: Owner or Worker select, Owner insert/update/delete
DROP POLICY IF EXISTS shop_memberships_select ON public.shop_memberships;
CREATE POLICY shop_memberships_select ON public.shop_memberships
    FOR SELECT USING (
        auth.role() = 'service_role' OR
        auth.uid() = owner_user_id OR
        auth.uid() = worker_user_id
    );

DROP POLICY IF EXISTS shop_memberships_insert_update_delete ON public.shop_memberships;
CREATE POLICY shop_memberships_insert_update_delete ON public.shop_memberships
    FOR ALL USING (
        auth.role() = 'service_role' OR
        (auth.uid() = owner_user_id AND public.is_owner(auth.uid()))
    );

-- Customers: Scoped to shops where the customer has items
DROP POLICY IF EXISTS customers_select ON public.customers;
CREATE POLICY customers_select ON public.customers
    FOR SELECT USING (
        auth.role() = 'service_role' OR
        EXISTS (
            SELECT 1 FROM public.customer_items ci
            WHERE ci.customer_id = customers.id AND public.is_authorized_for_shop(ci.owner_user_id)
        )
    );

-- Customer Items, Transactions & Allocations: Shop isolation via is_authorized_for_shop
DROP POLICY IF EXISTS customer_items_policy ON public.customer_items;
CREATE POLICY customer_items_policy ON public.customer_items
    FOR ALL USING (public.is_authorized_for_shop(owner_user_id));

DROP POLICY IF EXISTS customer_transactions_policy ON public.customer_transactions;
CREATE POLICY customer_transactions_policy ON public.customer_transactions
    FOR ALL USING (public.is_authorized_for_shop(owner_user_id));

DROP POLICY IF EXISTS customer_payment_allocations_policy ON public.customer_payment_allocations;
CREATE POLICY customer_payment_allocations_policy ON public.customer_payment_allocations
    FOR ALL USING (public.is_authorized_for_shop(owner_user_id));

-- Suppliers, Supplier Items & Transactions: Shop isolation via is_authorized_for_shop
DROP POLICY IF EXISTS suppliers_policy ON public.suppliers;
CREATE POLICY suppliers_policy ON public.suppliers
    FOR ALL USING (public.is_authorized_for_shop(owner_user_id));

DROP POLICY IF EXISTS supplier_items_policy ON public.supplier_items;
CREATE POLICY supplier_items_policy ON public.supplier_items
    FOR ALL USING (public.is_authorized_for_shop(owner_user_id));

DROP POLICY IF EXISTS supplier_transactions_policy ON public.supplier_transactions;
CREATE POLICY supplier_transactions_policy ON public.supplier_transactions
    FOR ALL USING (public.is_authorized_for_shop(owner_user_id));

DROP POLICY IF EXISTS supplier_payment_allocations_policy ON public.supplier_payment_allocations;
CREATE POLICY supplier_payment_allocations_policy ON public.supplier_payment_allocations
    FOR ALL USING (public.is_authorized_for_shop(owner_user_id));

-- Market Rates: Read access for authenticated users
DROP POLICY IF EXISTS market_rates_read ON public.market_rate_snapshots;
CREATE POLICY market_rates_read ON public.market_rate_snapshots
    FOR SELECT USING (auth.role() = 'authenticated' OR auth.role() = 'service_role');

-- Idempotency Logs: Restricted to authorized shop owner
DROP POLICY IF EXISTS idempotency_logs_policy ON public.idempotency_logs;
CREATE POLICY idempotency_logs_policy ON public.idempotency_logs
    FOR ALL USING (public.is_authorized_for_shop(owner_user_id));

-- ============================================================================
-- 5. HARDENED STORED PROCEDURES (RPCs WITH ATOMIC CONCURRENCY IDEMPOTENCY)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. create_customer_with_items_and_payment
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.create_customer_with_items_and_payment(
    p_name TEXT,
    p_phone_number TEXT,
    p_address TEXT,
    p_owner_user_id UUID,
    p_payment_mode TEXT,
    p_payment_method TEXT,
    p_total_payment NUMERIC,
    p_items JSONB
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_customer_id UUID;
    v_customer_rec RECORD;
    v_item JSONB;
    v_item_rec RECORD;
    v_inserted_items JSONB := '[]'::jsonb;
    v_transaction_id UUID := NULL;
    v_receipt_number TEXT;
    v_total_paid_accum NUMERIC := 0;
    v_purchase_total NUMERIC := 0;
    v_item_paid NUMERIC;
    v_item_total NUMERIC;
    v_remaining_payment NUMERIC;
    v_alloc NUMERIC;
BEGIN
    IF NOT public.is_authorized_for_shop(p_owner_user_id) THEN
        RAISE EXCEPTION 'Permission denied for this shop.' USING ERRCODE = '42501';
    END IF;

    IF p_payment_mode NOT IN ('item_wise', 'total') THEN
        RAISE EXCEPTION 'Invalid payment mode.' USING ERRCODE = '22000';
    END IF;

    IF p_items IS NULL OR jsonb_typeof(p_items) <> 'array' OR jsonb_array_length(p_items) = 0 THEN
        RAISE EXCEPTION 'Customer items array cannot be empty or null.' USING ERRCODE = '22000';
    END IF;

    IF EXISTS (SELECT 1 FROM public.customers WHERE phone_number = p_phone_number) THEN
        RAISE EXCEPTION 'A customer with this phone number already exists.' USING ERRCODE = '23505';
    END IF;

    FOR v_item IN SELECT * FROM jsonb_array_elements(p_items)
    LOOP
        v_purchase_total := v_purchase_total + (v_item->>'total_amount')::NUMERIC;
    END LOOP;

    INSERT INTO public.customers (name, phone_number, address)
    VALUES (p_name, p_phone_number, p_address)
    RETURNING id, name, phone_number, address, created_at INTO v_customer_rec;
    v_customer_id := v_customer_rec.id;

    IF p_payment_mode = 'total' THEN
        v_total_paid_accum := COALESCE(p_total_payment, 0);
        IF v_total_paid_accum > v_purchase_total THEN
            RAISE EXCEPTION 'Payment cannot exceed total purchase amount.' USING ERRCODE = '23514';
        END IF;
        v_remaining_payment := v_total_paid_accum;
    END IF;

    FOR v_item IN SELECT * FROM jsonb_array_elements(p_items)
    LOOP
        v_item_total := (v_item->>'total_amount')::NUMERIC;
        IF p_payment_mode = 'item_wise' THEN
            v_item_paid := COALESCE((v_item->>'amount_paid')::NUMERIC, 0);
            IF v_item_paid > v_item_total THEN
                RAISE EXCEPTION 'Item payment exceeds total amount.' USING ERRCODE = '23514';
            END IF;
            v_total_paid_accum := v_total_paid_accum + v_item_paid;
        ELSE
            v_alloc := LEAST(v_remaining_payment, v_item_total);
            v_item_paid := v_alloc;
            v_remaining_payment := v_remaining_payment - v_alloc;
        END IF;

        INSERT INTO public.customer_items (
            customer_id, owner_user_id, item_name, weight_grams, rate_per_gram,
            total_amount, amount_paid, reminder_date, sms_reminder_enabled
        ) VALUES (
            v_customer_id, p_owner_user_id, v_item->>'item_name', (v_item->>'weight_grams')::NUMERIC,
            (v_item->>'rate_per_gram')::NUMERIC, v_item_total, v_item_paid,
            (v_item->>'reminder_date')::DATE, COALESCE((v_item->>'sms_reminder_enabled')::BOOLEAN, false)
        )
        RETURNING * INTO v_item_rec;

        v_inserted_items := v_inserted_items || to_jsonb(v_item_rec);
    END LOOP;

    IF v_total_paid_accum > 0 THEN
        v_receipt_number := 'REC-' || to_char(now(), 'YYYYMMDD') || '-' || upper(substring(md5(random()::text) from 1 for 6));

        INSERT INTO public.customer_transactions (
            receipt_number, customer_id, owner_user_id, amount, payment_method
        ) VALUES (
            v_receipt_number, v_customer_id, p_owner_user_id, v_total_paid_accum, p_payment_method
        )
        RETURNING id INTO v_transaction_id;

        FOR v_item_rec IN SELECT * FROM jsonb_to_recordset(v_inserted_items) AS x(id UUID, amount_paid NUMERIC)
        LOOP
            IF v_item_rec.amount_paid > 0 THEN
                INSERT INTO public.customer_payment_allocations (transaction_id, item_id, owner_user_id, allocated_amount)
                VALUES (v_transaction_id, v_item_rec.id, p_owner_user_id, v_item_rec.amount_paid);
            END IF;
        END LOOP;
    END IF;

    RETURN jsonb_build_object(
        'customer', to_jsonb(v_customer_rec),
        'items', v_inserted_items,
        'transaction_id', v_transaction_id,
        'payment_amount', v_total_paid_accum
    );
END;
$$;

-- ----------------------------------------------------------------------------
-- 2. add_items_for_existing_customer
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.add_items_for_existing_customer(
    p_phone_number TEXT,
    p_receiver TEXT,
    p_owner_user_id UUID,
    p_payment_mode TEXT,
    p_payment_method TEXT,
    p_total_payment NUMERIC,
    p_items JSONB
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_customer_rec RECORD;
    v_item JSONB;
    v_item_rec RECORD;
    v_inserted_items JSONB := '[]'::jsonb;
    v_transaction_id UUID := NULL;
    v_receipt_number TEXT;
    v_total_paid_accum NUMERIC := 0;
    v_purchase_total NUMERIC := 0;
    v_item_paid NUMERIC;
    v_item_total NUMERIC;
    v_remaining_payment NUMERIC;
    v_alloc NUMERIC;
BEGIN
    IF NOT public.is_authorized_for_shop(p_owner_user_id) THEN
        RAISE EXCEPTION 'Permission denied for this shop.' USING ERRCODE = '42501';
    END IF;

    IF p_payment_mode NOT IN ('item_wise', 'total') THEN
        RAISE EXCEPTION 'Invalid payment mode.' USING ERRCODE = '22000';
    END IF;

    IF p_items IS NULL OR jsonb_typeof(p_items) <> 'array' OR jsonb_array_length(p_items) = 0 THEN
        RAISE EXCEPTION 'Customer items array cannot be empty or null.' USING ERRCODE = '22000';
    END IF;

    SELECT * INTO v_customer_rec FROM public.customers WHERE phone_number = p_phone_number;
    IF v_customer_rec.id IS NULL THEN
        RAISE EXCEPTION 'Customer not found.' USING ERRCODE = 'P0002';
    END IF;

    FOR v_item IN SELECT * FROM jsonb_array_elements(p_items)
    LOOP
        v_purchase_total := v_purchase_total + (v_item->>'total_amount')::NUMERIC;
    END LOOP;

    IF p_payment_mode = 'total' THEN
        v_total_paid_accum := COALESCE(p_total_payment, 0);
        IF v_total_paid_accum > v_purchase_total THEN
            RAISE EXCEPTION 'Payment cannot exceed total purchase amount.' USING ERRCODE = '23514';
        END IF;
        v_remaining_payment := v_total_paid_accum;
    END IF;

    FOR v_item IN SELECT * FROM jsonb_array_elements(p_items)
    LOOP
        v_item_total := (v_item->>'total_amount')::NUMERIC;
        IF p_payment_mode = 'item_wise' THEN
            v_item_paid := COALESCE((v_item->>'amount_paid')::NUMERIC, 0);
            IF v_item_paid > v_item_total THEN
                RAISE EXCEPTION 'Item payment exceeds total amount.' USING ERRCODE = '23514';
            END IF;
            v_total_paid_accum := v_total_paid_accum + v_item_paid;
        ELSE
            v_alloc := LEAST(v_remaining_payment, v_item_total);
            v_item_paid := v_alloc;
            v_remaining_payment := v_remaining_payment - v_alloc;
        END IF;

        INSERT INTO public.customer_items (
            customer_id, owner_user_id, item_name, weight_grams, rate_per_gram,
            total_amount, amount_paid, receiver, reminder_date, sms_reminder_enabled
        ) VALUES (
            v_customer_rec.id, p_owner_user_id, v_item->>'item_name', (v_item->>'weight_grams')::NUMERIC,
            (v_item->>'rate_per_gram')::NUMERIC, v_item_total, v_item_paid, p_receiver,
            (v_item->>'reminder_date')::DATE, COALESCE((v_item->>'sms_reminder_enabled')::BOOLEAN, false)
        )
        RETURNING * INTO v_item_rec;

        v_inserted_items := v_inserted_items || to_jsonb(v_item_rec);
    END LOOP;

    IF v_total_paid_accum > 0 THEN
        v_receipt_number := 'REC-' || to_char(now(), 'YYYYMMDD') || '-' || upper(substring(md5(random()::text) from 1 for 6));

        INSERT INTO public.customer_transactions (
            receipt_number, customer_id, owner_user_id, amount, payment_method
        ) VALUES (
            v_receipt_number, v_customer_rec.id, p_owner_user_id, v_total_paid_accum, p_payment_method
        )
        RETURNING id INTO v_transaction_id;

        FOR v_item_rec IN SELECT * FROM jsonb_to_recordset(v_inserted_items) AS x(id UUID, amount_paid NUMERIC)
        LOOP
            IF v_item_rec.amount_paid > 0 THEN
                INSERT INTO public.customer_payment_allocations (transaction_id, item_id, owner_user_id, allocated_amount)
                VALUES (v_transaction_id, v_item_rec.id, p_owner_user_id, v_item_rec.amount_paid);
            END IF;
        END LOOP;
    END IF;

    RETURN jsonb_build_object(
        'customer', to_jsonb(v_customer_rec),
        'items', v_inserted_items,
        'transaction_id', v_transaction_id,
        'payment_amount', v_total_paid_accum
    );
END;
$$;

-- ----------------------------------------------------------------------------
-- 3. record_customer_payment (CONCURRENCY SAFE IDEMPOTENCY)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.record_customer_payment(
    p_customer_id UUID,
    p_owner_user_id UUID,
    p_payment_method TEXT,
    p_allocations JSONB,
    p_idempotency_key TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_receipt_number TEXT;
    v_transaction_rec RECORD;
    v_alloc JSONB;
    v_item_id UUID;
    v_alloc_amount NUMERIC;
    v_item_due NUMERIC;
    v_total_payment NUMERIC := 0;
    v_updated_items JSONB := '[]'::jsonb;
    v_created_allocations JSONB := '[]'::jsonb;
    v_item_rec RECORD;
    v_clean_key TEXT;
    v_payload_hash TEXT;
    v_existing_status TEXT;
    v_existing_hash TEXT;
    v_existing_response JSONB;
    v_result JSONB;
BEGIN
    IF NOT public.is_authorized_for_shop(p_owner_user_id) THEN
        RAISE EXCEPTION 'Permission denied for this shop.' USING ERRCODE = '42501';
    END IF;

    IF p_payment_method IS NULL OR p_payment_method NOT IN ('cash', 'upi', 'card', 'bank_transfer') THEN
        RAISE EXCEPTION 'Invalid or missing payment method.' USING ERRCODE = '22000';
    END IF;

    IF p_allocations IS NULL OR jsonb_typeof(p_allocations) <> 'array' OR jsonb_array_length(p_allocations) = 0 THEN
        RAISE EXCEPTION 'Payment allocations array cannot be empty or null.' USING ERRCODE = '22000';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM public.customers WHERE id = p_customer_id) THEN
        RAISE EXCEPTION 'Customer not found.' USING ERRCODE = 'P0002';
    END IF;

    -- Deep JSON & numeric validation
    FOR v_alloc IN SELECT * FROM jsonb_array_elements(p_allocations)
    LOOP
        IF jsonb_typeof(v_alloc) <> 'object' THEN
            RAISE EXCEPTION 'Each allocation must be a valid JSON object.' USING ERRCODE = '22000';
        END IF;

        IF (v_alloc->>'item_id') IS NULL OR (v_alloc->>'amount') IS NULL THEN
            RAISE EXCEPTION 'Allocation must contain non-null item_id and amount.' USING ERRCODE = '22000';
        END IF;

        BEGIN
            v_item_id := (v_alloc->>'item_id')::UUID;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION 'Invalid UUID format for item_id.' USING ERRCODE = '22000';
        END;

        BEGIN
            v_alloc_amount := (v_alloc->>'amount')::NUMERIC;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION 'Invalid numeric format for allocation amount.' USING ERRCODE = '22000';
        END;

        IF v_alloc_amount <= 0 THEN
            RAISE EXCEPTION 'Allocation amount must be greater than zero.' USING ERRCODE = '23514';
        END IF;

        v_total_payment := v_total_payment + v_alloc_amount;
    END LOOP;

    -- Reject duplicate item_ids
    IF (SELECT count(DISTINCT (v->>'item_id')::UUID) FROM jsonb_array_elements(p_allocations) v) <> jsonb_array_length(p_allocations) THEN
        RAISE EXCEPTION 'Duplicate item IDs in payment allocations are not allowed.' USING ERRCODE = '22000';
    END IF;

    -- ATOMIC CONCURRENCY-SAFE IDEMPOTENCY RESERVATION
    IF p_idempotency_key IS NOT NULL AND trim(p_idempotency_key) <> '' THEN
        v_clean_key := trim(p_idempotency_key);
        v_payload_hash := encode(digest(p_customer_id::text || ':' || p_payment_method || ':' || v_total_payment::text || ':' || p_allocations::text, 'sha256'), 'hex');

        -- 1. Insert reservation or update lock time atomically
        INSERT INTO public.idempotency_logs (
            owner_user_id, operation, idempotency_key, payload_hash, status
        ) VALUES (
            p_owner_user_id, 'record_customer_payment', v_clean_key, v_payload_hash, 'pending'
        )
        ON CONFLICT (owner_user_id, operation, idempotency_key) DO UPDATE
        SET updated_at = now();

        -- 2. Lock row to serialize concurrent duplicate requests
        SELECT status, payload_hash, response_payload
        INTO v_existing_status, v_existing_hash, v_existing_response
        FROM public.idempotency_logs
        WHERE owner_user_id = p_owner_user_id
          AND operation = 'record_customer_payment'
          AND idempotency_key = v_clean_key
        FOR UPDATE;

        -- 3. Handle completed replay
        IF v_existing_status = 'completed' THEN
            IF v_existing_hash = v_payload_hash THEN
                RETURN v_existing_response;
            ELSE
                RAISE EXCEPTION 'Idempotency key reuse with conflicting payload.' USING ERRCODE = '22000';
            END IF;
        END IF;
    END IF;

    -- Execute payment transaction
    v_receipt_number := 'REC-' || to_char(now(), 'YYYYMMDD') || '-' || upper(substring(md5(random()::text) from 1 for 6));
    INSERT INTO public.customer_transactions (
        receipt_number, customer_id, owner_user_id, amount, payment_method
    ) VALUES (
        v_receipt_number, p_customer_id, p_owner_user_id, v_total_payment, p_payment_method
    )
    RETURNING * INTO v_transaction_rec;

    -- Sort by item_id ASC to prevent deadlocks under concurrent multi-item payment requests
    FOR v_alloc IN SELECT value FROM jsonb_array_elements(p_allocations) ORDER BY (value->>'item_id')::UUID ASC
    LOOP
        v_item_id := (v_alloc->>'item_id')::UUID;
        v_alloc_amount := (v_alloc->>'amount')::NUMERIC;

        SELECT due_amount INTO v_item_due FROM public.customer_items
        WHERE id = v_item_id AND owner_user_id = p_owner_user_id AND customer_id = p_customer_id
        FOR UPDATE;

        IF v_item_due IS NULL THEN
            RAISE EXCEPTION 'Item not found for this customer and shop.' USING ERRCODE = 'P0002';
        END IF;

        IF v_alloc_amount > v_item_due THEN
            RAISE EXCEPTION 'Payment exceeds item outstanding balance.' USING ERRCODE = '23514';
        END IF;

        INSERT INTO public.customer_payment_allocations (transaction_id, item_id, owner_user_id, allocated_amount)
        VALUES (v_transaction_rec.id, v_item_id, p_owner_user_id, v_alloc_amount);

        UPDATE public.customer_items
        SET amount_paid = amount_paid + v_alloc_amount
        WHERE id = v_item_id
        RETURNING * INTO v_item_rec;

        v_updated_items := v_updated_items || to_jsonb(v_item_rec);
        v_created_allocations := v_created_allocations || jsonb_build_object('item_id', v_item_id, 'allocated_amount', v_alloc_amount);
    END LOOP;

    v_result := jsonb_build_object(
        'transaction', to_jsonb(v_transaction_rec),
        'payment_amount', v_total_payment,
        'items', v_updated_items,
        'allocations', v_created_allocations
    );

    -- Complete idempotency reservation
    IF p_idempotency_key IS NOT NULL AND trim(p_idempotency_key) <> '' THEN
        UPDATE public.idempotency_logs
        SET status = 'completed',
            response_payload = v_result
        WHERE owner_user_id = p_owner_user_id
          AND operation = 'record_customer_payment'
          AND idempotency_key = v_clean_key;
    END IF;

    RETURN v_result;
END;
$$;

-- ----------------------------------------------------------------------------
-- 4. get_owner_customers_summary
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_owner_customers_summary(
    p_owner_user_id UUID,
    p_filter TEXT DEFAULT 'all',
    p_search TEXT DEFAULT NULL,
    p_limit INT DEFAULT 20,
    p_offset INT DEFAULT 0
)
RETURNS TABLE (
    customer_id UUID,
    name TEXT,
    phone_number TEXT,
    total_paid_amount NUMERIC,
    total_due_amount NUMERIC,
    total_count BIGINT
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    IF NOT public.is_authorized_for_shop(p_owner_user_id) THEN
        RAISE EXCEPTION 'Permission denied for this shop.' USING ERRCODE = '42501';
    END IF;

    RETURN QUERY
    WITH summary AS (
        SELECT
            c.id AS customer_id,
            c.name,
            c.phone_number,
            COALESCE(SUM(ci.amount_paid), 0) AS total_paid_amount,
            COALESCE(SUM(ci.due_amount), 0) AS total_due_amount
        FROM public.customers c
        JOIN public.customer_items ci ON ci.customer_id = c.id
        WHERE ci.owner_user_id = p_owner_user_id
          AND (p_search IS NULL OR c.name ILIKE '%' || p_search || '%' OR c.phone_number ILIKE '%' || p_search || '%')
        GROUP BY c.id, c.name, c.phone_number
        HAVING
            (p_filter = 'all') OR
            (p_filter = 'baki' AND COALESCE(SUM(ci.due_amount), 0) > 0) OR
            (p_filter = 'jama' AND COALESCE(SUM(ci.due_amount), 0) = 0)
    )
    SELECT
        s.customer_id,
        s.name,
        s.phone_number,
        s.total_paid_amount,
        s.total_due_amount,
        COUNT(*) OVER() AS total_count
    FROM summary s
    ORDER BY s.name ASC
    LIMIT p_limit OFFSET p_offset;
END;
$$;

-- ----------------------------------------------------------------------------
-- 5. get_owner_transactions
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_owner_transactions(
    p_owner_user_id UUID,
    p_search TEXT DEFAULT NULL,
    p_payment_method TEXT DEFAULT NULL,
    p_start_date TEXT DEFAULT NULL,
    p_end_date TEXT DEFAULT NULL,
    p_limit INT DEFAULT 20,
    p_offset INT DEFAULT 0
)
RETURNS TABLE (
    transaction_id UUID,
    customer_id UUID,
    customer_name TEXT,
    customer_phone_number TEXT,
    amount NUMERIC,
    payment_method TEXT,
    transaction_date TIMESTAMPTZ,
    notes TEXT,
    total_count BIGINT,
    total_collected_amount NUMERIC
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    IF NOT public.is_authorized_for_shop(p_owner_user_id) THEN
        RAISE EXCEPTION 'Permission denied for this shop.' USING ERRCODE = '42501';
    END IF;

    RETURN QUERY
    WITH filtered_tx AS (
        SELECT
            ct.id AS transaction_id,
            ct.customer_id,
            c.name AS customer_name,
            c.phone_number AS customer_phone_number,
            ct.amount,
            ct.payment_method,
            ct.transaction_date,
            ct.notes
        FROM public.customer_transactions ct
        JOIN public.customers c ON c.id = ct.customer_id
        WHERE ct.owner_user_id = p_owner_user_id
          AND (p_search IS NULL OR c.name ILIKE '%' || p_search || '%' OR c.phone_number ILIKE '%' || p_search || '%')
          AND (p_payment_method IS NULL OR ct.payment_method = p_payment_method)
          AND (p_start_date IS NULL OR ct.transaction_date >= p_start_date::TIMESTAMPTZ)
          AND (p_end_date IS NULL OR ct.transaction_date <= p_end_date::TIMESTAMPTZ)
    )
    SELECT
        f.transaction_id,
        f.customer_id,
        f.customer_name,
        f.customer_phone_number,
        f.amount,
        f.payment_method,
        f.transaction_date,
        f.notes,
        COUNT(*) OVER() AS total_count,
        SUM(f.amount) OVER() AS total_collected_amount
    FROM filtered_tx f
    ORDER BY f.transaction_date DESC
    LIMIT p_limit OFFSET p_offset;
END;
$$;

-- ----------------------------------------------------------------------------
-- 6. get_owner_transaction_details
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_owner_transaction_details(
    p_transaction_id UUID,
    p_owner_user_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_tx_rec RECORD;
    v_allocations JSONB;
BEGIN
    IF NOT public.is_authorized_for_shop(p_owner_user_id) THEN
        RAISE EXCEPTION 'Permission denied for this shop.' USING ERRCODE = '42501';
    END IF;

    SELECT
        ct.id, ct.receipt_number, ct.customer_id, c.name AS customer_name,
        c.phone_number AS customer_phone_number, ct.amount, ct.payment_method,
        ct.transaction_date, ct.notes
    INTO v_tx_rec
    FROM public.customer_transactions ct
    JOIN public.customers c ON c.id = ct.customer_id
    WHERE ct.id = p_transaction_id AND ct.owner_user_id = p_owner_user_id;

    IF v_tx_rec.id IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'item_id', ci.id,
        'item_name', ci.item_name,
        'weight_grams', ci.weight_grams,
        'total_amount', ci.total_amount,
        'allocated_amount', cpa.allocated_amount
    )), '[]'::jsonb)
    INTO v_allocations
    FROM public.customer_payment_allocations cpa
    JOIN public.customer_items ci ON ci.id = cpa.item_id
    WHERE cpa.transaction_id = p_transaction_id AND cpa.owner_user_id = p_owner_user_id;

    RETURN jsonb_build_object(
        'transaction', to_jsonb(v_tx_rec),
        'allocations', v_allocations
    );
END;
$$;

-- ----------------------------------------------------------------------------
-- 7. create_supplier_with_items_and_payment
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.create_supplier_with_items_and_payment(
    p_owner_user_id UUID,
    p_supplier_name TEXT,
    p_supplier_phone TEXT,
    p_supplier_address TEXT,
    p_items JSONB,
    p_payment_mode TEXT,
    p_total_payment NUMERIC,
    p_payment_method TEXT,
    p_notes TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_supplier_id UUID;
    v_supplier_rec RECORD;
    v_item JSONB;
    v_item_rec RECORD;
    v_inserted_items JSONB := '[]'::jsonb;
    v_transaction_id UUID := NULL;
    v_total_paid_accum NUMERIC := 0;
    v_purchase_total NUMERIC := 0;
    v_item_paid NUMERIC;
    v_item_total NUMERIC;
    v_remaining_payment NUMERIC;
    v_alloc NUMERIC;
BEGIN
    IF NOT public.is_authorized_for_shop(p_owner_user_id) THEN
        RAISE EXCEPTION 'Permission denied for this shop.' USING ERRCODE = '42501';
    END IF;

    IF p_payment_mode NOT IN ('item_wise', 'total') THEN
        RAISE EXCEPTION 'Invalid payment mode.' USING ERRCODE = '22000';
    END IF;

    IF p_items IS NULL OR jsonb_typeof(p_items) <> 'array' OR jsonb_array_length(p_items) = 0 THEN
        RAISE EXCEPTION 'Supplier items array cannot be empty or null.' USING ERRCODE = '22000';
    END IF;

    FOR v_item IN SELECT * FROM jsonb_array_elements(p_items)
    LOOP
        v_purchase_total := v_purchase_total + (v_item->>'total_amount')::NUMERIC;
    END LOOP;

    INSERT INTO public.suppliers (owner_user_id, supplier_name, phone_number, address)
    VALUES (p_owner_user_id, p_supplier_name, p_supplier_phone, p_supplier_address)
    RETURNING * INTO v_supplier_rec;
    v_supplier_id := v_supplier_rec.id;

    IF p_payment_mode = 'total' THEN
        v_total_paid_accum := COALESCE(p_total_payment, 0);
        IF v_total_paid_accum > v_purchase_total THEN
            RAISE EXCEPTION 'Payment cannot exceed total purchase amount.' USING ERRCODE = '23514';
        END IF;
        v_remaining_payment := v_total_paid_accum;
    END IF;

    FOR v_item IN SELECT * FROM jsonb_array_elements(p_items)
    LOOP
        v_item_total := (v_item->>'total_amount')::NUMERIC;
        IF p_payment_mode = 'item_wise' THEN
            v_item_paid := COALESCE((v_item->>'amount_paid')::NUMERIC, 0);
            IF v_item_paid > v_item_total THEN
                RAISE EXCEPTION 'Item payment exceeds total amount.' USING ERRCODE = '23514';
            END IF;
            v_total_paid_accum := v_total_paid_accum + v_item_paid;
        ELSE
            v_alloc := LEAST(v_remaining_payment, v_item_total);
            v_item_paid := v_alloc;
            v_remaining_payment := v_remaining_payment - v_alloc;
        END IF;

        INSERT INTO public.supplier_items (
            supplier_id, owner_user_id, item_name, weight_grams, rate_per_gram,
            total_amount, amount_paid
        ) VALUES (
            v_supplier_id, p_owner_user_id, v_item->>'item_name', (v_item->>'weight_grams')::NUMERIC,
            (v_item->>'rate_per_gram')::NUMERIC, v_item_total, v_item_paid
        )
        RETURNING * INTO v_item_rec;

        v_inserted_items := v_inserted_items || to_jsonb(v_item_rec);
    END LOOP;

    IF v_total_paid_accum > 0 THEN
        INSERT INTO public.supplier_transactions (
            supplier_id, owner_user_id, amount, payment_method, notes
        ) VALUES (
            v_supplier_id, p_owner_user_id, v_total_paid_accum, p_payment_method, p_notes
        )
        RETURNING id INTO v_transaction_id;

        FOR v_item_rec IN SELECT * FROM jsonb_to_recordset(v_inserted_items) AS x(id UUID, amount_paid NUMERIC)
        LOOP
            IF v_item_rec.amount_paid > 0 THEN
                INSERT INTO public.supplier_payment_allocations (transaction_id, item_id, owner_user_id, allocated_amount)
                VALUES (v_transaction_id, v_item_rec.id, p_owner_user_id, v_item_rec.amount_paid);
            END IF;
        END LOOP;
    END IF;

    RETURN jsonb_build_object(
        'supplier', to_jsonb(v_supplier_rec),
        'items', v_inserted_items,
        'transaction_id', v_transaction_id,
        'payment_amount', v_total_paid_accum
    );
END;
$$;

-- ----------------------------------------------------------------------------
-- 8. add_items_for_existing_supplier
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.add_items_for_existing_supplier(
    p_owner_user_id UUID,
    p_supplier_id UUID,
    p_items JSONB,
    p_payment_mode TEXT,
    p_total_payment NUMERIC,
    p_payment_method TEXT,
    p_notes TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_supplier_rec RECORD;
    v_item JSONB;
    v_item_rec RECORD;
    v_inserted_items JSONB := '[]'::jsonb;
    v_transaction_id UUID := NULL;
    v_total_paid_accum NUMERIC := 0;
    v_purchase_total NUMERIC := 0;
    v_item_paid NUMERIC;
    v_item_total NUMERIC;
    v_remaining_payment NUMERIC;
    v_alloc NUMERIC;
BEGIN
    IF NOT public.is_authorized_for_shop(p_owner_user_id) THEN
        RAISE EXCEPTION 'Permission denied for this shop.' USING ERRCODE = '42501';
    END IF;

    IF p_payment_mode NOT IN ('item_wise', 'total') THEN
        RAISE EXCEPTION 'Invalid payment mode.' USING ERRCODE = '22000';
    END IF;

    IF p_items IS NULL OR jsonb_typeof(p_items) <> 'array' OR jsonb_array_length(p_items) = 0 THEN
        RAISE EXCEPTION 'Supplier items array cannot be empty or null.' USING ERRCODE = '22000';
    END IF;

    SELECT * INTO v_supplier_rec FROM public.suppliers WHERE id = p_supplier_id AND owner_user_id = p_owner_user_id;
    IF v_supplier_rec.id IS NULL THEN
        RAISE EXCEPTION 'Supplier not found.' USING ERRCODE = 'P0002';
    END IF;

    FOR v_item IN SELECT * FROM jsonb_array_elements(p_items)
    LOOP
        v_purchase_total := v_purchase_total + (v_item->>'total_amount')::NUMERIC;
    END LOOP;

    IF p_payment_mode = 'total' THEN
        v_total_paid_accum := COALESCE(p_total_payment, 0);
        IF v_total_paid_accum > v_purchase_total THEN
            RAISE EXCEPTION 'Payment cannot exceed total purchase amount.' USING ERRCODE = '23514';
        END IF;
        v_remaining_payment := v_total_paid_accum;
    END IF;

    FOR v_item IN SELECT * FROM jsonb_array_elements(p_items)
    LOOP
        v_item_total := (v_item->>'total_amount')::NUMERIC;
        IF p_payment_mode = 'item_wise' THEN
            v_item_paid := COALESCE((v_item->>'amount_paid')::NUMERIC, 0);
            IF v_item_paid > v_item_total THEN
                RAISE EXCEPTION 'Item payment exceeds total amount.' USING ERRCODE = '23514';
            END IF;
            v_total_paid_accum := v_total_paid_accum + v_item_paid;
        ELSE
            v_alloc := LEAST(v_remaining_payment, v_item_total);
            v_item_paid := v_alloc;
            v_remaining_payment := v_remaining_payment - v_alloc;
        END IF;

        INSERT INTO public.supplier_items (
            supplier_id, owner_user_id, item_name, weight_grams, rate_per_gram,
            total_amount, amount_paid
        ) VALUES (
            p_supplier_id, p_owner_user_id, v_item->>'item_name', (v_item->>'weight_grams')::NUMERIC,
            (v_item->>'rate_per_gram')::NUMERIC, v_item_total, v_item_paid
        )
        RETURNING * INTO v_item_rec;

        v_inserted_items := v_inserted_items || to_jsonb(v_item_rec);
    END LOOP;

    IF v_total_paid_accum > 0 THEN
        INSERT INTO public.supplier_transactions (
            supplier_id, owner_user_id, amount, payment_method, notes
        ) VALUES (
            p_supplier_id, p_owner_user_id, v_total_paid_accum, p_payment_method, p_notes
        )
        RETURNING id INTO v_transaction_id;

        FOR v_item_rec IN SELECT * FROM jsonb_to_recordset(v_inserted_items) AS x(id UUID, amount_paid NUMERIC)
        LOOP
            IF v_item_rec.amount_paid > 0 THEN
                INSERT INTO public.supplier_payment_allocations (transaction_id, item_id, owner_user_id, allocated_amount)
                VALUES (v_transaction_id, v_item_rec.id, p_owner_user_id, v_item_rec.amount_paid);
            END IF;
        END LOOP;
    END IF;

    RETURN jsonb_build_object(
        'supplier', to_jsonb(v_supplier_rec),
        'items', v_inserted_items,
        'transaction_id', v_transaction_id,
        'payment_amount', v_total_paid_accum
    );
END;
$$;

-- ----------------------------------------------------------------------------
-- 9. record_supplier_payment (CONCURRENCY SAFE IDEMPOTENCY)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.record_supplier_payment(
    p_owner_user_id UUID,
    p_supplier_id UUID,
    p_amount NUMERIC,
    p_payment_method TEXT,
    p_allocations JSONB,
    p_notes TEXT,
    p_idempotency_key TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_transaction_rec RECORD;
    v_alloc JSONB;
    v_item_id UUID;
    v_alloc_amount NUMERIC;
    v_sum_allocations NUMERIC := 0;
    v_item_due NUMERIC;
    v_updated_items JSONB := '[]'::jsonb;
    v_item_rec RECORD;
    v_clean_key TEXT;
    v_payload_hash TEXT;
    v_existing_status TEXT;
    v_existing_hash TEXT;
    v_existing_response JSONB;
    v_result JSONB;
BEGIN
    IF NOT public.is_authorized_for_shop(p_owner_user_id) THEN
        RAISE EXCEPTION 'Permission denied for this shop.' USING ERRCODE = '42501';
    END IF;

    IF p_amount IS NULL OR p_amount <= 0 THEN
        RAISE EXCEPTION 'Payment amount must be greater than zero.' USING ERRCODE = '23514';
    END IF;

    IF p_payment_method IS NULL OR p_payment_method NOT IN ('cash', 'upi', 'card', 'bank_transfer') THEN
        RAISE EXCEPTION 'Invalid or missing payment method.' USING ERRCODE = '22000';
    END IF;

    IF p_allocations IS NULL OR jsonb_typeof(p_allocations) <> 'array' OR jsonb_array_length(p_allocations) = 0 THEN
        RAISE EXCEPTION 'Payment allocations array cannot be empty or null.' USING ERRCODE = '22000';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM public.suppliers WHERE id = p_supplier_id AND owner_user_id = p_owner_user_id) THEN
        RAISE EXCEPTION 'Supplier not found.' USING ERRCODE = 'P0002';
    END IF;

    FOR v_alloc IN SELECT * FROM jsonb_array_elements(p_allocations)
    LOOP
        IF jsonb_typeof(v_alloc) <> 'object' THEN
            RAISE EXCEPTION 'Each allocation must be a valid JSON object.' USING ERRCODE = '22000';
        END IF;

        IF (v_alloc->>'item_id') IS NULL OR (v_alloc->>'allocated_amount') IS NULL THEN
            RAISE EXCEPTION 'Allocation must contain non-null item_id and allocated_amount.' USING ERRCODE = '22000';
        END IF;

        BEGIN
            v_item_id := (v_alloc->>'item_id')::UUID;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION 'Invalid UUID format for item_id.' USING ERRCODE = '22000';
        END;

        BEGIN
            v_alloc_amount := (v_alloc->>'allocated_amount')::NUMERIC;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION 'Invalid numeric format for allocation amount.' USING ERRCODE = '22000';
        END;

        IF v_alloc_amount <= 0 THEN
            RAISE EXCEPTION 'Allocation amount must be greater than zero.' USING ERRCODE = '23514';
        END IF;

        v_sum_allocations := v_sum_allocations + v_alloc_amount;
    END LOOP;

    IF (SELECT count(DISTINCT (v->>'item_id')::UUID) FROM jsonb_array_elements(p_allocations) v) <> jsonb_array_length(p_allocations) THEN
        RAISE EXCEPTION 'Duplicate item IDs in payment allocations are not allowed.' USING ERRCODE = '22000';
    END IF;

    IF v_sum_allocations <> p_amount THEN
        RAISE EXCEPTION 'Total allocated amount does not match payment amount.' USING ERRCODE = '23514';
    END IF;

    -- ATOMIC CONCURRENCY-SAFE IDEMPOTENCY RESERVATION
    IF p_idempotency_key IS NOT NULL AND trim(p_idempotency_key) <> '' THEN
        v_clean_key := trim(p_idempotency_key);
        v_payload_hash := encode(digest(p_supplier_id::text || ':' || p_payment_method || ':' || p_amount::text || ':' || p_allocations::text || ':' || COALESCE(p_notes, ''), 'sha256'), 'hex');

        INSERT INTO public.idempotency_logs (
            owner_user_id, operation, idempotency_key, payload_hash, status
        ) VALUES (
            p_owner_user_id, 'record_supplier_payment', v_clean_key, v_payload_hash, 'pending'
        )
        ON CONFLICT (owner_user_id, operation, idempotency_key) DO UPDATE
        SET updated_at = now();

        SELECT status, payload_hash, response_payload
        INTO v_existing_status, v_existing_hash, v_existing_response
        FROM public.idempotency_logs
        WHERE owner_user_id = p_owner_user_id
          AND operation = 'record_supplier_payment'
          AND idempotency_key = v_clean_key
        FOR UPDATE;

        IF v_existing_status = 'completed' THEN
            IF v_existing_hash = v_payload_hash THEN
                RETURN v_existing_response;
            ELSE
                RAISE EXCEPTION 'Idempotency key reuse with conflicting payload.' USING ERRCODE = '22000';
            END IF;
        END IF;
    END IF;

    INSERT INTO public.supplier_transactions (
        supplier_id, owner_user_id, amount, payment_method, notes
    ) VALUES (
        p_supplier_id, p_owner_user_id, p_amount, p_payment_method, p_notes
    )
    RETURNING * INTO v_transaction_rec;

    FOR v_alloc IN SELECT value FROM jsonb_array_elements(p_allocations) ORDER BY (value->>'item_id')::UUID ASC
    LOOP
        v_item_id := (v_alloc->>'item_id')::UUID;
        v_alloc_amount := (v_alloc->>'allocated_amount')::NUMERIC;

        SELECT due_amount INTO v_item_due FROM public.supplier_items
        WHERE id = v_item_id AND owner_user_id = p_owner_user_id AND supplier_id = p_supplier_id
        FOR UPDATE;

        IF v_item_due IS NULL THEN
            RAISE EXCEPTION 'Supplier purchase item not found.' USING ERRCODE = 'P0002';
        END IF;

        IF v_alloc_amount > v_item_due THEN
            RAISE EXCEPTION 'Payment exceeds outstanding balance.' USING ERRCODE = '23514';
        END IF;

        INSERT INTO public.supplier_payment_allocations (transaction_id, item_id, owner_user_id, allocated_amount)
        VALUES (v_transaction_rec.id, v_item_id, p_owner_user_id, v_alloc_amount);

        UPDATE public.supplier_items
        SET amount_paid = amount_paid + v_alloc_amount
        WHERE id = v_item_id
        RETURNING * INTO v_item_rec;

        v_updated_items := v_updated_items || to_jsonb(v_item_rec);
    END LOOP;

    v_result := jsonb_build_object(
        'transaction', to_jsonb(v_transaction_rec),
        'payment_amount', p_amount,
        'items', v_updated_items
    );

    IF p_idempotency_key IS NOT NULL AND trim(p_idempotency_key) <> '' THEN
        UPDATE public.idempotency_logs
        SET status = 'completed',
            response_payload = v_result
        WHERE owner_user_id = p_owner_user_id
          AND operation = 'record_supplier_payment'
          AND idempotency_key = v_clean_key;
    END IF;

    RETURN v_result;
END;
$$;

-- ----------------------------------------------------------------------------
-- 10. get_owner_suppliers_summary
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_owner_suppliers_summary(
    p_owner_user_id UUID,
    p_filter TEXT DEFAULT 'all',
    p_search TEXT DEFAULT NULL,
    p_limit INT DEFAULT 20,
    p_offset INT DEFAULT 0
)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_suppliers JSONB;
    v_total BIGINT;
BEGIN
    IF NOT public.is_authorized_for_shop(p_owner_user_id) THEN
        RAISE EXCEPTION 'Permission denied for this shop.' USING ERRCODE = '42501';
    END IF;

    WITH summary AS (
        SELECT
            s.id AS supplier_id,
            s.supplier_name,
            s.phone_number,
            COALESCE(SUM(si.amount_paid), 0) AS total_paid_amount,
            COALESCE(SUM(si.due_amount), 0) AS total_due_amount
        FROM public.suppliers s
        LEFT JOIN public.supplier_items si ON si.supplier_id = s.id
        WHERE s.owner_user_id = p_owner_user_id
          AND (p_search IS NULL OR s.supplier_name ILIKE '%' || p_search || '%' OR s.phone_number ILIKE '%' || p_search || '%')
        GROUP BY s.id, s.supplier_name, s.phone_number
        HAVING
            (p_filter = 'all') OR
            (p_filter = 'baki' AND COALESCE(SUM(si.due_amount), 0) > 0) OR
            (p_filter = 'paid' AND COALESCE(SUM(si.due_amount), 0) = 0)
    )
    SELECT
        COALESCE(jsonb_agg(to_jsonb(sub)), '[]'::jsonb),
        (SELECT COUNT(*) FROM summary)
    INTO v_suppliers, v_total
    FROM (
        SELECT * FROM summary ORDER BY supplier_name ASC LIMIT p_limit OFFSET p_offset
    ) sub;

    RETURN jsonb_build_object(
        'suppliers', v_suppliers,
        'total', COALESCE(v_total, 0),
        'limit', p_limit,
        'offset', p_offset
    );
END;
$$;

-- ----------------------------------------------------------------------------
-- 11. get_owner_supplier_details
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_owner_supplier_details(
    p_owner_user_id UUID,
    p_supplier_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_supplier_rec RECORD;
    v_items JSONB;
    v_summary JSONB;
BEGIN
    IF NOT public.is_authorized_for_shop(p_owner_user_id) THEN
        RAISE EXCEPTION 'Permission denied for this shop.' USING ERRCODE = '42501';
    END IF;

    SELECT * INTO v_supplier_rec FROM public.suppliers
    WHERE id = p_supplier_id AND owner_user_id = p_owner_user_id;

    IF v_supplier_rec.id IS NULL THEN
        RAISE EXCEPTION 'Supplier not found.' USING ERRCODE = 'P0002';
    END IF;

    SELECT COALESCE(jsonb_agg(to_jsonb(si)), '[]'::jsonb) INTO v_items
    FROM public.supplier_items si
    WHERE si.supplier_id = p_supplier_id AND si.owner_user_id = p_owner_user_id;

    SELECT jsonb_build_object(
        'total_purchase_amount', COALESCE(SUM(total_amount), 0),
        'total_paid_amount', COALESCE(SUM(amount_paid), 0),
        'total_due_amount', COALESCE(SUM(due_amount), 0),
        'items_count', COUNT(*)
    ) INTO v_summary
    FROM public.supplier_items
    WHERE supplier_id = p_supplier_id AND owner_user_id = p_owner_user_id;

    RETURN jsonb_build_object(
        'supplier', to_jsonb(v_supplier_rec),
        'items', v_items,
        'summary', v_summary
    );
END;
$$;

-- ----------------------------------------------------------------------------
-- 12. get_owner_supplier_transactions
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_owner_supplier_transactions(
    p_owner_user_id UUID,
    p_supplier_id UUID DEFAULT NULL,
    p_search TEXT DEFAULT NULL,
    p_start_date TEXT DEFAULT NULL,
    p_end_date TEXT DEFAULT NULL,
    p_limit INT DEFAULT 20,
    p_offset INT DEFAULT 0
)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_transactions JSONB;
    v_total BIGINT;
    v_total_collected NUMERIC;
BEGIN
    IF NOT public.is_authorized_for_shop(p_owner_user_id) THEN
        RAISE EXCEPTION 'Permission denied for this shop.' USING ERRCODE = '42501';
    END IF;

    WITH filtered AS (
        SELECT
            st.id AS transaction_id,
            st.supplier_id,
            s.supplier_name,
            s.phone_number AS supplier_phone_number,
            st.amount,
            st.payment_method,
            st.transaction_date,
            st.notes
        FROM public.supplier_transactions st
        JOIN public.suppliers s ON s.id = st.supplier_id
        WHERE st.owner_user_id = p_owner_user_id
          AND (p_supplier_id IS NULL OR st.supplier_id = p_supplier_id)
          AND (p_search IS NULL OR s.supplier_name ILIKE '%' || p_search || '%' OR s.phone_number ILIKE '%' || p_search || '%')
          AND (p_start_date IS NULL OR st.transaction_date >= p_start_date::TIMESTAMPTZ)
          AND (p_end_date IS NULL OR st.transaction_date <= p_end_date::TIMESTAMPTZ)
    )
    SELECT
        COALESCE(jsonb_agg(to_jsonb(sub)), '[]'::jsonb),
        (SELECT COUNT(*) FROM filtered),
        (SELECT COALESCE(SUM(amount), 0) FROM filtered)
    INTO v_transactions, v_total, v_total_collected
    FROM (
        SELECT * FROM filtered ORDER BY transaction_date DESC LIMIT p_limit OFFSET p_offset
    ) sub;

    RETURN jsonb_build_object(
        'transactions', v_transactions,
        'total', COALESCE(v_total, 0),
        'total_collected_amount', v_total_collected,
        'limit', p_limit,
        'offset', p_offset
    );
END;
$$;

-- ----------------------------------------------------------------------------
-- 13. get_owner_supplier_transaction_details
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_owner_supplier_transaction_details(
    p_owner_user_id UUID,
    p_transaction_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_tx_rec RECORD;
    v_allocations JSONB;
BEGIN
    IF NOT public.is_authorized_for_shop(p_owner_user_id) THEN
        RAISE EXCEPTION 'Permission denied for this shop.' USING ERRCODE = '42501';
    END IF;

    SELECT
        st.id, st.supplier_id, s.supplier_name, s.phone_number AS supplier_phone_number,
        st.amount, st.payment_method, st.transaction_date, st.notes
    INTO v_tx_rec
    FROM public.supplier_transactions st
    JOIN public.suppliers s ON s.id = st.supplier_id
    WHERE st.id = p_transaction_id AND st.owner_user_id = p_owner_user_id;

    IF v_tx_rec.id IS NULL THEN
        RAISE EXCEPTION 'Supplier transaction not found.' USING ERRCODE = 'P0002';
    END IF;

    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'item_id', si.id,
        'item_name', si.item_name,
        'weight_grams', si.weight_grams,
        'total_amount', si.total_amount,
        'allocated_amount', spa.allocated_amount
    )), '[]'::jsonb)
    INTO v_allocations
    FROM public.supplier_payment_allocations spa
    JOIN public.supplier_items si ON si.id = spa.item_id
    WHERE spa.transaction_id = p_transaction_id AND spa.owner_user_id = p_owner_user_id;

    RETURN jsonb_build_object(
        'transaction', to_jsonb(v_tx_rec),
        'allocations', v_allocations
    );
END;
$$;

-- ----------------------------------------------------------------------------
-- 14. upsert_market_rate_snapshots
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.upsert_market_rate_snapshots(
    p_snapshots JSONB
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_snapshot JSONB;
BEGIN
    FOR v_snapshot IN SELECT * FROM jsonb_array_elements(p_snapshots)
    LOOP
        INSERT INTO public.market_rate_snapshots (
            metal, market_date, source_generated_at, fetched_at, rates
        ) VALUES (
            v_snapshot->>'metal',
            (v_snapshot->>'market_date')::DATE,
            (v_snapshot->>'source_generated_at')::TIMESTAMPTZ,
            now(),
            v_snapshot->'rates'
        )
        ON CONFLICT (metal) DO UPDATE SET
            market_date = EXCLUDED.market_date,
            source_generated_at = EXCLUDED.source_generated_at,
            fetched_at = EXCLUDED.fetched_at,
            rates = EXCLUDED.rates;
    END LOOP;
END;
$$;

-- ============================================================================
-- 6. PERMISSIONS & PRIVILEGE HARDENING (EXPLICIT GRANULAR GRANTS)
-- ============================================================================

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, anon, authenticated;

-- Set default privilege revokes for future objects created by postgres role
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC, anon, authenticated;

GRANT USAGE ON SCHEMA public TO authenticated, service_role;

-- Direct table access for authenticated users
GRANT SELECT ON public.user_profiles TO authenticated, service_role;
GRANT SELECT ON public.shop_memberships TO authenticated, service_role;
GRANT SELECT ON public.customers TO authenticated, service_role;
GRANT SELECT ON public.customer_items TO authenticated, service_role;
GRANT SELECT ON public.customer_transactions TO authenticated, service_role;
GRANT SELECT ON public.customer_payment_allocations TO authenticated, service_role;
GRANT SELECT ON public.suppliers TO authenticated, service_role;
GRANT SELECT ON public.supplier_items TO authenticated, service_role;
GRANT SELECT ON public.supplier_transactions TO authenticated, service_role;
GRANT SELECT ON public.supplier_payment_allocations TO authenticated, service_role;
GRANT SELECT ON public.market_rate_snapshots TO authenticated, service_role;
GRANT SELECT ON public.idempotency_logs TO authenticated, service_role;

-- Full administrative permissions for service_role
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO service_role;

-- Explicitly grant RPC execution privileges to authenticated and service_role
GRANT EXECUTE ON FUNCTION public.is_owner(UUID) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.is_authorized_for_shop(UUID) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.create_customer_with_items_and_payment(TEXT, TEXT, TEXT, UUID, TEXT, TEXT, NUMERIC, JSONB) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.add_items_for_existing_customer(TEXT, TEXT, UUID, TEXT, TEXT, NUMERIC, JSONB) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.record_customer_payment(UUID, UUID, TEXT, JSONB, TEXT) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.get_owner_customers_summary(UUID, TEXT, TEXT, INT, INT) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.get_owner_transactions(UUID, TEXT, TEXT, TEXT, TEXT, INT, INT) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.get_owner_transaction_details(UUID, UUID) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.create_supplier_with_items_and_payment(UUID, TEXT, TEXT, TEXT, JSONB, TEXT, NUMERIC, TEXT, TEXT) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.add_items_for_existing_supplier(UUID, UUID, JSONB, TEXT, NUMERIC, TEXT, TEXT) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.record_supplier_payment(UUID, UUID, NUMERIC, TEXT, JSONB, TEXT, TEXT) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.get_owner_suppliers_summary(UUID, TEXT, TEXT, INT, INT) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.get_owner_supplier_details(UUID, UUID) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.get_owner_supplier_transactions(UUID, UUID, TEXT, TEXT, TEXT, INT, INT) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.get_owner_supplier_transaction_details(UUID, UUID) TO authenticated, service_role;

-- Exclusively grant rate refresh RPC to service_role ONLY
GRANT EXECUTE ON FUNCTION public.upsert_market_rate_snapshots(JSONB) TO service_role;

-- ============================================================================
-- END OF MIGRATION SCRIPT
-- ============================================================================
