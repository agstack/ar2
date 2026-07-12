import os
import json
import pytest
from fastapi.testclient import TestClient

# Mock environment variables BEFORE importing app components
# Allow overriding TESTKIT_DIR, defaulting to a relative path assuming pancake is checked out next to ar2
DEFAULT_TESTKIT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../pancake/services/pancake_services/grants/testkit/dev_keys"))
TESTKIT_DIR = os.getenv("TESTKIT_DIR", DEFAULT_TESTKIT_DIR)
os.environ["AR_TRUSTED_ISSUER_PUBKEY"] = os.path.join(TESTKIT_DIR, "dev_issuer_public.pem")
os.environ["TEST_STATUS_LIST_DIR"] = TESTKIT_DIR

from app.main import app
from app.database import Base, engine, get_db, SessionLocal
from app.models import GeoID
from unittest.mock import patch

# Override dependencies to decouple from external Hub auth
from app.auth import require_l1
app.dependency_overrides[require_l1] = lambda: {"sub": "test@demo.com"}

client = TestClient(app)

TEST_POLYGON_WKT = "POLYGON ((76.57207310199738 31.02188526513206,76.57203555107118 31.021319507765583,76.572362780571 31.021319507765583,76.57239496707916 31.02189446441107,76.57207310199738 31.02188526513206))"
TEST_POINT_WKT = "POINT (76.5720 31.0218)"

@pytest.fixture
def mock_auth():
    with patch('app.auth.verify_token') as mock_verify:
        def side_effect(token):
            if token == "valid-token":
                return {"sub": "test"}
            return None
        mock_verify.side_effect = side_effect
        yield mock_verify

def test_register_and_fetch_field(mock_auth):
    print("\n\n[TEST] Starting test_register_and_fetch_field")
    print("  -> Step 1: Attempting to register a Field Boundary")
    res = client.post(
        "/register-field-boundary",
        json={"wkt": TEST_POLYGON_WKT, "threshold": 95, "return_s2_indices": False}
    )
    print(f"  <- Response Code: {res.status_code}")
    assert res.status_code in [200, 400]
    data = res.json()
    geo_id = data.get("Geo Id") or data.get("detail", {}).get("Geo Id")
    if not geo_id:
        geo_id = data.get("matched geo ids", [None])[0]
        if not geo_id:
            geo_id = "test-geo-id"
    
    if res.status_code == 400:
        if "matched geo ids" in res.json().get("detail", {}):
            geo_id = res.json()["detail"]["matched geo ids"][0]
        elif "Geo Id" in res.json().get("detail", {}):
            geo_id = res.json()["detail"]["Geo Id"]

    print(f"  -> Extracted Geo ID for subsequent tests: {geo_id}")

    print("  -> Step 2: Fetching /resolve endpoint anonymously (No Auth)")
    res_l0 = client.get(f"/resolve/{geo_id}")
    print(f"  <- Response Code: {res_l0.status_code}, MaskingLevel: {res_l0.json().get('MaskingLevel')}")
    assert res_l0.status_code == 200
    assert res_l0.json()["MaskingLevel"] == "L0"
    
    print("  -> Step 3: Fetching /resolve endpoint with Garbage Token")
    res_l0_garbage = client.get(f"/resolve/{geo_id}", headers={"Authorization": "Bearer garbage-token"})
    print(f"  <- Response Code: {res_l0_garbage.status_code}, MaskingLevel: {res_l0_garbage.json().get('MaskingLevel')}")
    assert res_l0_garbage.status_code == 200
    assert res_l0_garbage.json()["MaskingLevel"] == "L0"
    
    print("  -> Step 4: Fetching /resolve endpoint with Valid Token (L0 without grant)")
    res_l1 = client.get(f"/resolve/{geo_id}", headers={"Authorization": "Bearer valid-token"})
    print(f"  <- Response Code: {res_l1.status_code}, MaskingLevel: {res_l1.json().get('MaskingLevel')}")
    assert res_l1.status_code == 200
    assert res_l1.json()["MaskingLevel"] == "L0"
    
    print("  -> Step 5: Fetching with Valid Token in AR_ACCOUNT_L1=legacy mode (L1)")
    os.environ["AR_ACCOUNT_L1"] = "legacy"
    res_l1_leg = client.get(f"/resolve/{geo_id}", headers={"Authorization": "Bearer valid-token"})
    assert res_l1_leg.status_code == 200
    assert res_l1_leg.json()["MaskingLevel"] == "L1"
    os.environ.pop("AR_ACCOUNT_L1", None)
    print("[TEST] test_register_and_fetch_field SUCCESS")

def test_fetch_field_wkt(mock_auth):
    print("\n\n[TEST] Starting test_fetch_field_wkt")
    res = client.post(
        "/register-field-boundary",
        json={"wkt": TEST_POLYGON_WKT, "threshold": 95, "return_s2_indices": False}
    )
    if res.status_code == 200:
        geo_id = res.json()["Geo Id"]
    else:
        geo_id = res.json()["detail"].get("Geo Id") or res.json()["detail"].get("matched geo ids")[0]

    print("  -> Fetching WKT anonymously (should be None)")
    res_l0 = client.get(f"/fetch-field-wkt/{geo_id}")
    print(f"  <- Response Code: {res_l0.status_code}, WKT received: {res_l0.json().get('WKT')}")
    assert res_l0.status_code == 200
    assert res_l0.json()["WKT"] is None
    
    print("  -> Fetching WKT with Valid Token (should return None because no grant)")
    res_l1 = client.get(f"/fetch-field-wkt/{geo_id}", headers={"Authorization": "Bearer valid-token"})
    assert res_l1.status_code == 200
    assert res_l1.json()["WKT"] is None
    print("[TEST] test_fetch_field_wkt SUCCESS")

def test_eudr_export(mock_auth):
    print("\n\n[TEST] Starting test_eudr_export")
    res = client.post(
        "/register-field-boundary",
        json={"wkt": TEST_POLYGON_WKT, "threshold": 95, "return_s2_indices": False}
    )
    if res.status_code == 200:
        geo_id = res.json()["Geo Id"]
    else:
        geo_id = res.json()["detail"].get("Geo Id") or res.json()["detail"].get("matched geo ids")[0]

    print("  -> Testing EUDR Export anonymously (should fail with 401)")
    res_l0 = client.get(f"/geoid/{geo_id}/eudr-export")
    print(f"  <- Response Code: {res_l0.status_code}, Detail: {res_l0.json().get('detail')}")
    assert res_l0.status_code == 401
    
    print("  -> Testing EUDR Export with garbage token (should fail with 401)")
    res_l0_garbage = client.get(f"/geoid/{geo_id}/eudr-export", headers={"Authorization": "Bearer garbage"})
    print(f"  <- Response Code: {res_l0_garbage.status_code}, Detail: {res_l0_garbage.json().get('detail')}")
    assert res_l0_garbage.status_code == 401

    print("  -> Testing EUDR Export with valid token (should fail with 401)")
    res_l1 = client.get(f"/geoid/{geo_id}/eudr-export", headers={"Authorization": "Bearer valid-token"})
    assert res_l1.status_code == 401
    print("[TEST] test_eudr_export SUCCESS")

def test_fetch_field_centroid(mock_auth):
    print("\n\n[TEST] Starting test_fetch_field_centroid")
    res = client.post(
        "/register-field-boundary",
        json={"wkt": TEST_POLYGON_WKT, "threshold": 95, "return_s2_indices": False}
    )
    if res.status_code == 200:
        geo_id = res.json()["Geo Id"]
    else:
        geo_id = res.json()["detail"].get("Geo Id") or res.json()["detail"].get("matched geo ids")[0]

    print("  -> Fetching centroid anonymously (should return L0 masked centroid)")
    res_l0 = client.get(f"/fetch-field-centroid/{geo_id}")
    print(f"  <- Response Code: {res_l0.status_code}, MaskingLevel: {res_l0.json().get('MaskingLevel')}, Centroid: {res_l0.json().get('Centroid')}")
    assert res_l0.status_code == 200
    assert res_l0.json()["MaskingLevel"] == "L0"
    
    print("  -> Fetching centroid with valid token (should return L0 masked centroid)")
    res_l1 = client.get(f"/fetch-field-centroid/{geo_id}", headers={"Authorization": "Bearer valid-token"})
    assert res_l1.status_code == 200
    assert res_l1.json()["MaskingLevel"] == "L0" 
    print("[TEST] test_fetch_field_centroid SUCCESS")

def test_register_duplicate_point():
    print("\n\n[TEST] Starting test_register_duplicate_point")
    print("  -> Registering a point initially")
    res1 = client.post("/register-point", json={"wkt": TEST_POINT_WKT})
    print(f"  <- Response Code: {res1.status_code}")
    assert res1.status_code in [200, 400]
    
    print("  -> Attempting to register the EXACT same point (duplicate)")
    res2 = client.post("/register-point", json={"wkt": TEST_POINT_WKT})
    print(f"  <- Response Code: {res2.status_code}")
    assert res2.status_code in [200, 400]
    
    data = res2.json() if res2.status_code == 200 else res2.json()["detail"]
    mask_level = data.get("MaskingLevel")
    print(f"  -> Extracted MaskingLevel from duplicate response: {mask_level}")
    
    assert mask_level == "L0"
    
    if "Geo JSON registered" in data:
        geo_type = data["Geo JSON registered"]["type"]
    else:
        geo_type = data["Geo JSON"]["type"]
        
    print(f"  -> Extracted Geometry Type from duplicate response: {geo_type} (Expected: Polygon for L0 masking, NOT exact Point)")
    assert geo_type == "Polygon"
    print("[TEST] test_register_duplicate_point SUCCESS")

def test_fetch_fields_for_a_point(mock_auth):
    print("\n\n[TEST] Starting test_fetch_fields_for_a_point")
    client.post(
        "/register-field-boundary",
        json={"wkt": TEST_POLYGON_WKT, "threshold": 95, "return_s2_indices": False}
    )
    
    print("  -> Fetching fields for a specific coordinate anonymously")
    res_l0 = client.post(
        "/fetch-fields-for-a-point",
        json={"latitude": 31.0215, "longitude": 76.5722, "s2_index": "13,20"}
    )
    print(f"  <- Response Code: {res_l0.status_code}")
    assert res_l0.status_code == 200
    fields = res_l0.json()["Fetched fields"]
    if fields:
        print(f"  -> Returned {len(fields)} fields. First field MaskingLevel: {fields[0]['MaskingLevel']}")
        assert fields[0]["MaskingLevel"] == "L0"
        
    print("  -> Fetching fields for a specific coordinate with valid token")
    res_l1 = client.post(
        "/fetch-fields-for-a-point",
        json={"latitude": 31.0215, "longitude": 76.5722, "s2_index": "13,20"},
        headers={"Authorization": "Bearer valid-token"}
    )
    print(f"  <- Response Code: {res_l1.status_code}")
    assert res_l1.status_code == 200
    fields_l1 = res_l1.json()["Fetched fields"]
    if fields_l1:
        print(f"  -> Returned {len(fields_l1)} fields. First field MaskingLevel: {fields_l1[0]['MaskingLevel']}")
        assert fields_l1[0]["MaskingLevel"] == "L1"
    print("[TEST] test_fetch_fields_for_a_point SUCCESS\n")

def test_network_error_degrades_to_l0():
    print("\n\n[TEST] Starting test_network_error_degrades_to_l0")
    with patch("app.auth.get_jwks_client") as mock_get_client:
        mock_get_client.side_effect = Exception("Network Error")
        
        res = client.post(
            "/register-field-boundary",
            json={"wkt": TEST_POLYGON_WKT, "threshold": 95, "return_s2_indices": False}
        )
        data = res.json()
        geo_id = data.get("Geo Id") or data.get("detail", {}).get("Geo Id")
        if not geo_id:
            geo_id = data.get("detail", {}).get("matched geo ids", [None])[0]
        
        res_fetch = client.get(f"/resolve/{geo_id}", headers={"Authorization": "Bearer some-token"})
        assert res_fetch.status_code == 200
        assert res_fetch.json()["MaskingLevel"] == "L0"
    print("[TEST] test_network_error_degrades_to_l0 SUCCESS\n")

def test_real_rs256_auth():
    print("\n\n[TEST] Starting test_real_rs256_auth")
    from cryptography.hazmat.primitives.asymmetric import rsa
    import jwt
    
    private_key1 = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    private_key2 = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    
    token1 = jwt.encode({"sub": "test1"}, private_key1, algorithm="RS256")
    token2 = jwt.encode({"sub": "test2"}, private_key2, algorithm="RS256")
    
    public_key1 = private_key1.public_key()
    
    class MockSigningKey:
        def __init__(self, key):
            self.key = key
            
    with patch("app.auth.get_jwks_client") as mock_get_client:
        class MockClient:
            def get_signing_key_from_jwt(self, token):
                return MockSigningKey(public_key1)
        mock_get_client.return_value = MockClient()
        
        res = client.post(
            "/register-field-boundary",
            json={"wkt": TEST_POLYGON_WKT, "threshold": 95, "return_s2_indices": False}
        )
        data = res.json()
        geo_id = data.get("Geo Id") or data.get("detail", {}).get("Geo Id")
        if not geo_id:
            geo_id = data.get("detail", {}).get("matched geo ids", [None])[0]
            
        res1 = client.get(f"/resolve/{geo_id}", headers={"Authorization": f"Bearer {token1}"})
        assert res1.status_code == 200
        assert res1.json()["MaskingLevel"] == "L0"
        
        os.environ["AR_ACCOUNT_L1"] = "legacy"
        res1_leg = client.get(f"/resolve/{geo_id}", headers={"Authorization": f"Bearer {token1}"})
        assert res1_leg.status_code == 200
        assert res1_leg.json()["MaskingLevel"] == "L1"
        os.environ.pop("AR_ACCOUNT_L1", None)
        
        res2 = client.get(f"/resolve/{geo_id}", headers={"Authorization": f"Bearer {token2}"})
        assert res2.status_code == 200
        assert res2.json()["MaskingLevel"] == "L0"
    print("[TEST] test_real_rs256_auth SUCCESS\n")


@pytest.fixture(scope="module")
def setup_database():
    db = SessionLocal()
    
    # We need to register a geoid matching the one in the credentials.
    # From manifest.json, the first geoid is:
    geoid_hex = "3f1a9f0f36e44c0cb1ad4c2f8e3a7d6b1c5e9d8f7a6b5c4d3e2f1a0b9c8d7e6f"
    geo_data = {
        "wkt": "POLYGON((0 0, 0 1, 1 1, 1 0, 0 0))" # Dummy
    }
    
    test_field = GeoID(
        geo_id=geoid_hex,
        geo_id_short="3f1a9f0f",
        content_hash="dummyhash",
        country="TestCountry",
        geo_data=geo_data,
        mask_level="L0"
    )
    db.add(test_field)
    db.commit()
    
    yield
    db.delete(test_field)
    db.commit()

def get_token(name):
    with open(os.path.join(TESTKIT_DIR, name)) as f:
        return f.read().strip()

def test_valid_grant(setup_database):
    geoid = "3f1a9f0f36e44c0cb1ad4c2f8e3a7d6b1c5e9d8f7a6b5c4d3e2f1a0b9c8d7e6f"
    token = get_token("valid.sdjwt")
    res = client.get(f"/fetch-field/{geoid}", headers={"X-Field-Grant": token})
    assert res.status_code == 200
    assert res.json()["MaskingLevel"] == "L1"

def test_expired_grant(setup_database):
    geoid = "3f1a9f0f36e44c0cb1ad4c2f8e3a7d6b1c5e9d8f7a6b5c4d3e2f1a0b9c8d7e6f"
    token = get_token("expired.sdjwt")
    res = client.get(f"/fetch-field/{geoid}", headers={"X-Field-Grant": token})
    assert res.status_code == 200
    assert res.json()["MaskingLevel"] == "L0"

def test_revoked_grant(setup_database):
    geoid = "3f1a9f0f36e44c0cb1ad4c2f8e3a7d6b1c5e9d8f7a6b5c4d3e2f1a0b9c8d7e6f"
    token = get_token("revoked.sdjwt")
    res = client.get(f"/fetch-field/{geoid}", headers={"X-Field-Grant": token})
    assert res.status_code == 200
    assert res.json()["MaskingLevel"] == "L0"
    
    res_eudr = client.get(f"/geoid/{geoid}/eudr-export", headers={"X-Field-Grant": token})
    assert res_eudr.status_code == 401

def test_tampered_grant(setup_database):
    geoid = "3f1a9f0f36e44c0cb1ad4c2f8e3a7d6b1c5e9d8f7a6b5c4d3e2f1a0b9c8d7e6f"
    token = get_token("tampered.sdjwt")
    res = client.get(f"/fetch-field/{geoid}", headers={"X-Field-Grant": token})
    assert res.status_code == 200
    assert res.json()["MaskingLevel"] == "L0"

def test_wrong_geoid_grant(setup_database):
    geoid = "3f1a9f0f36e44c0cb1ad4c2f8e3a7d6b1c5e9d8f7a6b5c4d3e2f1a0b9c8d7e6f"
    token = get_token("wrong_geoid.sdjwt")
    res = client.get(f"/fetch-field/{geoid}", headers={"X-Field-Grant": token})
    assert res.status_code == 200
    assert res.json()["MaskingLevel"] == "L0"

def test_valid_grant_fetch_wkt(setup_database):
    geoid = "3f1a9f0f36e44c0cb1ad4c2f8e3a7d6b1c5e9d8f7a6b5c4d3e2f1a0b9c8d7e6f"
    token = get_token("valid.sdjwt")
    res = client.get(f"/fetch-field-wkt/{geoid}", headers={"X-Field-Grant": token})
    assert res.status_code == 200
    assert res.json().get("WKT") is not None

def test_valid_grant_eudr_export(setup_database):
    geoid = "3f1a9f0f36e44c0cb1ad4c2f8e3a7d6b1c5e9d8f7a6b5c4d3e2f1a0b9c8d7e6f"
    token = get_token("valid.sdjwt")
    res = client.get(f"/geoid/{geoid}/eudr-export", headers={"X-Field-Grant": token})
    assert res.status_code == 200
    assert "GEO Id" in res.json()

def test_valid_grant_fetch_centroid(setup_database):
    geoid = "3f1a9f0f36e44c0cb1ad4c2f8e3a7d6b1c5e9d8f7a6b5c4d3e2f1a0b9c8d7e6f"
    token = get_token("valid.sdjwt")
    res = client.get(f"/fetch-field-centroid/{geoid}", headers={"X-Field-Grant": token})
    assert res.status_code == 200
    assert res.json()["MaskingLevel"] == "L1"
