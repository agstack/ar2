import json
import os

import pytest

from app.utils import Utils

VECTOR_FILE = os.path.join(os.path.dirname(__file__), "testkit", "geoid_v2_vectors.json")

def load_vectors():
    with open(VECTOR_FILE, "r") as f:
        return json.load(f)

@pytest.mark.parametrize("vector", load_vectors(), ids=lambda v: v["name"])
def test_generate_geo_id_v2(vector):
    wkt = vector["wkt"]
    expected_tokens = vector["expected_tokens"]
    expected_geoid = vector["expected_geoid"]
    
    tokens, geoid = Utils.generate_geo_id_v2_with_tokens(wkt)
    
    assert tokens == expected_tokens, f"Tokens mismatch for {vector['name']}"
    assert geoid == expected_geoid, f"GeoID mismatch for {vector['name']}"
    
    # Also verify the main function returns exactly the same
    assert Utils.generate_geo_id_v2(wkt) == expected_geoid
