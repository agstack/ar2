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

def test_pure_geoid_root_regression():
    """
    Day 2 requirement: pure-GeoID root regression (byte-identical).
    """
    members = [
        "urn:agstack:geoid:A",
        "urn:agstack:geoid:B",
        "urn:agstack:geoid:C"
    ]
    res = client.post("/list-artifact", json={"members": members})
    assert res.status_code == 200
    list_id_new = res.json()["list_id"]
    
    # Compare with known python merkle root
    from app.merkle import merkle_root, canonical_members
    expected = merkle_root(canonical_members(members))
    assert list_id_new == expected

def test_2_hop_reverse_lookup():
    """
    Day 2 requirement: field -> shred -> case (2 hops); g in R in RL
    """
    from app.database import SessionLocal
    from app.models.geo_id_model import GeoID
    import uuid

    db = SessionLocal()
    # 1. Create a GeoID with S2 cells
    test_geoid_str = "urn:agstack:geoid:TEST_HOP_" + str(uuid.uuid4())[:8]
    test_geoid = GeoID(
        geo_id=test_geoid_str,
        geo_id_short="SHRT" + str(uuid.uuid4())[:8],
        content_hash="HASH" + str(uuid.uuid4())[:8],
        s2_cells=["8085800000000000", "8085900000000000"]
    )
    db.add(test_geoid)
    db.commit()

    # 2. Register a Region (shred) by providing the GeoID (it will extract its S2 cells)
    res_reg = client.post("/region-artifact", json={"members": [test_geoid_str]})
    assert res_reg.status_code == 200
    region_id = res_reg.json()["region_id"]
    
    # Check idempotency
    res_reg2 = client.post("/region-artifact", json={"members": [test_geoid_str]})
    assert res_reg2.status_code == 200
    assert res_reg2.json()["message"] == "RegionArtifact already exists"

    # 3. Register a List (case) containing this Region
    res_list = client.post("/list-artifact", json={"members": [f"R:{region_id}"]})
    assert res_list.status_code == 200
    case_list_id = res_list.json()["list_id"]

    # 4. Perform reverse lookup for the GeoID
    res_rev = client.get(f"/list-artifact/reverse/{test_geoid_str}")
    assert res_rev.status_code == 200
    returned_lists = res_rev.json()["list_ids"]
    
    # 5. It should return the case_list_id (hop 1: GeoID -> Region, hop 2: Region -> List)
    assert case_list_id in returned_lists

