import os
import json
import uuid
import concurrent.futures
import psycopg2
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_LIVE_POSTGRESQL_TESTS"),
    reason="Live local PostgreSQL database instance not running in environment"
)

PG_CONN_STR = "postgresql://postgres@127.0.0.1:5439/jaiwer_v464_fresh_test"

def get_db_conn():
    conn = psycopg2.connect(PG_CONN_STR)
    conn.autocommit = True
    return conn

def set_auth_context(cur, user_id: str, role: str = "authenticated"):
    if role == "authenticated":
        cur.execute("SET ROLE authenticated;")
    else:
        cur.execute("RESET ROLE;")
    cur.execute("SELECT set_config('request.jwt.claim.sub', %s, false);", (user_id,))
    cur.execute("SELECT set_config('request.jwt.claim.role', %s, false);", (role,))

def rand_phone() -> str:
    return f"+91{uuid.uuid4().int % 9000000000 + 1000000000}"

def test_real_pg_1_fresh_migration_and_setup():
    """Verify that migration applied cleanly and all 13 tables exist."""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';")
    tables = [row[0] for row in cur.fetchall()]
    conn.close()

    expected_tables = [
        "user_profiles", "shop_memberships", "customers", "customer_items",
        "customer_transactions", "customer_payment_allocations", "suppliers",
        "supplier_items", "supplier_transactions", "supplier_payment_allocations",
        "market_rate_snapshots", "market_rate_refresh_runs", "idempotency_logs"
    ]
    for table in expected_tables:
        assert table in tables, f"Table {table} missing in local PostgreSQL"

def test_real_pg_2_customer_idempotency_sequential():
    """Test sequential customer payments with same idempotency key."""
    conn = get_db_conn()
    cur = conn.cursor()

    owner_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    email = f"owner1-{uuid.uuid4()}@example.com"
    phone = f"+91{uuid.uuid4().int % 10000000000:010d}"
    cust_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s);", (owner_id, email))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Owner One', %s, 'owner');", (owner_id, email, phone))
    cur.execute("INSERT INTO public.customers (id, name, phone_number) VALUES (%s, 'Customer One', %s);", (customer_id, cust_phone))
    cur.execute("INSERT INTO public.customer_items (id, customer_id, owner_user_id, item_name, weight_grams, total_amount, amount_paid) VALUES (%s, %s, %s, 'Gold Ring', 10, 50000, 0);", (item_id, customer_id, owner_id))

    set_auth_context(cur, owner_id, "authenticated")

    allocations = json.dumps([{"item_id": item_id, "amount": 10000.0}])
    idempotency_key = f"key-{uuid.uuid4()}"

    # Call 1
    cur.execute("SELECT public.record_customer_payment(%s, %s, 'cash', %s::jsonb, %s);", (customer_id, owner_id, allocations, idempotency_key))
    res1 = cur.fetchone()[0]

    # Call 2 with identical key and payload
    cur.execute("SELECT public.record_customer_payment(%s, %s, 'cash', %s::jsonb, %s);", (customer_id, owner_id, allocations, idempotency_key))
    res2 = cur.fetchone()[0]

    conn.close()

    assert res1["transaction"]["id"] == res2["transaction"]["id"]
    assert res1["payment_amount"] == res2["payment_amount"]

def test_real_pg_3_customer_idempotency_simultaneous_concurrent():
    """Test simultaneous concurrent payment requests with same idempotency key using 2 separate connections."""
    owner_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    idempotency_key = f"key-concurrent-{uuid.uuid4()}"
    email = f"owner2-{uuid.uuid4()}@example.com"
    phone = f"+91{uuid.uuid4().int % 10000000000:010d}"
    cust_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s);", (owner_id, email))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Owner Two', %s, 'owner');", (owner_id, email, phone))
    cur.execute("INSERT INTO public.customers (id, name, phone_number) VALUES (%s, 'Customer Two', %s);", (customer_id, cust_phone))
    cur.execute("INSERT INTO public.customer_items (id, customer_id, owner_user_id, item_name, weight_grams, total_amount, amount_paid) VALUES (%s, %s, %s, 'Necklace', 20, 100000, 0);", (item_id, customer_id, owner_id))
    conn.close()

    allocations = json.dumps([{"item_id": item_id, "amount": 20000.0}])

    def worker_request():
        c = get_db_conn()
        cur_w = c.cursor()
        set_auth_context(cur_w, owner_id, "authenticated")
        cur_w.execute("SELECT public.record_customer_payment(%s, %s, 'cash', %s::jsonb, %s);", (customer_id, owner_id, allocations, idempotency_key))
        res = cur_w.fetchone()[0]
        c.close()
        return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker_request)
        f2 = executor.submit(worker_request)
        res1 = f1.result()
        res2 = f2.result()

    assert res1["transaction"]["id"] == res2["transaction"]["id"]

def test_real_pg_4_idempotency_conflicting_payload_rejected():
    """Test same idempotency key with conflicting payload raises exception 22000."""
    conn = get_db_conn()
    cur = conn.cursor()

    owner_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    idempotency_key = f"key-conflict-{uuid.uuid4()}"
    email = f"owner3-{uuid.uuid4()}@example.com"
    phone = f"+91{uuid.uuid4().int % 10000000000:010d}"
    cust_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s);", (owner_id, email))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Owner Three', %s, 'owner');", (owner_id, email, phone))
    cur.execute("INSERT INTO public.customers (id, name, phone_number) VALUES (%s, 'Customer Three', %s);", (customer_id, cust_phone))
    cur.execute("INSERT INTO public.customer_items (id, customer_id, owner_user_id, item_name, weight_grams, total_amount, amount_paid) VALUES (%s, %s, %s, 'Bangle', 15, 60000, 0);", (item_id, customer_id, owner_id))

    set_auth_context(cur, owner_id, "authenticated")

    alloc1 = json.dumps([{"item_id": item_id, "amount": 10000.0}])
    alloc2 = json.dumps([{"item_id": item_id, "amount": 20000.0}])

    cur.execute("SELECT public.record_customer_payment(%s, %s, 'cash', %s::jsonb, %s);", (customer_id, owner_id, alloc1, idempotency_key))

    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("SELECT public.record_customer_payment(%s, %s, 'cash', %s::jsonb, %s);", (customer_id, owner_id, alloc2, idempotency_key))

    assert "Idempotency key reuse with conflicting payload" in str(exc_info.value)
    conn.close()

def test_real_pg_5_supplier_idempotency_sequential_and_concurrent():
    """Test supplier payment idempotency."""
    conn = get_db_conn()
    cur = conn.cursor()

    owner_id = str(uuid.uuid4())
    supplier_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    idempotency_key = f"supp-key-{uuid.uuid4()}"
    email = f"owner4-{uuid.uuid4()}@example.com"
    phone = f"+91{uuid.uuid4().int % 10000000000:010d}"
    supp_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s);", (owner_id, email))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Owner Four', %s, 'owner');", (owner_id, email, phone))
    cur.execute("INSERT INTO public.suppliers (id, owner_user_id, supplier_name, phone_number) VALUES (%s, %s, 'Supplier One', %s);", (supplier_id, owner_id, supp_phone))
    cur.execute("INSERT INTO public.supplier_items (id, supplier_id, owner_user_id, item_name, total_amount, amount_paid) VALUES (%s, %s, %s, 'Gold Alloy', 80000, 0);", (item_id, supplier_id, owner_id))

    set_auth_context(cur, owner_id, "authenticated")

    allocations = json.dumps([{"item_id": item_id, "allocated_amount": 25000.0}])

    cur.execute("SELECT public.record_supplier_payment(%s, %s, 25000.0, 'bank_transfer', %s::jsonb, 'Notes', %s);", (owner_id, supplier_id, allocations, idempotency_key))
    res1 = cur.fetchone()[0]

    cur.execute("SELECT public.record_supplier_payment(%s, %s, 25000.0, 'bank_transfer', %s::jsonb, 'Notes', %s);", (owner_id, supplier_id, allocations, idempotency_key))
    res2 = cur.fetchone()[0]

    assert res1["transaction"]["id"] == res2["transaction"]["id"]
    conn.close()

def test_real_pg_6_overpayment_protection():
    """Test that payments exceeding item due_amount are rejected by check constraint 23514."""
    conn = get_db_conn()
    cur = conn.cursor()

    owner_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    email = f"owner5-{uuid.uuid4()}@example.com"
    phone = f"+91{uuid.uuid4().int % 10000000000:010d}"
    cust_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s);", (owner_id, email))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Owner Five', %s, 'owner');", (owner_id, email, phone))
    cur.execute("INSERT INTO public.customers (id, name, phone_number) VALUES (%s, 'Customer Five', %s);", (customer_id, cust_phone))
    cur.execute("INSERT INTO public.customer_items (id, customer_id, owner_user_id, item_name, weight_grams, total_amount, amount_paid) VALUES (%s, %s, %s, 'Earrings', 5, 10000, 0);", (item_id, customer_id, owner_id))

    set_auth_context(cur, owner_id, "authenticated")

    allocations = json.dumps([{"item_id": item_id, "amount": 15000.0}])

    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("SELECT public.record_customer_payment(%s, %s, 'cash', %s::jsonb, NULL);", (customer_id, owner_id, allocations))

    assert "Payment exceeds item outstanding balance" in str(exc_info.value)
    conn.close()

def test_real_pg_7_concurrent_overpayment_race():
    """
    Test concurrent overpayment race condition:
    Item has total_amount = 50000, amount_paid = 0, due_amount = 50000.
    Two genuinely separate DB connections execute record_customer_payment for 30000 each (sum = 60000 > 50000).
    Exactly ONE transaction must succeed, and the other MUST fail with 'Payment exceeds item outstanding balance'.
    The item must NEVER become overpaid (final amount_paid MUST be 30000).
    """
    owner_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    email = f"owner-race-{uuid.uuid4()}@example.com"
    phone = f"+91{uuid.uuid4().int % 10000000000:010d}"
    cust_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s);", (owner_id, email))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Race Owner', %s, 'owner');", (owner_id, email, phone))
    cur.execute("INSERT INTO public.customers (id, name, phone_number) VALUES (%s, 'Race Customer', %s);", (customer_id, cust_phone))
    cur.execute("INSERT INTO public.customer_items (id, customer_id, owner_user_id, item_name, weight_grams, total_amount, amount_paid) VALUES (%s, %s, %s, 'Race Chain', 10, 50000, 0);", (item_id, customer_id, owner_id))
    conn.close()

    allocations = json.dumps([{"item_id": item_id, "amount": 30000.0}])

    def execute_payment(req_num):
        c = get_db_conn()
        cur_w = c.cursor()
        set_auth_context(cur_w, owner_id, "authenticated")
        try:
            cur_w.execute("SELECT public.record_customer_payment(%s, %s, 'cash', %s::jsonb, NULL);", (customer_id, owner_id, allocations))
            res = cur_w.fetchone()[0]
            c.close()
            return ("SUCCESS", res)
        except Exception as exc:
            c.close()
            return ("FAILED", str(exc))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(execute_payment, 1)
        f2 = executor.submit(execute_payment, 2)
        r1 = f1.result()
        r2 = f2.result()

    results = [r1, r2]
    successes = [r for r in results if r[0] == "SUCCESS"]
    failures = [r for r in results if r[0] == "FAILED"]

    assert len(successes) == 1, f"Expected 1 success, got {len(successes)}"
    assert len(failures) == 1, f"Expected 1 failure, got {len(failures)}"
    assert "Payment exceeds item outstanding balance" in failures[0][1]

    conn_chk = get_db_conn()
    cur_chk = conn_chk.cursor()
    cur_chk.execute("SELECT amount_paid, due_amount FROM public.customer_items WHERE id = %s;", (item_id,))
    item_row = cur_chk.fetchone()
    conn_chk.close()

    assert float(item_row[0]) == 30000.0, f"Expected amount_paid=30000, got {item_row[0]}"
    assert float(item_row[1]) == 20000.0, f"Expected due_amount=20000, got {item_row[1]}"

def test_real_pg_8_supplier_cross_shop_payment():
    """
    Test supplier cross-shop payment rejection:
    Owner A attempts to record a supplier payment for Owner B's supplier and item.
    Rejected by PostgreSQL RPC authorization checks.
    Verify 0 transactions or allocations were persisted.
    """
    conn = get_db_conn()
    cur = conn.cursor()

    ownerA_id = str(uuid.uuid4())
    ownerB_id = str(uuid.uuid4())
    supplierB_id = str(uuid.uuid4())
    itemB_id = str(uuid.uuid4())

    emailA = f"ownerA-supp-{uuid.uuid4()}@example.com"
    emailB = f"ownerB-supp-{uuid.uuid4()}@example.com"
    phoneA = f"+91{uuid.uuid4().int % 10000000000:010d}"
    phoneB = f"+91{uuid.uuid4().int % 10000000000:010d}"
    supp_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s), (%s, %s);", (ownerA_id, emailA, ownerB_id, emailB))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Owner A', %s, 'owner'), (%s, %s, 'Owner B', %s, 'owner');", (ownerA_id, emailA, phoneA, ownerB_id, emailB, phoneB))
    cur.execute("INSERT INTO public.suppliers (id, owner_user_id, supplier_name, phone_number) VALUES (%s, %s, 'Supplier B', %s);", (supplierB_id, ownerB_id, supp_phone))
    cur.execute("INSERT INTO public.supplier_items (id, supplier_id, owner_user_id, item_name, total_amount, amount_paid) VALUES (%s, %s, %s, 'Raw Gold B', 100000, 0);", (itemB_id, supplierB_id, ownerB_id))

    set_auth_context(cur, ownerA_id, "authenticated")

    allocations = json.dumps([{"item_id": itemB_id, "allocated_amount": 10000.0}])

    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("SELECT public.record_supplier_payment(%s, %s, 10000.0, 'cash', %s::jsonb, 'Cross-shop', NULL);", (ownerB_id, supplierB_id, allocations))

    assert ("Permission denied for this shop" in str(exc_info.value) or "Supplier not found" in str(exc_info.value))

    set_auth_context(cur, ownerA_id, "service_role")
    cur.execute("SELECT COUNT(*) FROM public.supplier_transactions WHERE supplier_id = %s;", (supplierB_id,))
    assert cur.fetchone()[0] == 0

    conn.close()

def test_real_pg_9_allocation_ownership_integrity():
    """
    Test composite foreign key constraints:
    Attempt to link a customer payment transaction from Shop A to an item from Shop B.
    Verify database-level rejection via composite foreign key constraint.
    """
    conn = get_db_conn()
    cur = conn.cursor()

    ownerA_id = str(uuid.uuid4())
    ownerB_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    itemB_id = str(uuid.uuid4())
    txA_id = str(uuid.uuid4())

    emailA = f"ownerA-fk-{uuid.uuid4()}@example.com"
    emailB = f"ownerB-fk-{uuid.uuid4()}@example.com"
    phoneA = f"+91{uuid.uuid4().int % 10000000000:010d}"
    phoneB = f"+91{uuid.uuid4().int % 10000000000:010d}"
    cust_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s), (%s, %s);", (ownerA_id, emailA, ownerB_id, emailB))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Owner A', %s, 'owner'), (%s, %s, 'Owner B', %s, 'owner');", (ownerA_id, emailA, phoneA, ownerB_id, emailB, phoneB))
    cur.execute("INSERT INTO public.customers (id, name, phone_number) VALUES (%s, 'Customer FK', %s);", (customer_id, cust_phone))

    cur.execute("INSERT INTO public.customer_items (id, customer_id, owner_user_id, item_name, weight_grams, total_amount, amount_paid) VALUES (%s, %s, %s, 'Item B', 10, 50000, 0);", (itemB_id, customer_id, ownerB_id))
    cur.execute("INSERT INTO public.customer_transactions (id, receipt_number, customer_id, owner_user_id, amount, payment_method) VALUES (%s, %s, %s, %s, 10000, 'cash');", (txA_id, f"REC-FK-{uuid.uuid4()}", customer_id, ownerA_id))

    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("INSERT INTO public.customer_payment_allocations (transaction_id, item_id, owner_user_id, allocated_amount) VALUES (%s, %s, %s, 10000);", (txA_id, itemB_id, ownerA_id))

    assert "fk_customer_alloc_item" in str(exc_info.value) or "violates foreign key constraint" in str(exc_info.value)
    conn.close()

def test_real_pg_10_invalid_payment_atomic_rollback():
    """Test that a failed payment rolls back both transactions and idempotency reservation."""
    conn = get_db_conn()
    cur = conn.cursor()

    owner_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    idempotency_key = f"rollback-key-{uuid.uuid4()}"
    email = f"owner6-{uuid.uuid4()}@example.com"
    phone = f"+91{uuid.uuid4().int % 10000000000:010d}"
    cust_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s);", (owner_id, email))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Owner Six', %s, 'owner');", (owner_id, email, phone))
    cur.execute("INSERT INTO public.customers (id, name, phone_number) VALUES (%s, 'Customer Six', %s);", (customer_id, cust_phone))
    cur.execute("INSERT INTO public.customer_items (id, customer_id, owner_user_id, item_name, weight_grams, total_amount, amount_paid) VALUES (%s, %s, %s, 'Pendant', 5, 10000, 0);", (item_id, customer_id, owner_id))

    set_auth_context(cur, owner_id, "authenticated")

    allocations = json.dumps([{"item_id": item_id, "amount": 99999.0}])

    with pytest.raises(psycopg2.Error):
        cur.execute("SELECT public.record_customer_payment(%s, %s, 'cash', %s::jsonb, %s);", (customer_id, owner_id, allocations, idempotency_key))

    set_auth_context(cur, owner_id, "service_role")

    cur.execute("SELECT COUNT(*) FROM public.customer_transactions WHERE customer_id = %s;", (customer_id,))
    assert cur.fetchone()[0] == 0

    cur.execute("SELECT COUNT(*) FROM public.idempotency_logs WHERE idempotency_key = %s;", (idempotency_key,))
    assert cur.fetchone()[0] == 0

    conn.close()

def test_real_pg_11_cross_shop_customer_payment_rejected():
    """Test cross-shop payment attempt is rejected by permission check."""
    conn = get_db_conn()
    cur = conn.cursor()

    ownerA_id = str(uuid.uuid4())
    ownerB_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())

    emailA = f"ownerA-{uuid.uuid4()}@example.com"
    emailB = f"ownerB-{uuid.uuid4()}@example.com"
    phoneA = f"+91{uuid.uuid4().int % 10000000000:010d}"
    phoneB = f"+91{uuid.uuid4().int % 10000000000:010d}"
    cust_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s), (%s, %s);", (ownerA_id, emailA, ownerB_id, emailB))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Owner A', %s, 'owner'), (%s, %s, 'Owner B', %s, 'owner');", (ownerA_id, emailA, phoneA, ownerB_id, emailB, phoneB))
    cur.execute("INSERT INTO public.customers (id, name, phone_number) VALUES (%s, 'Customer Seven', %s);", (customer_id, cust_phone))
    cur.execute("INSERT INTO public.customer_items (id, customer_id, owner_user_id, item_name, weight_grams, total_amount, amount_paid) VALUES (%s, %s, %s, 'Ring B', 5, 20000, 0);", (item_id, customer_id, ownerB_id))

    set_auth_context(cur, ownerA_id, "authenticated")

    allocations = json.dumps([{"item_id": item_id, "amount": 5000.0}])

    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("SELECT public.record_customer_payment(%s, %s, 'cash', %s::jsonb, NULL);", (customer_id, ownerB_id, allocations))

    assert "Permission denied for this shop" in str(exc_info.value)
    conn.close()

def test_real_pg_12_unauthorized_worker_access_denied():
    """Test unauthorized worker without approved membership is denied shop access."""
    conn = get_db_conn()
    cur = conn.cursor()

    owner_id = str(uuid.uuid4())
    worker_id = str(uuid.uuid4())
    emailO = f"owner-{uuid.uuid4()}@example.com"
    emailW = f"worker-{uuid.uuid4()}@example.com"
    phoneO = f"+91{uuid.uuid4().int % 10000000000:010d}"
    phoneW = f"+91{uuid.uuid4().int % 10000000000:010d}"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s), (%s, %s);", (owner_id, emailO, worker_id, emailW))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Owner 9', %s, 'owner'), (%s, %s, 'Worker 9', %s, 'worker');", (owner_id, emailO, phoneO, worker_id, emailW, phoneW))

    set_auth_context(cur, worker_id, "authenticated")

    cur.execute("SELECT public.is_authorized_for_shop(%s);", (owner_id,))
    is_auth = cur.fetchone()[0]

    assert is_auth is False
    conn.close()

def test_real_pg_13_approved_worker_access_granted():
    """Test approved worker with shop membership is granted shop access."""
    conn = get_db_conn()
    cur = conn.cursor()

    owner_id = str(uuid.uuid4())
    worker_id = str(uuid.uuid4())
    emailO = f"owner-{uuid.uuid4()}@example.com"
    emailW = f"worker-{uuid.uuid4()}@example.com"
    phoneO = f"+91{uuid.uuid4().int % 10000000000:010d}"
    phoneW = f"+91{uuid.uuid4().int % 10000000000:010d}"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s), (%s, %s);", (owner_id, emailO, worker_id, emailW))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Owner 10', %s, 'owner'), (%s, %s, 'Worker 10', %s, 'worker');", (owner_id, emailO, phoneO, worker_id, emailW, phoneW))
    cur.execute("INSERT INTO public.shop_memberships (owner_user_id, worker_user_id, status) VALUES (%s, %s, 'approved');", (owner_id, worker_id))

    set_auth_context(cur, worker_id, "authenticated")

    cur.execute("SELECT public.is_authorized_for_shop(%s);", (owner_id,))
    is_auth = cur.fetchone()[0]

    assert is_auth is True
    conn.close()

def test_real_pg_14_direct_table_write_revoked_for_authenticated():
    """Test direct INSERT on protected customer_items table by authenticated user is revoked."""
    conn = get_db_conn()
    cur = conn.cursor()

    owner_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    email = f"owner11-{uuid.uuid4()}@example.com"
    phone = f"+91{uuid.uuid4().int % 10000000000:010d}"
    cust_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s);", (owner_id, email))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Owner 11', %s, 'owner');", (owner_id, email, phone))
    cur.execute("INSERT INTO public.customers (id, name, phone_number) VALUES (%s, 'Customer 11', %s);", (customer_id, cust_phone))

    set_auth_context(cur, owner_id, "authenticated")

    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("INSERT INTO public.customer_items (customer_id, owner_user_id, item_name, weight_grams, total_amount) VALUES (%s, %s, 'Direct Insert Item', 10, 50000);", (customer_id, owner_id))

    assert "permission denied for table customer_items" in str(exc_info.value).lower()
    conn.close()

def test_real_pg_15_worker_privilege_escalation_blocked():
    """
    Test worker privilege escalation prevention:
    A worker attempts to update their own profile to set account_type = 'owner' or modify owner_reference_id.
    Rejected by table privileges or trigger enforce_user_profile_security.
    """
    conn = get_db_conn()
    cur = conn.cursor()

    worker_id = str(uuid.uuid4())
    email = f"worker-esc-{uuid.uuid4()}@example.com"
    phone = f"+91{uuid.uuid4().int % 10000000000:010d}"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s);", (worker_id, email))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Worker Esc', %s, 'worker');", (worker_id, email, phone))

    set_auth_context(cur, worker_id, "authenticated")

    # Worker attempts to change account_type to owner
    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("UPDATE public.user_profiles SET account_type = 'owner' WHERE user_id = %s;", (worker_id,))

    err = str(exc_info.value).lower()
    assert ("permission denied for table user_profiles" in err or "cannot change account_type" in err)
    conn.close()

def test_real_pg_16_multi_item_payment_allocation():
    """
    Test multi-item payment allocation:
    Pay ₹40,000 split across two items (Item 1: ₹20,000, Item 2: ₹20,000).
    Verifies that both items' amount_paid and due_amount update accurately in a single atomic transaction.
    """
    conn = get_db_conn()
    cur = conn.cursor()

    owner_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())
    item1_id = str(uuid.uuid4())
    item2_id = str(uuid.uuid4())
    email = f"owner-multi-{uuid.uuid4()}@example.com"
    phone = f"+91{uuid.uuid4().int % 10000000000:010d}"
    cust_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s);", (owner_id, email))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Owner Multi', %s, 'owner');", (owner_id, email, phone))
    cur.execute("INSERT INTO public.customers (id, name, phone_number) VALUES (%s, 'Customer Multi', %s);", (customer_id, cust_phone))

    cur.execute("INSERT INTO public.customer_items (id, customer_id, owner_user_id, item_name, weight_grams, total_amount, amount_paid) VALUES (%s, %s, %s, 'Gold Bangle', 10, 30000, 0);", (item1_id, customer_id, owner_id))
    cur.execute("INSERT INTO public.customer_items (id, customer_id, owner_user_id, item_name, weight_grams, total_amount, amount_paid) VALUES (%s, %s, %s, 'Gold Coin', 5, 20000, 0);", (item2_id, customer_id, owner_id))

    set_auth_context(cur, owner_id, "authenticated")

    allocations = json.dumps([
        {"item_id": item1_id, "amount": 20000.0},
        {"item_id": item2_id, "amount": 20000.0}
    ])

    cur.execute("SELECT public.record_customer_payment(%s, %s, 'upi', %s::jsonb, NULL);", (customer_id, owner_id, allocations))
    res = cur.fetchone()[0]

    assert res["payment_amount"] == 40000.0
    assert len(res["items"]) == 2

    # Verify both item balances in database
    set_auth_context(cur, owner_id, "service_role")
    cur.execute("SELECT amount_paid, due_amount FROM public.customer_items WHERE id = %s;", (item1_id,))
    row1 = cur.fetchone()
    cur.execute("SELECT amount_paid, due_amount FROM public.customer_items WHERE id = %s;", (item2_id,))
    row2 = cur.fetchone()

    # Item 1: paid 20000, due 10000
    assert float(row1[0]) == 20000.0 and float(row1[1]) == 10000.0
    # Item 2: paid 20000, due 0 (fully paid)
    assert float(row2[0]) == 20000.0 and float(row2[1]) == 0.0

    conn.close()

def test_real_pg_17_supplier_allocation_ownership_integrity():
    """
    Test supplier payment allocation ownership integrity:
    Creates Shop A and Shop B.
    Creates a supplier transaction for Shop A.
    Creates a supplier item for Shop B.
    Attempts direct INSERT into supplier_payment_allocations connecting Shop A's transaction with Shop B's supplier item.
    Strictly verifies:
    - psycopg2 exception type is IntegrityError / ForeignKeyViolation
    - SQLSTATE is 23503
    - diag.constraint_name is 'fk_supplier_alloc_item'
    - Explicitly rolls back the failed transaction before querying
    - Verifies zero invalid allocation rows persisted.
    """
    conn = get_db_conn()
    conn.autocommit = False
    cur = conn.cursor()

    ownerA_id = str(uuid.uuid4())
    ownerB_id = str(uuid.uuid4())
    supplierA_id = str(uuid.uuid4())
    supplierB_id = str(uuid.uuid4())
    itemB_id = str(uuid.uuid4())
    txA_id = str(uuid.uuid4())

    emailA = f"ownerA-suppfk-{uuid.uuid4()}@example.com"
    emailB = f"ownerB-suppfk-{uuid.uuid4()}@example.com"
    phoneA = f"+91{uuid.uuid4().int % 10000000000:010d}"
    phoneB = f"+91{uuid.uuid4().int % 10000000000:010d}"
    suppA_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"
    suppB_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s), (%s, %s);", (ownerA_id, emailA, ownerB_id, emailB))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Owner A', %s, 'owner'), (%s, %s, 'Owner B', %s, 'owner');", (ownerA_id, emailA, phoneA, ownerB_id, emailB, phoneB))

    cur.execute("INSERT INTO public.suppliers (id, owner_user_id, supplier_name, phone_number) VALUES (%s, %s, 'Supplier A', %s), (%s, %s, 'Supplier B', %s);", (supplierA_id, ownerA_id, suppA_phone, supplierB_id, ownerB_id, suppB_phone))

    # Supplier Item B belongs to Shop B
    cur.execute("INSERT INTO public.supplier_items (id, supplier_id, owner_user_id, item_name, total_amount, amount_paid) VALUES (%s, %s, %s, 'Raw Gold B', 50000, 0);", (itemB_id, supplierB_id, ownerB_id))

    # Supplier Transaction A belongs to Shop A
    cur.execute("INSERT INTO public.supplier_transactions (id, supplier_id, owner_user_id, amount, payment_method) VALUES (%s, %s, %s, 10000, 'cash');", (txA_id, supplierA_id, ownerA_id))
    conn.commit()

    # Attempt to insert an allocation for Shop A's transaction linking Shop B's supplier item
    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("INSERT INTO public.supplier_payment_allocations (transaction_id, item_id, owner_user_id, allocated_amount) VALUES (%s, %s, %s, 10000);", (txA_id, itemB_id, ownerA_id))
        conn.commit()

    # 1. Verify exact psycopg2 exception types
    assert isinstance(exc_info.value, psycopg2.IntegrityError)
    assert isinstance(exc_info.value, psycopg2.errors.ForeignKeyViolation)

    # 2. Verify SQLSTATE 23503
    assert exc_info.value.pgcode == "23503"

    # 3. Verify diag.constraint_name == "fk_supplier_alloc_item"
    assert exc_info.value.diag.constraint_name == "fk_supplier_alloc_item"

    # 4. Roll back the failed transaction before querying
    conn.rollback()

    # 5. Verify zero invalid allocation rows persisted
    set_auth_context(cur, ownerA_id, "service_role")
    cur.execute("SELECT COUNT(*) FROM public.supplier_payment_allocations WHERE transaction_id = %s;", (txA_id,))
    assert cur.fetchone()[0] == 0

    conn.close()

def test_real_pg_18a_select_rls_isolation():
    """
    1. SELECT Row-Level Security Isolation Test:
    Verifies that RLS strictly scopes SELECT queries across all 10 tables:
    - Owner: PERMITTED (sees rows)
    - Approved Worker: PERMITTED (sees rows via approved membership)
    - Unapproved Worker: ZERO ROWS (silently filtered by RLS, no SQL error)
    - Unrelated Authenticated User: ZERO ROWS (silently filtered by RLS, no SQL error)
    - Service Role: PERMITTED (bypasses RLS, sees rows)
    """
    conn = get_db_conn()
    conn.autocommit = True
    cur = conn.cursor()

    ownerA_id = str(uuid.uuid4())
    ownerB_id = str(uuid.uuid4())
    approved_worker_id = str(uuid.uuid4())
    unapproved_worker_id = str(uuid.uuid4())
    unrelated_user_id = str(uuid.uuid4())

    emailOA = f"oa-rls-{uuid.uuid4()}@example.com"
    emailOB = f"ob-rls-{uuid.uuid4()}@example.com"
    emailWA = f"wa-rls-{uuid.uuid4()}@example.com"
    emailWU = f"wu-rls-{uuid.uuid4()}@example.com"
    emailUN = f"un-rls-{uuid.uuid4()}@example.com"

    phoneOA = f"+91{uuid.uuid4().int % 10000000000:010d}"
    phoneOB = f"+91{uuid.uuid4().int % 10000000000:010d}"
    phoneWA = f"+91{uuid.uuid4().int % 10000000000:010d}"
    phoneWU = f"+91{uuid.uuid4().int % 10000000000:010d}"
    phoneUN = f"+91{uuid.uuid4().int % 10000000000:010d}"

    cust_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"
    supp_phone = f"+91{uuid.uuid4().int % 10000000000:010d}"
    customer_id = str(uuid.uuid4())
    cust_item_id = str(uuid.uuid4())
    cust_tx_id = str(uuid.uuid4())
    cust_alloc_id = str(uuid.uuid4())

    supplier_id = str(uuid.uuid4())
    supp_item_id = str(uuid.uuid4())
    supp_tx_id = str(uuid.uuid4())
    supp_alloc_id = str(uuid.uuid4())

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s), (%s, %s), (%s, %s), (%s, %s), (%s, %s);",
                (ownerA_id, emailOA, ownerB_id, emailOB, approved_worker_id, emailWA, unapproved_worker_id, emailWU, unrelated_user_id, emailUN))

    cur.execute("""
        INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type, shop_name) VALUES
        (%s, %s, 'Owner A', %s, 'owner', 'Shop A'),
        (%s, %s, 'Owner B', %s, 'owner', 'Shop B'),
        (%s, %s, 'Approved Worker', %s, 'worker', NULL),
        (%s, %s, 'Unapproved Worker', %s, 'worker', NULL),
        (%s, %s, 'Unrelated User', %s, 'owner', 'Shop B');
    """, (ownerA_id, emailOA, phoneOA, ownerB_id, emailOB, phoneOB, approved_worker_id, emailWA, phoneWA, unapproved_worker_id, emailWU, phoneWU, unrelated_user_id, emailUN, phoneUN))

    cur.execute("INSERT INTO public.shop_memberships (owner_user_id, worker_user_id, status) VALUES (%s, %s, 'approved');",
                (ownerA_id, approved_worker_id))

    cur.execute("INSERT INTO public.customers (id, name, phone_number) VALUES (%s, 'Customer Matrix', %s);", (customer_id, cust_phone))
    cur.execute("INSERT INTO public.customer_items (id, customer_id, owner_user_id, item_name, weight_grams, total_amount, amount_paid) VALUES (%s, %s, %s, 'Ring A', 10, 50000, 10000);", (cust_item_id, customer_id, ownerA_id))
    cur.execute("INSERT INTO public.customer_transactions (id, receipt_number, customer_id, owner_user_id, amount, payment_method) VALUES (%s, %s, %s, %s, 10000, 'cash');", (cust_tx_id, f"REC-MTRX-{uuid.uuid4().hex[:8]}", customer_id, ownerA_id))
    cur.execute("INSERT INTO public.customer_payment_allocations (id, transaction_id, item_id, owner_user_id, allocated_amount) VALUES (%s, %s, %s, %s, 10000);", (cust_alloc_id, cust_tx_id, cust_item_id, ownerA_id))

    cur.execute("INSERT INTO public.suppliers (id, owner_user_id, supplier_name, phone_number) VALUES (%s, %s, 'Supplier Matrix', %s);", (supplier_id, ownerA_id, supp_phone))
    cur.execute("INSERT INTO public.supplier_items (id, supplier_id, owner_user_id, item_name, total_amount, amount_paid) VALUES (%s, %s, %s, 'Raw Gold A', 80000, 20000);", (supp_item_id, supplier_id, ownerA_id))
    cur.execute("INSERT INTO public.supplier_transactions (id, supplier_id, owner_user_id, amount, payment_method) VALUES (%s, %s, %s, 20000, 'bank_transfer');", (supp_tx_id, supplier_id, ownerA_id))
    cur.execute("INSERT INTO public.supplier_payment_allocations (id, transaction_id, item_id, owner_user_id, allocated_amount) VALUES (%s, %s, %s, %s, 20000);", (supp_alloc_id, supp_tx_id, supp_item_id, ownerA_id))

    target_tables = [
        ("user_profiles", "user_id", ownerA_id),
        ("shop_memberships", "owner_user_id", ownerA_id),
        ("customers", "id", customer_id),
        ("customer_items", "id", cust_item_id),
        ("customer_transactions", "id", cust_tx_id),
        ("customer_payment_allocations", "id", cust_alloc_id),
        ("suppliers", "id", supplier_id),
        ("supplier_items", "id", supp_item_id),
        ("supplier_transactions", "id", supp_tx_id),
        ("supplier_payment_allocations", "id", supp_alloc_id)
    ]

    roles = [
        ("Owner", ownerA_id, "authenticated", True),
        ("Approved Worker", approved_worker_id, "authenticated", True),
        ("Unapproved Worker", unapproved_worker_id, "authenticated", False),
        ("Unrelated User", unrelated_user_id, "authenticated", False),
        ("Service Role", ownerA_id, "service_role", True)
    ]

    for role_name, user_id, auth_role, should_permit_select in roles:
        set_auth_context(cur, user_id, auth_role)

        for tbl, id_col, target_id in target_tables:
            cur.execute(f"SELECT COUNT(*) FROM public.{tbl} WHERE {id_col} = %s;", (target_id,))
            count = cur.fetchone()[0]
            if should_permit_select:
                assert count > 0, f"Role {role_name} expected to SELECT from {tbl}, but got 0 rows"
            else:
                assert count == 0, f"Role {role_name} expected silent RLS filtering (0 rows) on {tbl}, but got {count} rows"

    conn.close()

def test_real_pg_18b_direct_table_write_privilege_restrictions():
    """
    2. Direct Table Write Privilege Restrictions Test:
    Verifies that direct INSERT, UPDATE, and DELETE operations are revoked on all 10 tables
    for all authenticated user contexts (Owner, Approved Worker, Unapproved Worker, Unrelated User).
    Strictly asserts SQLSTATE 42501 (InsufficientPrivilege).
    This proves table privilege hardening (GRANT/REVOKE), distinguished from RLS row filtering.
    """
    conn = get_db_conn()
    conn.autocommit = True
    cur = conn.cursor()

    owner_id = str(uuid.uuid4())
    worker_id = str(uuid.uuid4())
    unappr_id = str(uuid.uuid4())
    unrel_id = str(uuid.uuid4())

    emailO = f"o-priv-{uuid.uuid4()}@example.com"
    emailW = f"w-priv-{uuid.uuid4()}@example.com"
    emailU = f"u-priv-{uuid.uuid4()}@example.com"
    emailR = f"r-priv-{uuid.uuid4()}@example.com"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s), (%s, %s), (%s, %s), (%s, %s);",
                (owner_id, emailO, worker_id, emailW, unappr_id, emailU, unrel_id, emailR))

    cur.execute("""
        INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type, shop_name) VALUES
        (%s, %s, 'Owner Priv', '+919999700001', 'owner', 'Priv Shop'),
        (%s, %s, 'Worker Priv', '+919999700002', 'worker', NULL),
        (%s, %s, 'Unappr Priv', '+919999700003', 'worker', NULL),
        (%s, %s, 'Unrel Priv', '+919999700004', 'owner', 'Other Shop');
    """, (owner_id, emailO, worker_id, emailW, unappr_id, emailU, unrel_id, emailR))

    cur.execute("INSERT INTO public.shop_memberships (owner_user_id, worker_user_id, status) VALUES (%s, %s, 'approved');",
                (owner_id, worker_id))

    tables = [
        "user_profiles", "shop_memberships", "customers", "customer_items",
        "customer_transactions", "customer_payment_allocations", "suppliers",
        "supplier_items", "supplier_transactions", "supplier_payment_allocations"
    ]

    authenticated_users = [
        ("Owner", owner_id),
        ("Approved Worker", worker_id),
        ("Unapproved Worker", unappr_id),
        ("Unrelated User", unrel_id)
    ]

    for role_name, uid in authenticated_users:
        set_auth_context(cur, uid, "authenticated")

        for tbl in tables:
            dummy_id = str(uuid.uuid4())

            # 1. Direct INSERT rejected with 42501
            with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc_info:
                if tbl == "user_profiles":
                    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, 'dummy@ex.com', 'Dummy', '+919999999999', 'owner');", (dummy_id,))
                elif tbl == "shop_memberships":
                    cur.execute("INSERT INTO public.shop_memberships (owner_user_id, worker_user_id) VALUES (%s, %s);", (owner_id, dummy_id))
                elif tbl == "customers":
                    cur.execute("INSERT INTO public.customers (id, name, phone_number) VALUES (%s, 'Dummy', '+919999999998');", (dummy_id,))
                elif tbl == "customer_items":
                    cur.execute("INSERT INTO public.customer_items (id, customer_id, owner_user_id, item_name, weight_grams, total_amount) VALUES (%s, %s, %s, 'Dummy', 1, 100);", (dummy_id, dummy_id, owner_id))
                elif tbl == "customer_transactions":
                    cur.execute("INSERT INTO public.customer_transactions (id, receipt_number, customer_id, owner_user_id, amount, payment_method) VALUES (%s, 'REC-DUMMY', %s, %s, 100, 'cash');", (dummy_id, dummy_id, owner_id))
                elif tbl == "customer_payment_allocations":
                    cur.execute("INSERT INTO public.customer_payment_allocations (id, transaction_id, item_id, owner_user_id, allocated_amount) VALUES (%s, %s, %s, %s, 100);", (dummy_id, dummy_id, dummy_id, owner_id))
                elif tbl == "suppliers":
                    cur.execute("INSERT INTO public.suppliers (id, owner_user_id, supplier_name, phone_number) VALUES (%s, %s, 'Dummy', '+919999999997');", (dummy_id, owner_id))
                elif tbl == "supplier_items":
                    cur.execute("INSERT INTO public.supplier_items (id, supplier_id, owner_user_id, item_name, total_amount) VALUES (%s, %s, %s, 'Dummy', 100);", (dummy_id, dummy_id, owner_id))
                elif tbl == "supplier_transactions":
                    cur.execute("INSERT INTO public.supplier_transactions (id, supplier_id, owner_user_id, amount, payment_method) VALUES (%s, %s, %s, 100, 'cash');", (dummy_id, dummy_id, owner_id))
                elif tbl == "supplier_payment_allocations":
                    cur.execute("INSERT INTO public.supplier_payment_allocations (id, transaction_id, item_id, owner_user_id, allocated_amount) VALUES (%s, %s, %s, %s, 100);", (dummy_id, dummy_id, dummy_id, owner_id))
            assert exc_info.value.pgcode == "42501", f"{role_name} on {tbl} INSERT expected 42501, got {exc_info.value.pgcode}"

            # 2. Direct UPDATE rejected with 42501
            with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc_info:
                if tbl == "user_profiles":
                    cur.execute("UPDATE public.user_profiles SET name = 'Hacked' WHERE user_id = %s;", (owner_id,))
                else:
                    cur.execute(f"UPDATE public.{tbl} SET id = id WHERE id = %s;", (dummy_id,))
            assert exc_info.value.pgcode == "42501", f"{role_name} on {tbl} UPDATE expected 42501, got {exc_info.value.pgcode}"

            # 3. Direct DELETE rejected with 42501
            with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc_info:
                if tbl == "user_profiles":
                    cur.execute("DELETE FROM public.user_profiles WHERE user_id = %s;", (owner_id,))
                else:
                    cur.execute(f"DELETE FROM public.{tbl} WHERE id = %s;", (dummy_id,))
            assert exc_info.value.pgcode == "42501", f"{role_name} on {tbl} DELETE expected 42501, got {exc_info.value.pgcode}"

    conn.close()

def test_real_pg_18c_service_role_actual_crud_operations():
    """
    3. Service Role Actual Table Operations Test:
    Actually executes INSERT, UPDATE, and DELETE operations as service_role on all applicable tables,
    and explicitly verifies the persisted database effects at each step.
    """
    conn = get_db_conn()
    conn.autocommit = True
    cur = conn.cursor()

    owner_id = str(uuid.uuid4())
    worker_id = str(uuid.uuid4())
    email_o = f"o-crud-{uuid.uuid4()}@example.com"
    email_w = f"w-crud-{uuid.uuid4()}@example.com"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s), (%s, %s);", (owner_id, email_o, worker_id, email_w))

    set_auth_context(cur, owner_id, "service_role")

    # 1. user_profiles CRUD
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type, shop_name) VALUES (%s, %s, 'Crud Owner', %s, 'owner', 'Crud Shop');", (owner_id, email_o, rand_phone()))
    cur.execute("SELECT name FROM public.user_profiles WHERE user_id = %s;", (owner_id,))
    assert cur.fetchone()[0] == 'Crud Owner'

    cur.execute("UPDATE public.user_profiles SET name = 'Updated Crud Owner' WHERE user_id = %s;", (owner_id,))
    cur.execute("SELECT name FROM public.user_profiles WHERE user_id = %s;", (owner_id,))
    assert cur.fetchone()[0] == 'Updated Crud Owner'

    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Crud Worker', %s, 'worker');", (worker_id, email_w, rand_phone()))

    # 2. shop_memberships CRUD
    m_id = str(uuid.uuid4())
    cur.execute("INSERT INTO public.shop_memberships (id, owner_user_id, worker_user_id, status) VALUES (%s, %s, %s, 'pending');", (m_id, owner_id, worker_id))
    cur.execute("SELECT status FROM public.shop_memberships WHERE id = %s;", (m_id,))
    assert cur.fetchone()[0] == 'pending'

    cur.execute("UPDATE public.shop_memberships SET status = 'approved' WHERE id = %s;", (m_id,))
    cur.execute("SELECT status FROM public.shop_memberships WHERE id = %s;", (m_id,))
    assert cur.fetchone()[0] == 'approved'

    cur.execute("DELETE FROM public.shop_memberships WHERE id = %s;", (m_id,))
    cur.execute("SELECT COUNT(*) FROM public.shop_memberships WHERE id = %s;", (m_id,))
    assert cur.fetchone()[0] == 0

    # 3. customers CRUD
    c_id = str(uuid.uuid4())
    cur.execute("INSERT INTO public.customers (id, name, phone_number) VALUES (%s, 'Cust CRUD', %s);", (c_id, rand_phone()))
    cur.execute("SELECT name FROM public.customers WHERE id = %s;", (c_id,))
    assert cur.fetchone()[0] == 'Cust CRUD'

    cur.execute("UPDATE public.customers SET name = 'Cust CRUD Updated' WHERE id = %s;", (c_id,))
    cur.execute("SELECT name FROM public.customers WHERE id = %s;", (c_id,))
    assert cur.fetchone()[0] == 'Cust CRUD Updated'

    # 4. customer_items CRUD
    ci_id = str(uuid.uuid4())
    cur.execute("INSERT INTO public.customer_items (id, customer_id, owner_user_id, item_name, weight_grams, total_amount, amount_paid) VALUES (%s, %s, %s, 'Ring CRUD', 5, 25000, 5000);", (ci_id, c_id, owner_id))
    cur.execute("SELECT item_name FROM public.customer_items WHERE id = %s;", (ci_id,))
    assert cur.fetchone()[0] == 'Ring CRUD'

    cur.execute("UPDATE public.customer_items SET item_name = 'Ring CRUD Renamed' WHERE id = %s;", (ci_id,))
    cur.execute("SELECT item_name FROM public.customer_items WHERE id = %s;", (ci_id,))
    assert cur.fetchone()[0] == 'Ring CRUD Renamed'

    # 5. customer_transactions CRUD
    ctx_id = str(uuid.uuid4())
    cur.execute("INSERT INTO public.customer_transactions (id, receipt_number, customer_id, owner_user_id, amount, payment_method, notes) VALUES (%s, %s, %s, %s, 5000, 'cash', 'Initial note');", (ctx_id, f"REC-CRUD-{uuid.uuid4().hex[:8]}", c_id, owner_id))
    cur.execute("SELECT notes FROM public.customer_transactions WHERE id = %s;", (ctx_id,))
    assert cur.fetchone()[0] == 'Initial note'

    cur.execute("UPDATE public.customer_transactions SET notes = 'Updated note' WHERE id = %s;", (ctx_id,))
    cur.execute("SELECT notes FROM public.customer_transactions WHERE id = %s;", (ctx_id,))
    assert cur.fetchone()[0] == 'Updated note'

    # 6. customer_payment_allocations CRUD
    calloc_id = str(uuid.uuid4())
    cur.execute("INSERT INTO public.customer_payment_allocations (id, transaction_id, item_id, owner_user_id, allocated_amount) VALUES (%s, %s, %s, %s, 5000);", (calloc_id, ctx_id, ci_id, owner_id))
    cur.execute("SELECT allocated_amount FROM public.customer_payment_allocations WHERE id = %s;", (calloc_id,))
    assert float(cur.fetchone()[0]) == 5000.0

    cur.execute("UPDATE public.customer_payment_allocations SET allocated_amount = 4000 WHERE id = %s;", (calloc_id,))
    cur.execute("SELECT allocated_amount FROM public.customer_payment_allocations WHERE id = %s;", (calloc_id,))
    assert float(cur.fetchone()[0]) == 4000.0

    # Clean up customer chain
    cur.execute("DELETE FROM public.customer_payment_allocations WHERE id = %s;", (calloc_id,))
    cur.execute("SELECT COUNT(*) FROM public.customer_payment_allocations WHERE id = %s;", (calloc_id,))
    assert cur.fetchone()[0] == 0

    cur.execute("DELETE FROM public.customer_transactions WHERE id = %s;", (ctx_id,))
    cur.execute("SELECT COUNT(*) FROM public.customer_transactions WHERE id = %s;", (ctx_id,))
    assert cur.fetchone()[0] == 0

    cur.execute("DELETE FROM public.customer_items WHERE id = %s;", (ci_id,))
    cur.execute("SELECT COUNT(*) FROM public.customer_items WHERE id = %s;", (ci_id,))
    assert cur.fetchone()[0] == 0

    cur.execute("DELETE FROM public.customers WHERE id = %s;", (c_id,))
    cur.execute("SELECT COUNT(*) FROM public.customers WHERE id = %s;", (c_id,))
    assert cur.fetchone()[0] == 0

    # 7. suppliers CRUD
    s_id = str(uuid.uuid4())
    cur.execute("INSERT INTO public.suppliers (id, owner_user_id, supplier_name, phone_number) VALUES (%s, %s, 'Supp CRUD', %s);", (s_id, owner_id, rand_phone()))
    cur.execute("SELECT supplier_name FROM public.suppliers WHERE id = %s;", (s_id,))
    assert cur.fetchone()[0] == 'Supp CRUD'

    cur.execute("UPDATE public.suppliers SET supplier_name = 'Supp CRUD Updated' WHERE id = %s;", (s_id,))
    cur.execute("SELECT supplier_name FROM public.suppliers WHERE id = %s;", (s_id,))
    assert cur.fetchone()[0] == 'Supp CRUD Updated'

    # 8. supplier_items CRUD
    si_id = str(uuid.uuid4())
    cur.execute("INSERT INTO public.supplier_items (id, supplier_id, owner_user_id, item_name, total_amount, amount_paid) VALUES (%s, %s, %s, 'Bar CRUD', 50000, 10000);", (si_id, s_id, owner_id))
    cur.execute("SELECT item_name FROM public.supplier_items WHERE id = %s;", (si_id,))
    assert cur.fetchone()[0] == 'Bar CRUD'

    cur.execute("UPDATE public.supplier_items SET item_name = 'Bar CRUD Renamed' WHERE id = %s;", (si_id,))
    cur.execute("SELECT item_name FROM public.supplier_items WHERE id = %s;", (si_id,))
    assert cur.fetchone()[0] == 'Bar CRUD Renamed'

    # 9. supplier_transactions CRUD
    stx_id = str(uuid.uuid4())
    cur.execute("INSERT INTO public.supplier_transactions (id, supplier_id, owner_user_id, amount, payment_method, notes) VALUES (%s, %s, %s, 10000, 'cash', 'Supp Note');", (stx_id, s_id, owner_id))
    cur.execute("SELECT notes FROM public.supplier_transactions WHERE id = %s;", (stx_id,))
    assert cur.fetchone()[0] == 'Supp Note'

    cur.execute("UPDATE public.supplier_transactions SET notes = 'Updated Supp Note' WHERE id = %s;", (stx_id,))
    cur.execute("SELECT notes FROM public.supplier_transactions WHERE id = %s;", (stx_id,))
    assert cur.fetchone()[0] == 'Updated Supp Note'

    # 10. supplier_payment_allocations CRUD
    salloc_id = str(uuid.uuid4())
    cur.execute("INSERT INTO public.supplier_payment_allocations (id, transaction_id, item_id, owner_user_id, allocated_amount) VALUES (%s, %s, %s, %s, 10000);", (salloc_id, stx_id, si_id, owner_id))
    cur.execute("SELECT allocated_amount FROM public.supplier_payment_allocations WHERE id = %s;", (salloc_id,))
    assert float(cur.fetchone()[0]) == 10000.0

    cur.execute("UPDATE public.supplier_payment_allocations SET allocated_amount = 8000 WHERE id = %s;", (salloc_id,))
    cur.execute("SELECT allocated_amount FROM public.supplier_payment_allocations WHERE id = %s;", (salloc_id,))
    assert float(cur.fetchone()[0]) == 8000.0

    # Clean up supplier chain
    cur.execute("DELETE FROM public.supplier_payment_allocations WHERE id = %s;", (salloc_id,))
    cur.execute("SELECT COUNT(*) FROM public.supplier_payment_allocations WHERE id = %s;", (salloc_id,))
    assert cur.fetchone()[0] == 0

    cur.execute("DELETE FROM public.supplier_transactions WHERE id = %s;", (stx_id,))
    cur.execute("SELECT COUNT(*) FROM public.supplier_transactions WHERE id = %s;", (stx_id,))
    assert cur.fetchone()[0] == 0

    cur.execute("DELETE FROM public.supplier_items WHERE id = %s;", (si_id,))
    cur.execute("SELECT COUNT(*) FROM public.supplier_items WHERE id = %s;", (si_id,))
    assert cur.fetchone()[0] == 0

    cur.execute("DELETE FROM public.suppliers WHERE id = %s;", (s_id,))
    cur.execute("SELECT COUNT(*) FROM public.suppliers WHERE id = %s;", (s_id,))
    assert cur.fetchone()[0] == 0

    conn.close()

def test_real_pg_18d_security_definer_rpc_authorization():
    """
    4. SECURITY DEFINER RPC Authorization Test:
    Verifies RPC authorization across roles and ownership contexts:
    - Owner: PERMITTED
    - Approved Worker: PERMITTED
    - Unapproved Worker: REJECTED (Permission denied for this shop, 42501)
    - Unrelated User: REJECTED (Permission denied for this shop, 42501)
    - Invalid shop ownership: REJECTED (Permission denied for this shop, 42501)
    """
    conn = get_db_conn()
    conn.autocommit = True
    cur = conn.cursor()

    ownerA_id = str(uuid.uuid4())
    ownerB_id = str(uuid.uuid4())
    workerA_id = str(uuid.uuid4())
    workerU_id = str(uuid.uuid4())
    unrelated_id = str(uuid.uuid4())
    invalid_owner_id = str(uuid.uuid4())

    emailOA = f"oa-rpc-{uuid.uuid4()}@example.com"
    emailOB = f"ob-rpc-{uuid.uuid4()}@example.com"
    emailWA = f"wa-rpc-{uuid.uuid4()}@example.com"
    emailWU = f"wu-rpc-{uuid.uuid4()}@example.com"
    emailUN = f"un-rpc-{uuid.uuid4()}@example.com"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s), (%s, %s), (%s, %s), (%s, %s), (%s, %s);",
                (ownerA_id, emailOA, ownerB_id, emailOB, workerA_id, emailWA, workerU_id, emailWU, unrelated_id, emailUN))

    cur.execute("""
        INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type, shop_name) VALUES
        (%s, %s, 'Owner A', %s, 'owner', 'Shop A'),
        (%s, %s, 'Owner B', %s, 'owner', 'Shop B'),
        (%s, %s, 'Worker Appr', %s, 'worker', NULL),
        (%s, %s, 'Worker Unappr', %s, 'worker', NULL),
        (%s, %s, 'Unrelated', %s, 'owner', 'Shop B');
    """, (ownerA_id, emailOA, rand_phone(), ownerB_id, emailOB, rand_phone(), workerA_id, emailWA, rand_phone(), workerU_id, emailWU, rand_phone(), unrelated_id, emailUN, rand_phone()))

    cur.execute("INSERT INTO public.shop_memberships (owner_user_id, worker_user_id, status) VALUES (%s, %s, 'approved');",
                (ownerA_id, workerA_id))

    cust_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    cur.execute("INSERT INTO public.customers (id, name, phone_number) VALUES (%s, 'Cust A', %s);", (cust_id, rand_phone()))
    cur.execute("INSERT INTO public.customer_items (id, customer_id, owner_user_id, item_name, weight_grams, total_amount, amount_paid) VALUES (%s, %s, %s, 'Gold Chain', 10, 50000, 10000);", (item_id, cust_id, ownerA_id))

    supp_id = str(uuid.uuid4())
    s_item_id = str(uuid.uuid4())
    cur.execute("INSERT INTO public.suppliers (id, owner_user_id, supplier_name, phone_number) VALUES (%s, %s, 'Supp A', %s);", (supp_id, ownerA_id, rand_phone()))
    cur.execute("INSERT INTO public.supplier_items (id, supplier_id, owner_user_id, item_name, total_amount, amount_paid) VALUES (%s, %s, %s, 'Bullion', 100000, 20000);", (s_item_id, supp_id, ownerA_id))

    alloc_cust = json.dumps([{"item_id": item_id, "amount": 1000.0}])
    alloc_supp = json.dumps([{"item_id": s_item_id, "allocated_amount": 2000.0}])

    # 1. Owner A permitted
    set_auth_context(cur, ownerA_id, "authenticated")
    cur.execute("SELECT public.record_customer_payment(%s, %s, 'cash', %s::jsonb, NULL);", (cust_id, ownerA_id, alloc_cust))
    res1 = cur.fetchone()[0]
    assert res1["payment_amount"] == 1000.0

    cur.execute("SELECT public.record_supplier_payment(%s, %s, 2000.0, 'cash', %s::jsonb, NULL, NULL);", (ownerA_id, supp_id, alloc_supp))
    res2 = cur.fetchone()[0]
    assert res2["payment_amount"] == 2000.0

    # 2. Approved Worker permitted
    set_auth_context(cur, workerA_id, "authenticated")
    cur.execute("SELECT public.record_customer_payment(%s, %s, 'upi', %s::jsonb, NULL);", (cust_id, ownerA_id, alloc_cust))
    res3 = cur.fetchone()[0]
    assert res3["payment_amount"] == 1000.0

    cur.execute("SELECT public.record_supplier_payment(%s, %s, 2000.0, 'upi', %s::jsonb, NULL, NULL);", (ownerA_id, supp_id, alloc_supp))
    res4 = cur.fetchone()[0]
    assert res4["payment_amount"] == 2000.0

    # 3. Unapproved Worker rejected
    set_auth_context(cur, workerU_id, "authenticated")
    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("SELECT public.record_customer_payment(%s, %s, 'cash', %s::jsonb, NULL);", (cust_id, ownerA_id, alloc_cust))
    assert exc_info.value.pgcode == "42501"

    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("SELECT public.record_supplier_payment(%s, %s, 1000.0, 'cash', %s::jsonb, NULL, NULL);", (ownerA_id, supp_id, alloc_supp))
    assert exc_info.value.pgcode == "42501"

    # 4. Unrelated User rejected
    set_auth_context(cur, unrelated_id, "authenticated")
    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("SELECT public.record_customer_payment(%s, %s, 'cash', %s::jsonb, NULL);", (cust_id, ownerA_id, alloc_cust))
    assert exc_info.value.pgcode == "42501"

    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("SELECT public.record_supplier_payment(%s, %s, 1000.0, 'cash', %s::jsonb, NULL, NULL);", (ownerA_id, supp_id, alloc_supp))
    assert exc_info.value.pgcode == "42501"

    # 5. Invalid shop ownership rejected
    set_auth_context(cur, ownerA_id, "authenticated")
    # Non-existent shop owner UUID
    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("SELECT public.record_customer_payment(%s, %s, 'cash', %s::jsonb, NULL);", (cust_id, invalid_owner_id, alloc_cust))
    assert exc_info.value.pgcode == "42501"

    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("SELECT public.record_supplier_payment(%s, %s, 1000.0, 'cash', %s::jsonb, NULL, NULL);", (invalid_owner_id, supp_id, alloc_supp))
    assert exc_info.value.pgcode == "42501"

    # Owner A attempting to record payment for Shop B
    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("SELECT public.record_customer_payment(%s, %s, 'cash', %s::jsonb, NULL);", (cust_id, ownerB_id, alloc_cust))
    assert exc_info.value.pgcode == "42501"

    conn.close()

def test_real_pg_19_owner_single_shop_architecture():
    """
    Owner Single-Shop Architecture Test:
    Verifies that shop details are stored directly in user_profiles and no separate shops table exists.
    Verifies:
    1. An existing owner cannot create a second profile for the same user_id (enforced by user_profiles_pkey).
    2. A worker cannot be assigned to two owners (enforced by shop_memberships_worker_user_id_key).
    3. A worker cannot alter their owner assignment (direct table update revoked with 42501).
    4. Shop profile updates modify the existing owner profile in-place, without creating another shop or row.
    """
    conn = get_db_conn()
    conn.autocommit = False
    cur = conn.cursor()

    owner1_id = str(uuid.uuid4())
    owner2_id = str(uuid.uuid4())
    worker_id = str(uuid.uuid4())

    emailO1 = f"o1-single-{uuid.uuid4()}@example.com"
    emailO2 = f"o2-single-{uuid.uuid4()}@example.com"
    emailW = f"w-single-{uuid.uuid4()}@example.com"

    cur.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s), (%s, %s), (%s, %s);", (owner1_id, emailO1, owner2_id, emailO2, worker_id, emailW))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type, shop_name) VALUES (%s, %s, 'Owner One', %s, 'owner', 'Jewelers 1');", (owner1_id, emailO1, rand_phone()))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type, shop_name) VALUES (%s, %s, 'Owner Two', %s, 'owner', 'Jewelers 2');", (owner2_id, emailO2, rand_phone()))
    cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type) VALUES (%s, %s, 'Worker One', %s, 'worker');", (worker_id, emailW, rand_phone()))
    conn.commit()

    # 1. Existing owner cannot create a second profile for the same user_id
    with pytest.raises(psycopg2.errors.UniqueViolation) as exc_info:
        cur.execute("INSERT INTO public.user_profiles (user_id, email, name, phone_number, account_type, shop_name) VALUES (%s, 'o1-dup@example.com', 'Owner One Dup', %s, 'owner', 'Shop Dup');", (owner1_id, rand_phone()))
        conn.commit()
    assert exc_info.value.diag.constraint_name == "user_profiles_pkey"
    assert exc_info.value.pgcode == "23505"
    conn.rollback()

    # 2. Worker cannot be assigned to two owners
    cur.execute("INSERT INTO public.shop_memberships (owner_user_id, worker_user_id, status) VALUES (%s, %s, 'approved');", (owner1_id, worker_id))
    conn.commit()

    with pytest.raises(psycopg2.errors.UniqueViolation) as exc_info:
        cur.execute("INSERT INTO public.shop_memberships (owner_user_id, worker_user_id, status) VALUES (%s, %s, 'approved');", (owner2_id, worker_id))
        conn.commit()
    assert exc_info.value.diag.constraint_name == "shop_memberships_worker_user_id_key"
    assert exc_info.value.pgcode == "23505"
    conn.rollback()

    # 3. Worker cannot alter their owner assignment
    set_auth_context(cur, worker_id, "authenticated")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc_info:
        cur.execute("UPDATE public.shop_memberships SET owner_user_id = %s WHERE worker_user_id = %s;", (owner2_id, worker_id))
        conn.commit()
    assert exc_info.value.pgcode == "42501"
    conn.rollback()

    # 4. Shop profile updates modify the existing owner profile, not create another shop
    set_auth_context(cur, owner1_id, "service_role")
    cur.execute("SELECT COUNT(*) FROM public.user_profiles WHERE user_id = %s;", (owner1_id,))
    assert cur.fetchone()[0] == 1

    cur.execute("""
        UPDATE public.user_profiles
        SET shop_name = 'Updated Jewelers 1',
            city = 'Mumbai',
            state = 'Maharashtra'
        WHERE user_id = %s;
    """, (owner1_id,))
    conn.commit()

    cur.execute("SELECT shop_name, city, state FROM public.user_profiles WHERE user_id = %s;", (owner1_id,))
    row = cur.fetchone()
    assert row == ('Updated Jewelers 1', 'Mumbai', 'Maharashtra')

    # Total profiles for owner1_id remains exactly 1
    cur.execute("SELECT COUNT(*) FROM public.user_profiles WHERE user_id = %s;", (owner1_id,))
    assert cur.fetchone()[0] == 1

    conn.close()

