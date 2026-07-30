import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.auth import require_hub_user

# Mock auth to inject a test user
app.dependency_overrides[require_hub_user] = lambda: {"sub": "test@demo.com"}

client = TestClient(app)

def test_three_list_reverse_lookup():
    """
    Day 1 Closeout requirement:
    One GeoID in three distinct ListIDs -> reverse returns exactly those three.
    """
    common_geoid = "urn:agstack:geoid:COMMON_TEST_999"
    
    lists = [
        [common_geoid, "urn:agstack:geoid:UNIQUE_A"],
        [common_geoid, "urn:agstack:geoid:UNIQUE_B"],
        [common_geoid, "urn:agstack:geoid:UNIQUE_C"]
    ]
    
    list_ids = []
    for members in lists:
        res = client.post("/list-artifact", json={"members": members})
        assert res.status_code == 200
        list_ids.append(res.json()["list_id"])
        
    # Ensure they are distinct
    assert len(set(list_ids)) == 3
    
    # Perform reverse lookup
    res_rev = client.get(f"/list-artifact/reverse/{common_geoid}")
    assert res_rev.status_code == 200
    returned_lists = res_rev.json()["list_ids"]
    
    # Assert exactly the 3 distinct list IDs are returned
    assert len(returned_lists) == 3
    assert set(returned_lists) == set(list_ids)
