import pytest
from pydantic import ValidationError
from app.schemas.shop_profile import ShopProfileUpdateRequest

def test_valid_shop_profile_update():
    payload = ShopProfileUpdateRequest(
        shop_name="Jaiwer Jewellers",
        pincode="380001",
        gstin="24ABCDE1234F1Z5"
    )
    assert payload.shop_name == "Jaiwer Jewellers"
    assert payload.pincode == "380001"
    assert payload.gstin == "24ABCDE1234F1Z5"

def test_invalid_pincode():
    with pytest.raises(ValidationError):
        ShopProfileUpdateRequest(pincode="38001")  # Must be 6 digits

def test_invalid_gstin():
    with pytest.raises(ValidationError):
        ShopProfileUpdateRequest(gstin="INVALIDGSTIN")  # Must be 15 chars
