"""Verification of the v2 primitive and the resolution arithmetic.

Run: python -m pytest migration/tests/test_primitive.py -v
or standalone: python migration/tests/test_primitive.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shapely.geometry import MultiPolygon, Polygon  # noqa: E402

from migration import geoid_v2 as g2  # noqa: E402
from migration.resolve import CHILD_OF, NEW, SAME_AS, iou_and_containment, resolve  # noqa: E402

D = 1 / 111_320.0  # degrees per metre at the equator


def square(lat, lng, side_m):
    s = side_m * D
    return Polygon([(lng, lat), (lng + s, lat), (lng + s, lat + s), (lng, lat + s)])


# ---------------------------------------------------------------- determinism

def test_deterministic():
    wkt = square(0, 0, 200).wkt
    first = g2.geo_id(wkt)
    assert all(g2.geo_id(wkt) == first for _ in range(20))


def test_sorted_tokens():
    tokens, _ = g2.geo_id_with_tokens(square(1, 1, 300).wkt)
    assert tokens == sorted(tokens)


def test_normalized_no_mergeable_siblings():
    """Four siblings at one level must never survive; that is what Normalize means."""
    tokens, _ = g2.geo_id_with_tokens(square(0, 0, 800).wkt)
    from collections import Counter
    by_parent = Counter()
    for t in tokens:
        lvl = g2.token_level(t)
        if lvl > 1:
            by_parent[(lvl, g2._ancestor_token(t, lvl - 1))] += 1
    assert not [k for k, n in by_parent.items() if n == 4]


# ---------------------------------------------------------------- the fixes

def test_holes_are_not_covered():
    """A polygon with a hole must not receive the solid polygon's identity."""
    outer = [(0, 0), (0.01, 0), (0.01, 0.01), (0, 0.01)]
    inner = [(0.003, 0.003), (0.007, 0.003), (0.007, 0.007), (0.003, 0.007)]
    solid = Polygon(outer)
    holed = Polygon(outer, [inner])

    assert g2.geo_id(solid.wkt) != g2.geo_id(holed.wkt)

    # and the hole must actually be absent from the cover, by area
    solid_leaves = g2.leaf_cells_from_tokens(g2.cover_tokens(solid.wkt))
    holed_leaves = g2.leaf_cells_from_tokens(g2.cover_tokens(holed.wkt))
    expected_ratio = 1 - (0.004 ** 2) / (0.01 ** 2)      # 0.84
    assert math.isclose(holed_leaves / solid_leaves, expected_ratio, rel_tol=0.02)


def test_multipolygon_uses_every_part():
    a = square(0, 0, 200)
    b = square(0, 0.01, 200)
    multi = MultiPolygon([a, b])

    assert g2.geo_id(multi.wkt) != g2.geo_id(a.wkt)
    assert g2.geo_id(multi.wkt) != g2.geo_id(b.wkt)

    both = g2.leaf_cells_from_tokens(g2.cover_tokens(multi.wkt))
    one = g2.leaf_cells_from_tokens(g2.cover_tokens(a.wkt))
    assert math.isclose(both / one, 2.0, rel_tol=0.05)


def test_part_order_does_not_matter():
    a, b = square(0, 0, 200), square(0, 0.01, 200)
    assert g2.geo_id(MultiPolygon([a, b]).wkt) == g2.geo_id(MultiPolygon([b, a]).wkt)


# ---------------------------------------------------------------- canonicalization

def test_winding_order_irrelevant():
    ccw = Polygon([(0, 0), (0.001, 0), (0.001, 0.001), (0, 0.001)])
    cw = Polygon([(0, 0), (0, 0.001), (0.001, 0.001), (0.001, 0)])
    assert g2.geo_id(ccw.wkt) == g2.geo_id(cw.wkt)


def test_duplicate_vertices_irrelevant():
    clean = Polygon([(0, 0), (0.001, 0), (0.001, 0.001), (0, 0.001)])
    dupes = Polygon([(0, 0), (0, 0), (0.001, 0), (0.001, 0.001), (0.001, 0.001), (0, 0.001)])
    assert g2.geo_id(clean.wkt) == g2.geo_id(dupes.wkt)


def test_collision_fixed_neighbouring_fields_differ():
    """The L13 defect: 1 ha fields a few hundred metres apart shared an identity."""
    a = square(0, 0, 100)
    for gap_m in (300, 500, 800):
        b = square(0, gap_m * D, 100)
        assert g2.geo_id(a.wkt) != g2.geo_id(b.wkt), f"collision at {gap_m} m"


def test_shape_not_bounding_box():
    """An L-shape must not hash to the rectangle that bounds it."""
    l_shape = Polygon([(0, 0), (0.002, 0), (0.002, 0.001), (0.001, 0.001),
                       (0.001, 0.002), (0, 0.002)])
    assert g2.geo_id(l_shape.wkt) != g2.geo_id(l_shape.envelope.wkt)


def test_unusable_geometry_refused():
    """Degenerate input must raise, never silently receive an identity."""
    import pytest
    for wkt in ("POLYGON ((0 0, 0 0, 0 0, 0 0))", "POLYGON EMPTY", "POINT (0 0)"):
        with pytest.raises(g2.GeometryUnusable):
            g2.geo_id(wkt)


# ---------------------------------------------------------------- IoU

def test_iou_tracks_geometry():
    side = 200
    a = g2.cover_tokens(square(0, 0, side).wkt)
    for frac in (0.02, 0.05, 0.10, 0.20, 0.50):
        b = g2.cover_tokens(square(0, frac * side * D, side).wkt)
        iou, _ = iou_and_containment(a, b)
        true = (1 - frac) / (1 + frac)
        assert abs(iou - true) < 0.06, f"offset {frac}: iou={iou:.4f} true={true:.4f}"


def test_iou_sees_ancestor_descendant_overlap():
    """The defect that leaf-weighting alone does not fix."""
    import s2geometry as s2g

    # take a real cell out of a real cover, then compare it to its own children
    tokens = g2.cover_tokens(square(10, 20, 400).wkt)
    parent_token = g2._ancestor_token(tokens[0], 16)
    base = s2g.S2CellId.FromToken(parent_token)

    coarse = [base.ToToken()]
    fine = [base.child(i).ToToken() for i in range(4)]

    assert not (set(coarse) & set(fine))          # no shared token at all
    iou, containment = iou_and_containment(coarse, fine)
    assert iou == 1.0 and containment == 1.0      # yet identical regions


def test_iou_invariants():
    a = g2.cover_tokens(square(0, 0, 200).wkt)
    assert iou_and_containment(a, a) == (1.0, 1.0)
    assert iou_and_containment([], a) == (0.0, 0.0)


def test_nesting_is_child_not_same():
    """P6: a plot inside a field resolves child_of, never same_as."""
    big = g2.cover_tokens(square(0, 0, 500).wkt)
    small = g2.cover_tokens(square(0.0005, 0.0005, 100).wkt)
    iou, containment = iou_and_containment(small, big)
    assert iou < 0.15 and containment > 0.90
    r = resolve(small, [("big", big)])
    assert r.outcome == CHILD_OF and r.canonical_geo_id == "big"


def test_resolve_outcomes():
    a = g2.cover_tokens(square(0, 0, 200).wkt)
    assert resolve(a, []).outcome == NEW
    assert resolve(a, [("x", a)]).outcome == SAME_AS
    far = g2.cover_tokens(square(0, 0.05, 200).wkt)
    assert resolve(a, [("x", far)]).outcome == NEW


def test_blocking_key_is_coarse_and_shared():
    """Near neighbours must share a blocking key or they would never be compared."""
    a = g2.cover_tokens(square(0, 0, 100).wkt)
    b = g2.cover_tokens(square(0, 200 * D, 100).wkt)
    assert set(g2.blocking_key(a)) & set(g2.blocking_key(b))
    assert all(g2.token_level(t) <= 13 for t in g2.blocking_key(a))


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v", "--tb=short"]))
