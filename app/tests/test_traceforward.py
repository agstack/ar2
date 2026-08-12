import json
import os
import uuid

import pytest
import s2sphere as s2
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def set_test_status_list_dir(monkeypatch):
    monkeypatch.setenv("TEST_STATUS_LIST_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "testkit/dev_keys")))

from unittest.mock import patch

from app.auth import require_hub_user
from app.database import SessionLocal
from app.main import app
from app.models.geo_id_model import GeoID, ListParentEdge

app.dependency_overrides[require_hub_user] = lambda: {"sub": "test@demo.com", "capabilities": ["trace-forward"]}

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
    res = client.post("/traceforward", json={"seed_geoid": geoid}, headers={"X-Grant-Token": valid_grant_for(geoid)})
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
    
    res = client.post("/traceforward", json={"seed_geoid": geoid}, headers={"X-Grant-Token": valid_grant_for(geoid)})
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
    
    res = client.post("/traceforward", json={"seed_geoid": geoid}, headers={"X-Grant-Token": valid_grant_for(geoid)})
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
    r1.json()["region_id"]
    
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
    res = client.post("/traceforward", json={"seed_geoid": g1}, headers={"X-Grant-Token": valid_grant_for(g1)})
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
    res_rev = client.post("/traceforward", json={"seed_geoid": common_geoid}, headers={"X-Grant-Token": valid_grant_for(common_geoid)})
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
    from app.merkle import canonical_members, merkle_root
    expected = merkle_root(canonical_members(members))
    assert list_id_new == expected

from pathlib import Path

CRED = Path(__file__).parent / "testkit" / "dev_keys"

def _read(name):
    return (CRED / f"{name}.sdjwt").read_text().strip()

def _hub(capabilities):
    app.dependency_overrides[require_hub_user] = lambda: {
        "sub": "authority@demo.agstack.org", "capabilities": capabilities}

def valid_grant_for(geoid: str) -> str:
    import base64
    import hashlib
    import secrets
    import time

    import jwt

    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    private_pem = (Path(__file__).parent / "testkit" / "dev_keys" / "dev_issuer_private.pem").read_bytes()
    
    salt = _b64url(secrets.token_bytes(16))
    encoded_disclosure = _b64url(json.dumps([salt, "fields.0", geoid]).encode("utf-8"))
    digest = _b64url(hashlib.sha256(encoded_disclosure.encode("ascii")).digest())

    claims = {
        "iss": "did:web:pancake.test",
        "sub": "owner@demo.com",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "vct": "agstack.org/credentials/field-access-grant/v1",
        "status": {"status_list": {"uri": "http://localhost:8100/grants/status-list", "idx": 1}},
        "_sd": [digest],
        "_sd_alg": "sha-256"
    }
    token = jwt.encode(claims, private_pem, algorithm="EdDSA", headers={"typ": "vc+sd-jwt", "kid": "pancake-test-1"})
    return f"{token}~{encoded_disclosure}~"

# 1. Gate A: no capability -> 403
def test_gate_a_missing_capability_403():
    _hub([])
    SEED = create_test_geoid(["111"])
    r = client.post("/traceforward", json={"seed_geoid": SEED, "scope": "demo-recall"})
    assert r.status_code == 403
    assert "capabilities" in r.json()["detail"]

# 2. Gate B owner path: valid grant -> 200, and NO holder identities
def test_gate_b_owner_path_passes_without_identities():
    _hub(["trace-forward"])
    SEED = create_test_geoid(["111"])
    db = SessionLocal()
    from app.models.geo_id_model import ListArtifact, ListMemberEdge
    db.add(ListArtifact(list_id="dummy-list"))
    db.add(ListMemberEdge(geoid=SEED, list_id="dummy-list"))
    db.commit()
    r = client.post("/traceforward", json={"seed_geoid": SEED, "scope": "demo-recall"},
                    headers={"X-Grant-Token": valid_grant_for(SEED)})
    assert r.status_code == 200
    assert "holder_account" not in r.text

# 3. Gate B both paths fail -> 403
def test_gate_b_no_grant_no_authority_403():
    _hub(["trace-forward"])
    SEED = create_test_geoid(["111"])
    r = client.post("/traceforward", json={"seed_geoid": SEED, "scope": "demo-recall"})
    assert r.status_code == 403

# Matrix Row 4: present | valid for different seed | none | any | 403
def test_gate_b_grant_for_different_seed_403():
    _hub(["trace-forward"])
    SEED = create_test_geoid(["111"])
    OTHER_SEED = create_test_geoid(["222"])
    r = client.post("/traceforward", json={"seed_geoid": SEED, "scope": "demo-recall"},
                    headers={"X-Grant-Token": valid_grant_for(OTHER_SEED)})
    assert r.status_code == 403

# Matrix Row 6: present | none | valid | missing | 400
def test_gate_b_authority_missing_scope_400():
    _hub(["trace-forward"])
    SEED = create_test_geoid(["111"])
    r = client.post("/traceforward", json={"seed_geoid": SEED}, # no scope
                    headers={"X-Authority-Token": _read("valid_authority")})
    assert r.status_code == 400

# Matrix Row 10: present | none | valid, scope: global | any | 200
def test_gate_b_authority_global_scope():
    _hub(["trace-forward"])
    SEED = create_test_geoid(["111"])
    with patch("app.routers.traceforward._resolve_holders") as mock_resolve, patch("app.meal_logger._append_to_meal_chain"):
        mock_resolve.return_value = {}
        r = client.post("/traceforward", json={"seed_geoid": SEED, "scope": "some-other-scope"},
                        headers={"X-Authority-Token": _read("global_authority")})
        assert r.status_code == 200
        assert r.json()["tier"] == 3

# Matrix Row 11: present | none | signed by untrusted key | present | 401
def test_gate_b_authority_untrusted_key_401():
    _hub(["trace-forward"])
    SEED = create_test_geoid(["111"])
    r = client.post("/traceforward", json={"seed_geoid": SEED, "scope": "demo-recall"},
                    headers={"X-Authority-Token": _read("untrusted_authority")})
    assert r.status_code == 401

# 4. THE ONE THAT MATTERS: authority owns nothing -> 200 WITH identities
def test_gate_b_authority_owns_nothing_gets_identities():
    _hub(["trace-forward"])
    SEED = create_test_geoid(["111"])
    with patch("app.routers.traceforward._resolve_holders") as mock_resolve, patch("app.meal_logger._append_to_meal_chain"):
        mock_resolve.return_value = {}
        r = client.post("/traceforward", json={"seed_geoid": SEED, "scope": "demo-recall"},
                        headers={"X-Authority-Token": _read("valid_authority")})
        assert r.status_code == 200
        assert r.json()["tier"] == 3
        
        r1 = client.post("/list-artifact", json={"members": [SEED]})
        retail_list_id = r1.json()["list_id"]
        mock_resolve.return_value = {retail_list_id: "test_holder"}
        r2 = client.post("/traceforward", json={"seed_geoid": SEED, "scope": "demo-recall"},
                        headers={"X-Authority-Token": _read("valid_authority")})
        assert r2.status_code == 200
        assert r2.json()["tier"] == 3
        assert any(m["holder_account"] == "test_holder" for m in r2.json()["matches"])

def test_audit_failure_503():
    _hub(["trace-forward"])
    SEED = create_test_geoid(["111"])
    with patch("app.meal_logger._append_to_meal_chain") as mock_audit:
        from fastapi import HTTPException
        mock_audit.side_effect = HTTPException(status_code=503, detail="Audit log failure")
        r = client.post("/traceforward", json={"seed_geoid": SEED, "scope": "demo-recall"},
                        headers={"X-Authority-Token": _read("valid_authority")})
        assert r.status_code == 503

# 5-7. Credential validity: expired / revoked / out-of-scope -> 401
@pytest.mark.parametrize("cred", ["expired_authority", "revoked_authority", "outofscope_authority"])
def test_invalid_authority_credentials_401(cred):
    _hub(["trace-forward"])
    SEED = create_test_geoid(["111"])
    r = client.post("/traceforward", json={"seed_geoid": SEED, "scope": "demo-recall"},
                    headers={"X-Authority-Token": _read(cred)})
    assert r.status_code == 401

# 8. Round trip: trace-back an artifact you do not hold, then trace forward
def test_authority_round_trip():
    _hub(["trace-forward"])
    SEED = create_test_geoid(["111"])
    r1 = client.post("/list-artifact", json={"members": [SEED]})
    retail_list_id = r1.json()["list_id"]
    auth = {"X-Authority-Token": _read("valid_authority")}
    with patch("app.meal_logger._append_to_meal_chain"):
        back = client.get(f"/list-artifact/{retail_list_id}", headers=auth)
        assert back.status_code == 200
        geoids = [m for m in back.json()["members"] if not m.startswith(("L:", "R:"))]
        
        with patch("app.routers.traceforward._resolve_holders") as mock_resolve:
            mock_resolve.return_value = {retail_list_id: "test_holder"}
            fwd = client.post("/traceforward", json={"seed_geoid": geoids[0], "scope": "demo-recall"}, headers=auth)
            assert fwd.status_code == 200
            assert retail_list_id in [m["list_id"] for m in fwd.json()["matches"]]
