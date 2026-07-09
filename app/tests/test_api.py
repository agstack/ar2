import pytest
from fastapi.testclient import TestClient
from app.main import app
from unittest.mock import patch

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
    
    print("  -> Step 4: Fetching /resolve endpoint with Valid Token (L1)")
    res_l1 = client.get(f"/resolve/{geo_id}", headers={"Authorization": "Bearer valid-token"})
    print(f"  <- Response Code: {res_l1.status_code}, MaskingLevel: {res_l1.json().get('MaskingLevel')}")
    assert res_l1.status_code == 200
    assert res_l1.json()["MaskingLevel"] == "L1"
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
    
    print("  -> Fetching WKT with Valid Token (should return exact WKT)")
    res_l1 = client.get(f"/fetch-field-wkt/{geo_id}", headers={"Authorization": "Bearer valid-token"})
    wkt_snippet = res_l1.json().get('WKT')[:30] + "..." if res_l1.json().get('WKT') else "None"
    print(f"  <- Response Code: {res_l1.status_code}, WKT received: {wkt_snippet}")
    assert res_l1.status_code == 200
    assert res_l1.json()["WKT"] == TEST_POLYGON_WKT
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

    print("  -> Testing EUDR Export with valid token (should succeed with L1)")
    res_l1 = client.get(f"/geoid/{geo_id}/eudr-export", headers={"Authorization": "Bearer valid-token"})
    print(f"  <- Response Code: {res_l1.status_code}, MaskingLevel: {res_l1.json().get('MaskingLevel')}")
    assert res_l1.status_code == 200
    assert res_l1.json()["MaskingLevel"] == "L1"
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
    
    print("  -> Fetching centroid with valid token (should return L1 exact centroid)")
    res_l1 = client.get(f"/fetch-field-centroid/{geo_id}", headers={"Authorization": "Bearer valid-token"})
    print(f"  <- Response Code: {res_l1.status_code}, MaskingLevel: {res_l1.json().get('MaskingLevel')}, Centroid: {res_l1.json().get('Centroid')}")
    assert res_l1.status_code == 200
    assert res_l1.json()["MaskingLevel"] == "L1"
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
