from unittest.mock import MagicMock
from app.services.shop_profile import check_receipt_profile_completion

def test_receipt_profile_completion_success():
    mock_supabase = MagicMock()
    mock_supabase.table().select().eq().maybe_single().execute.return_value.data = {
        "name": "Aditya Owner",
        "phone_number": "+919876543210",
        "shop_name": "Jaiwer Jewellers",
        "shop_address": "Main Market Road",
        "city": "Ahmedabad",
        "state": "Gujarat",
        "pincode": "380001"
    }

    res = check_receipt_profile_completion(owner_user_id="owner-uuid-1", supabase=mock_supabase)
    assert res["is_complete"] is True
    assert res["missing_fields"] == []

def test_receipt_profile_completion_missing_fields():
    mock_supabase = MagicMock()
    mock_supabase.table().select().eq().maybe_single().execute.return_value.data = {
        "name": "Aditya Owner",
        "phone_number": "+919876543210",
        "shop_name": "",  # Empty
        "shop_address": None, # Missing
        "city": "Ahmedabad",
        "state": "Gujarat",
        "pincode": "380001"
    }

    res = check_receipt_profile_completion(owner_user_id="owner-uuid-1", supabase=mock_supabase)
    assert res["is_complete"] is False
    assert "shop_name" in res["missing_fields"]
    assert "shop_address" in res["missing_fields"]
