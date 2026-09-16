"""The point regime: a plot declared by one coordinate, named like a field and resolved like one.

Regulation (EU) 2023/1115 Art. 2(28) lets a plot of at most four hectares be
described by a single point. A point is therefore a first-class plot here, and
these tests pin the two halves of that:

  * the name is a one-cell cover run through the same digest a polygon's cover
    is, so a List can hold points and fields without knowing which is which;
  * two fixes of one plot are brought together by distance at registration, the
    way two redraws of one field are brought together by IoU.

The regime tried between those two on 2026-09-15 -- naming a point by its level
20 cell so that jitter would land in the same cell -- is kept here as a
measurement rather than a story: test_the_grid_regime_splits_and_merges_by_luck
runs it over the coffee belt and reports what it actually did.
"""

import math
import random

import pytest
import s2geometry as s2g

from app import geoid_v2

# A coffee plot in the Honduran highlands.
LAT, LNG = 14.7500, -88.2500
METRE_DEG = 1 / 111_320  # degrees of latitude per metre


def _offset(lat: float, lng: float, metres: float, bearing_rad: float) -> tuple[float, float]:
    dlat = metres * math.cos(bearing_rad) * METRE_DEG
    dlng = metres * math.sin(bearing_rad) * METRE_DEG / math.cos(math.radians(lat))
    return lat + dlat, lng + dlng


def test_the_point_identity_is_deterministic():
    tokens, geo_id = geoid_v2.point_geo_id_with_tokens(LAT, LNG)
    for _ in range(5):
        assert geoid_v2.point_geo_id_with_tokens(LAT, LNG) == (tokens, geo_id)
    assert len(geo_id) == 64


def test_a_point_is_named_by_the_same_arithmetic_as_a_field():
    """One cover, one digest. If these ever diverge, a List stops being one namespace."""
    tokens, geo_id = geoid_v2.point_geo_id_with_tokens(LAT, LNG)
    assert len(tokens) == 1
    assert geoid_v2.token_level(tokens[0]) == geoid_v2.POINT_LEVEL == geoid_v2.LEAF_LEVEL
    assert geo_id == geoid_v2.digest(tokens)

    field_wkt = (
        f"POLYGON(({LNG - 0.002} {LAT - 0.002}, {LNG - 0.002} {LAT + 0.002}, "
        f"{LNG + 0.002} {LAT + 0.002}, {LNG + 0.002} {LAT - 0.002}, {LNG - 0.002} {LAT - 0.002}))"
    )
    field_tokens, field_id = geoid_v2.geo_id_with_tokens(field_wkt)
    assert field_id == geoid_v2.digest(field_tokens)
    assert len(field_id) == len(geo_id) == 64


def test_the_fix_keeps_its_precision():
    """A point a centimetre away is a different coordinate and says so."""
    _, here = geoid_v2.point_geo_id_with_tokens(LAT, LNG)
    _, there = geoid_v2.point_geo_id_with_tokens(LAT + 0.01 * METRE_DEG, LNG)
    assert there != here


def test_two_fixes_of_one_tree_are_one_plot():
    """The resolver's job, and it does not depend on where the fix sits in a cell."""
    for bearing in range(0, 360, 45):
        lat2, lng2 = _offset(LAT, LNG, 1.0, math.radians(bearing))
        assert geoid_v2.point_same_as(LAT, LNG, lat2, lng2), bearing
        assert geoid_v2.metres_between(LAT, LNG, lat2, lng2) == pytest.approx(1.0, abs=0.05)


def test_a_fix_fifty_metres_off_is_a_different_plot():
    lat2, lng2 = _offset(LAT, LNG, 50.0, 0.0)
    assert not geoid_v2.point_same_as(LAT, LNG, lat2, lng2)


def test_the_threshold_is_where_the_answer_changes():
    inside = _offset(LAT, LNG, geoid_v2.POINT_SAME_AS_METRES - 0.5, 1.0)
    outside = _offset(LAT, LNG, geoid_v2.POINT_SAME_AS_METRES + 0.5, 1.0)
    assert geoid_v2.point_same_as(LAT, LNG, *inside)
    assert not geoid_v2.point_same_as(LAT, LNG, *outside)
    # and an operator may move it without touching this module
    assert geoid_v2.point_same_as(LAT, LNG, *outside, threshold_m=30.0)


def test_the_neighbours_are_in_the_blocking_key():
    """A fix and its re-survey across a cell boundary must be shown to each other."""
    key = geoid_v2.point_blocking_key(LAT, LNG)
    assert len(key) == 9
    own = s2g.S2CellId(s2g.S2LatLng.FromDegrees(LAT, LNG)).parent(geoid_v2.POINT_BLOCKING_LEVEL)
    assert own.ToToken() in key

    # Walk east until the cell changes: the far side must still block together.
    step = 0.25
    metres = step
    while True:
        lat2, lng2 = _offset(LAT, LNG, metres, math.pi / 2)
        other = s2g.S2CellId(s2g.S2LatLng.FromDegrees(lat2, lng2)).parent(geoid_v2.POINT_BLOCKING_LEVEL)
        if other.ToToken() != own.ToToken():
            break
        metres += step
        assert metres < 50, "never left the cell"
    assert other.ToToken() in key
    assert geoid_v2.point_same_as(LAT, LNG, lat2, lng2), "a cell boundary is not a plot boundary"


def test_the_grid_regime_splits_and_merges_by_luck():
    """Why the name is not the level-20 cell any more, as a measurement.

    Quantizing to an 8 m cell looks like it should absorb a 1 m jitter. Over the
    coffee belt it does so about six times in seven, because a fix near a cell
    boundary crosses it; and it calls two plots 5 m apart one plot about four
    times in ten. A distance threshold has neither failure -- the assertions
    below are what this test is for, and the rates are printed for the record.
    """
    rng = random.Random(7)
    trials = 4000
    grid_kept = distance_kept = grid_merged = distance_merged = 0
    for _ in range(trials):
        lat = 14.0 + rng.random()
        lng = -89.0 + rng.random() * 2

        lat2, lng2 = _offset(lat, lng, 1.0, rng.random() * 2 * math.pi)
        same_cell = (
            s2g.S2CellId(s2g.S2LatLng.FromDegrees(lat, lng)).parent(20).ToToken()
            == s2g.S2CellId(s2g.S2LatLng.FromDegrees(lat2, lng2)).parent(20).ToToken()
        )
        grid_kept += same_cell
        distance_kept += geoid_v2.point_same_as(lat, lng, lat2, lng2)

        lat3, lng3 = _offset(lat, lng, 25.0, rng.random() * 2 * math.pi)
        grid_merged += (
            s2g.S2CellId(s2g.S2LatLng.FromDegrees(lat, lng)).parent(20).ToToken()
            == s2g.S2CellId(s2g.S2LatLng.FromDegrees(lat3, lng3)).parent(20).ToToken()
        )
        distance_merged += geoid_v2.point_same_as(lat, lng, lat3, lng3)

    print(
        f"\n1 m re-survey held: grid {grid_kept / trials:.1%}, distance {distance_kept / trials:.1%}"
        f"\n25 m apart merged: grid {grid_merged / trials:.1%}, distance {distance_merged / trials:.1%}"
    )
    assert grid_kept < trials, "the grid used to be sold as jitter-proof; it is not"
    assert distance_kept == trials, "every 1 m re-survey must resolve to the same plot"
    assert distance_merged == 0, "25 m apart is two plots at any threshold we would set"


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

    # and the containment the resolver reads: the fix is inside the boundary
    _, containment = geoid_v2.iou_and_containment(point_tokens, field_tokens)
    assert containment == 1.0


def test_a_registered_point_can_be_found_from_its_cover_alone():
    """The resolver never needs the geometry, which the caller may not be shown."""
    tokens, _ = geoid_v2.point_geo_id_with_tokens(LAT, LNG)
    assert geoid_v2.is_point_cover(tokens)
    lat, lng = geoid_v2.point_of_cover(tokens)
    assert geoid_v2.metres_between(LAT, LNG, lat, lng) < 0.05

    field_wkt = (
        f"POLYGON(({LNG - 0.002} {LAT - 0.002}, {LNG - 0.002} {LAT + 0.002}, "
        f"{LNG + 0.002} {LAT + 0.002}, {LNG + 0.002} {LAT - 0.002}, {LNG - 0.002} {LAT - 0.002}))"
    )
    assert not geoid_v2.is_point_cover(geoid_v2.geo_id_with_tokens(field_wkt)[0])


def test_a_point_may_stand_for_at_most_four_hectares():
    assert geoid_v2.point_area_declared(None) == 0.0
    assert geoid_v2.point_area_declared(0.3) == 0.3
    assert geoid_v2.point_area_declared(4.0) == 4.0
    with pytest.raises(geoid_v2.GeometryUnusable, match="needs a polygon"):
        geoid_v2.point_area_declared(4.01)
    with pytest.raises(geoid_v2.GeometryUnusable):
        geoid_v2.point_area_declared(-1)
