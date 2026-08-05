import pytest
import uuid
import s2sphere as s2
from fastapi.testclient import TestClient

from app.main import app
from app.auth import require_hub_user
from app.database import SessionLocal
from app.models.geo_id_model import GeoID, ListParentEdge
import app.auth as auth
from app.main import app

from unittest.mock import patch

app.dependency_overrides[require_hub_user] = lambda: {"sub": "test@demo.com", "capabilities": ["trace-forward"]}

@pytest.fixture(autouse=True)
def mock_authorize_artifact():
    with patch("app.auth.authorize_artifact") as mock_auth:
        mock_auth.return_value = {"authorized": True, "used_authority": False}
        yield mock_auth

client = TestClient(app)

def create_test_geoid(cells: list[str]) -> str:
    db = SessionLocal()
    g_str = "urn:agstack:geoid:TEST_" + str(uuid.uuid4())[:8]
    g = GeoID(
        geo_id=g_str,
        geo_id_short="SHRT_" + str(uuid.uuid4())[:8],
        content_hash="HASH_" + str(uuid.uuid4())[:8],
        s2_cells=cells
    )
    db.add(g)
    db.commit()
    db.close()
    return g_str

def test_four_hop_reverse_lookup():
    # field -> shred-lot -> case -> pallet -> retail
    geoid = create_test_geoid(["89c259"])
    
    # 1. shred-lot
    r1 = client.post("/list-artifact", json={"members": [geoid]})
    shred_id = r1.json()["list_id"]
    
    # 2. case
    r2 = client.post("/list-artifact", json={"members": [f"L:{shred_id}"]})
    case_id = r2.json()["list_id"]
    
    # 3. pallet
    r3 = client.post("/list-artifact", json={"members": [f"L:{case_id}"]})
    pallet_id = r3.json()["list_id"]
    
    # 4. retail
    r4 = client.post("/list-artifact", json={"members": [f"L:{pallet_id}"]})
    retail_id = r4.json()["list_id"]
    
    # Reverse lookup
    res = client.post("/traceforward", json={"seed_geoid": geoid})
    assert res.status_code == 200
    returned = set(res.json()["list_ids"])
    
    # Should include all 4
    for lid in [shred_id, case_id, pallet_id, retail_id]:
        assert lid in returned

def test_manufactured_cycle_terminates():
    db = SessionLocal()
    geoid = create_test_geoid(["89c259"])
    
    r1 = client.post("/list-artifact", json={"members": [geoid]})
    l1 = r1.json()["list_id"]
    
    r2 = client.post("/list-artifact", json={"members": [f"L:{l1}"]})
    l2 = r2.json()["list_id"]
    
    # Manually insert l2 -> l1 to create cycle: l1 is child of l2, make l2 child of l1
    edge = ListParentEdge(child_list_id=l2, parent_list_id=l1)
    db.add(edge)
    db.commit()
    db.close()
    
    res = client.post("/traceforward", json={"seed_geoid": geoid})
    assert res.status_code == 200
    returned = set(res.json()["list_ids"])
    assert l1 in returned
    assert l2 in returned

def test_region_built_from_nested_list():
    geoid = create_test_geoid(["89c259"])
    
    r1 = client.post("/list-artifact", json={"members": [geoid]})
    l1 = r1.json()["list_id"]
    
    r2 = client.post("/list-artifact", json={"members": [f"L:{l1}"]})
    l2 = r2.json()["list_id"]
    
    # Region from l2 should include geoid's cells
    r_reg = client.post("/region-artifact", json={"members": [f"L:{l2}"]})
    assert r_reg.status_code == 200
    
    # Check already exists to verify idempotency too
    r_reg2 = client.post("/region-artifact", json={"members": [f"L:{l2}"]})
    assert r_reg2.json()["message"] == "RegionArtifact already exists"

def test_containment_ancestor_probe():
    # Use s2sphere to generate a level 8 token and a level 13 child token
    ll = s2.LatLng.from_degrees(36.0, -121.0)
    cid = s2.CellId.from_lat_lng(ll)
    
    parent_token = cid.parent(8).to_token()
    child_token = cid.parent(13).to_token()
    
    geoid = create_test_geoid([child_token])
    
    # We cheat the test by manually inserting a region cover cell for the parent token 
    # to simulate a WKT region that covers the parent.
    from app.models.geo_id_model import RegionArtifact, RegionCoverCell
    db = SessionLocal()
    reg_id = "test_ancestor_region_" + str(uuid.uuid4())[:8]
    db.add(RegionArtifact(region_id=reg_id))
    db.add(RegionCoverCell(region_id=reg_id, s2_cell=parent_token))
    
    # Also link it to a list so it's returned in reverse lookup
    from app.models.geo_id_model import RegionParentEdge
    list_id = "test_list_" + str(uuid.uuid4())[:8]
    from app.models.geo_id_model import ListArtifact
    db.add(ListArtifact(list_id=list_id))
    db.add(RegionParentEdge(child_region_id=reg_id, parent_list_id=list_id))
    db.commit()
    db.close()
    
    res = client.post("/traceforward", json={"seed_geoid": geoid})
    assert res.status_code == 200
    assert list_id in res.json()["list_ids"]

def test_region_normalization_determinism_and_wkt():
    # 4 sibling cells at level 13
    parent = s2.CellId.from_lat_lng(s2.LatLng.from_degrees(36.0, -121.0)).parent(12)
    children = [parent.child(i).to_token() for i in range(4)]
    
    g1 = create_test_geoid([children[0], children[1]])
    g2 = create_test_geoid([children[2], children[3]])
    
    r1 = client.post("/region-artifact", json={"members": [g1, g2]})
    assert r1.status_code == 200
    id1 = r1.json()["region_id"]
    
    # WKT registration
    # POLYGON around the area
    wkt = "POLYGON((-121.1 35.9, -121.1 36.1, -120.9 36.1, -120.9 35.9, -121.1 35.9))"
    r2 = client.post("/region-artifact", json={"wkt": wkt})
    assert r2.status_code == 200
    id2 = r2.json()["region_id"]
    
    # Test WKT idempotency
    r3 = client.post("/region-artifact", json={"wkt": wkt})
    assert r3.json()["message"] == "RegionArtifact already exists"
    assert r3.json()["region_id"] == id2

def test_expanding_composition():
    g1 = create_test_geoid(["89c259"])
    g2 = create_test_geoid(["89c25b"])
    
    # 1. ship lot A with g1
    rA = client.post("/list-artifact", json={"members": [g1]})
    list_A = rA.json()["list_id"]
    
    # 2. ship expanded lot B with g1 and g2
    rB = client.post("/list-artifact", json={"members": [g1, g2]})
    list_B = rB.json()["list_id"]
    
    # reverse on g1 finds both
    res = client.post("/traceforward", json={"seed_geoid": g1})
    returned = set(res.json()["list_ids"])
    assert list_A in returned
    assert list_B in returned

def test_three_list_reverse_lookup():
    common_geoid = create_test_geoid(["89c259"])
    lists = [
        [common_geoid, create_test_geoid(["111111"])],
        [common_geoid, create_test_geoid(["222222"])],
        [common_geoid, create_test_geoid(["333333"])]
    ]
    list_ids = []
    for members in lists:
        res = client.post("/list-artifact", json={"members": members})
        assert res.status_code == 200
        list_ids.append(res.json()["list_id"])
    
    assert len(set(list_ids)) == 3
    res_rev = client.post("/traceforward", json={"seed_geoid": common_geoid})
    assert res_rev.status_code == 200
    returned_lists = res_rev.json()["list_ids"]
    assert len(returned_lists) == 3
    assert set(returned_lists) == set(list_ids)

def test_pure_geoid_root_regression():
    members = [
        create_test_geoid(["aa"]),
        create_test_geoid(["bb"]),
        create_test_geoid(["cc"])
    ]
    res = client.post("/list-artifact", json={"members": members})
    assert res.status_code == 200
    list_id_new = res.json()["list_id"]
    from app.merkle import merkle_root, canonical_members
    expected = merkle_root(canonical_members(members))
    assert list_id_new == expected
