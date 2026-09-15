"""The point regime: a plot declared by one coordinate gets a name that survives GPS jitter.

Regulation (EU) 2023/1115 Art. 2(28) lets a plot of at most four hectares be
described by a single point. Until 2026-09-15 AR2 named such a point by its
level-30 leaf cell (about 1 cm), which made every re-registration a new plot.
These tests pin the regime that replaced it: the level-20 cell (about 8 m).
"""

import math

import pytest
import s2geometry as s2g

from app import geoid_v2

# A coffee plot in the Honduran highlands, well inside a level-20 cell's interior
# so the jitter tests are about the regime and not about a cell edge.
LAT, LNG = 14.7500, -88.2500
METRE_DEG = 1 / 111_320  # degrees of latitude per metre


def _cell_centre(lat: float, lng: float, level: int) -> tuple[float, float]:
    cell = s2g.S2CellId(s2g.S2LatLng.FromDegrees(lat, lng)).parent(level)
    ll = cell.ToLatLng()
    return ll.lat().degrees(), ll.lng().degrees()


def test_the_point_identity_is_deterministic():
    tokens, geo_id = geoid_v2.point_geo_id_with_tokens(LAT, LNG)
    for _ in range(5):
        assert geoid_v2.point_geo_id_with_tokens(LAT, LNG) == (tokens, geo_id)
    assert len(geo_id) == 64


def test_the_point_is_named_by_its_level_20_cell_not_its_leaf():
    tokens, _ = geoid_v2.point_geo_id_with_tokens(LAT, LNG)
    assert len(tokens) == 1
    assert geoid_v2.token_level(tokens[0]) == geoid_v2.POINT_LEVEL == 20


def test_a_fix_one_metre_off_converges_on_the_same_name():
    """Two GPS fixes of one tree, a minute apart, are one plot."""
    lat, lng = _cell_centre(LAT, LNG, geoid_v2.POINT_LEVEL)
    _, here = geoid_v2.point_geo_id_with_tokens(lat, lng)
    for dlat, dlng in ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, -1)):
        _, there = geoid_v2.point_geo_id_with_tokens(
            lat + dlat * METRE_DEG, lng + dlng * METRE_DEG / math.cos(math.radians(lat))
        )
        assert there == here, (dlat, dlng)


def test_a_fix_fifty_metres_off_is_a_different_plot():
    _, here = geoid_v2.point_geo_id_with_tokens(LAT, LNG)
    _, there = geoid_v2.point_geo_id_with_tokens(LAT + 50 * METRE_DEG, LNG)
    assert there != here


def test_the_leaf_regime_would_have_split_the_same_fixes():
    """Documents why the level changed: the old rule failed the one-metre test."""
    lat, lng = _cell_centre(LAT, LNG, geoid_v2.POINT_LEVEL)
    leaf_here = s2g.S2CellId(s2g.S2LatLng.FromDegrees(lat, lng)).ToToken()
    leaf_there = s2g.S2CellId(s2g.S2LatLng.FromDegrees(lat + METRE_DEG, lng)).ToToken()
    assert leaf_here != leaf_there


def test_a_point_inside_a_field_sits_under_that_fields_cover():
    """Points and polygons share one namespace: the ancestor probe relates them."""
    half = 0.002  # ~220 m: a 19-ha square around the point
    field_wkt = (
        f"POLYGON(({LNG - half} {LAT - half}, {LNG - half} {LAT + half}, "
        f"{LNG + half} {LAT + half}, {LNG + half} {LAT - half}, {LNG - half} {LAT - half}))"
    )
    field_tokens, field_id = geoid_v2.geo_id_with_tokens(field_wkt)
    point_tokens, point_id = geoid_v2.point_geo_id_with_tokens(LAT, LNG)
    assert point_id != field_id

    point_cell = s2g.S2CellId.FromToken(point_tokens[0])
    field_cells = [s2g.S2CellId.FromToken(t) for t in field_tokens]
    assert any(c.contains(point_cell) or point_cell.contains(c) for c in field_cells)


def test_a_point_may_stand_for_at_most_four_hectares():
    assert geoid_v2.point_area_declared(None) == 0.0
    assert geoid_v2.point_area_declared(0.3) == 0.3
    assert geoid_v2.point_area_declared(4.0) == 4.0
    with pytest.raises(geoid_v2.GeometryUnusable, match="needs a polygon"):
        geoid_v2.point_area_declared(4.01)
    with pytest.raises(geoid_v2.GeometryUnusable):
        geoid_v2.point_area_declared(-1)
