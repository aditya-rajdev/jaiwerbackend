import hashlib
import json
from uuid import uuid4

def compute_payload_hash(customer_id: str, payment_method: str, total_payment: float, allocations: list) -> str:
    payload_str = f"{customer_id}:{payment_method}:{total_payment}:{json.dumps(allocations, sort_keys=True)}"
    return hashlib.sha256(payload_str.encode('utf-8')).hexdigest()

def test_identical_payloads_produce_same_hash():
    customer_id = str(uuid4())
    allocations = [{"item_id": str(uuid4()), "amount": 5000.0}]

    hash1 = compute_payload_hash(customer_id, "cash", 5000.0, allocations)
    hash2 = compute_payload_hash(customer_id, "cash", 5000.0, allocations)

    assert hash1 == hash2

def test_differing_payloads_produce_different_hashes():
    customer_id = str(uuid4())
    allocations1 = [{"item_id": str(uuid4()), "amount": 5000.0}]
    allocations2 = [{"item_id": str(uuid4()), "amount": 6000.0}]

    hash1 = compute_payload_hash(customer_id, "cash", 5000.0, allocations1)
    hash2 = compute_payload_hash(customer_id, "cash", 6000.0, allocations2)

    assert hash1 != hash2
