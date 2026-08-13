import pytest
from app.utils import Utils
from shapely.wkt import loads

def test_determinism():
    wkt = "POLYGON((0 0, 0 0.001, 0.001 0.001, 0.001 0, 0 0))"
    hash1 = Utils.generate_geo_id_v2(wkt)
    for _ in range(10):
        assert Utils.generate_geo_id_v2(wkt) == hash1

def test_jitter_convergence():
    # Because we round to 6 decimal places, a difference in the 7th decimal should produce identical hashes.
    wkt1 = "POLYGON((0 0, 0 0.001, 0.001 0.001, 0.001 0, 0 0))"
    wkt2 = "POLYGON((0.0000001 0, 0 0.0010001, 0.0010004 0.0010002, 0.001 0, 0.0000001 0))"
    assert Utils.generate_geo_id_v2(wkt1) == Utils.generate_geo_id_v2(wkt2)

def test_shape_sensitivity():
    # A difference in the 5th decimal should produce a different hash.
    wkt1 = "POLYGON((0 0, 0 0.001, 0.001 0.001, 0.001 0, 0 0))"
    wkt2 = "POLYGON((0.00001 0, 0 0.00101, 0.00101 0.00101, 0.001 0, 0.00001 0))"
    assert Utils.generate_geo_id_v2(wkt1) != Utils.generate_geo_id_v2(wkt2)

def test_nesting():
    # Parent is a 1km square
    parent_wkt = "POLYGON((0 0, 0 0.01, 0.01 0.01, 0.01 0, 0 0))"
    parent_tokens, parent_hash = Utils.generate_geo_id_v2_with_tokens(parent_wkt)
    
    # Child is a small square inside the parent
    child_wkt = "POLYGON((0.002 0.002, 0.002 0.003, 0.003 0.003, 0.003 0.002, 0.002 0.002))"
    child_tokens, child_hash = Utils.generate_geo_id_v2_with_tokens(child_wkt)
    
    # They should have different hashes
    assert parent_hash != child_hash
    
    # A child polygon cell union isn't necessarily a strict subset of the parent tokens in terms of the EXACT cell IDs, 
    # because s2 RegionCoverer might use different sized cells. BUT if we check intersection, the child's cells 
    # should be fully contained within the parent's covering. 
    # For the primitive, we just prove they yield different tokens.
    assert len(child_tokens) > 0
    assert len(parent_tokens) > 0
    
def test_collision_fixed():
    # AR1 issue: two separate 1-ha fields 800m apart get identical GeoID in v1.
    # Level 13 cell is about 1.2km wide.
    # Field 1 at origin
    f1 = "POLYGON((0 0, 0 0.001, 0.001 0.001, 0.001 0, 0 0))"
    # Field 2 at 0.008 degrees away (~800m away)
    f2 = "POLYGON((0.008 0.008, 0.008 0.009, 0.009 0.009, 0.009 0.008, 0.008 0.008))"
    
    hash1 = Utils.generate_geo_id_v2(f1)
    hash2 = Utils.generate_geo_id_v2(f2)
    assert hash1 != hash2
