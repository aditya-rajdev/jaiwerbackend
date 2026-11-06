from unittest.mock import MagicMock
import pytest
from fastapi import HTTPException
from app.services.shop_access import get_accessible_shop_owner_id

def test_owner_resolves_to_self():
    mock_supabase = MagicMock()
    mock_supabase.table().select().eq().maybe_single().execute.return_value.data = {
        "user_id": "owner-uuid-1",
        "account_type": "owner"
    }

    owner_id = get_accessible_shop_owner_id(user_id="owner-uuid-1", supabase=mock_supabase)
    assert owner_id == "owner-uuid-1"

def test_worker_with_approved_membership_resolves_to_owner():
    mock_supabase = MagicMock()
    # Profile query returns worker
    mock_supabase.table().select().eq().maybe_single().execute.return_value.data = {
        "user_id": "worker-uuid-1",
        "account_type": "worker"
    }
    # Membership query returns approved membership
    mock_supabase.table().select().eq().eq().execute.return_value.data = [
        {"owner_user_id": "owner-uuid-1"}
    ]

    owner_id = get_accessible_shop_owner_id(user_id="worker-uuid-1", supabase=mock_supabase)
    assert owner_id == "owner-uuid-1"

def test_worker_without_approved_membership_raises_403():
    mock_supabase = MagicMock()
    mock_supabase.table().select().eq().maybe_single().execute.return_value.data = {
        "user_id": "worker-uuid-unapproved",
        "account_type": "worker"
    }
    mock_supabase.table().select().eq().eq().execute.return_value.data = []

    with pytest.raises(HTTPException) as exc_info:
        get_accessible_shop_owner_id(user_id="worker-uuid-unapproved", supabase=mock_supabase)
    assert exc_info.value.status_code == 403
