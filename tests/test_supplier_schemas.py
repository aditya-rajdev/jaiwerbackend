from decimal import Decimal
from uuid import uuid4
import pytest
from pydantic import ValidationError
from app.schemas.supplier import CreateSupplierRequest, SupplierItemRequest, RecordSupplierPaymentRequest, SupplierPaymentAllocation

def test_valid_supplier_creation():
    payload = CreateSupplierRequest(
        supplier_name="Ratan Bullion",
        phone_number="+919876543210",
        address="Gold Souk",
        payment_mode="item_wise",
        payment_method="bank_transfer",
        items=[
            SupplierItemRequest(
                item_name="Silver Bars 1KG",
                total_amount=Decimal("85000.00"),
                amount_paid=Decimal("20000.00")
            )
        ]
    )
    assert payload.supplier_name == "Ratan Bullion"
    assert payload.items[0].item_name == "Silver Bars 1KG"

def test_valid_supplier_payment():
    item_id = uuid4()
    payload = RecordSupplierPaymentRequest(
        supplier_id=uuid4(),
        amount=Decimal("15000.00"),
        payment_method="upi",
        allocations=[
            SupplierPaymentAllocation(
                item_id=item_id,
                allocated_amount=Decimal("15000.00")
            )
        ]
    )
    assert payload.amount == Decimal("15000.00")
    assert payload.allocations[0].item_id == item_id

def test_supplier_payment_allocation_mismatch():
    with pytest.raises(ValidationError):
        RecordSupplierPaymentRequest(
            supplier_id=uuid4(),
            amount=Decimal("20000.00"),  # Total is 20000
            payment_method="upi",
            allocations=[
                SupplierPaymentAllocation(
                    item_id=uuid4(),
                    allocated_amount=Decimal("15000.00")  # Allocations sum is 15000 != 20000
                )
            ]
        )
