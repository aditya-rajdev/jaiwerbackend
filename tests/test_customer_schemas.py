from decimal import Decimal
import pytest
from pydantic import ValidationError
from app.schemas.customers import AddCustomerRequest, CustomerItemFields

def test_valid_item_wise_add_customer():
    payload = AddCustomerRequest(
        name="Rahul Verma",
        phone_number="+919876543210",
        payment_mode="item_wise",
        payment_method="cash",
        items=[
            CustomerItemFields(
                item_name="Gold Ring 22K",
                weight_grams=Decimal("5.500"),
                rate_per_gram=Decimal("7200.00"),
                total_amount=Decimal("39600.00"),
                amount_paid=Decimal("10000.00")
            )
        ]
    )
    assert payload.name == "Rahul Verma"
    assert payload.items[0].item_name == "Gold Ring 22K"

def test_overpayment_validation():
    with pytest.raises(ValidationError):
        AddCustomerRequest(
            name="Overpay Customer",
            phone_number="+919876543210",
            payment_mode="total",
            total_payment=Decimal("50000.00"),  # Exceeds purchase total
            payment_method="cash",
            items=[
                CustomerItemFields(
                    item_name="Silver Chain",
                    weight_grams=Decimal("50.000"),
                    total_amount=Decimal("4000.00")
                )
            ]
        )
