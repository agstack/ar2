"""GeoID v2 in the live registration path, and the exact cover comparison.

Three changes are pinned here.

  1. The identifier cascade is gone. Registration used to hash the L13 cover,
     fall back to hashing L20 on collision, and fall back to uuid4() on a second
     collision. These tests assert no registration can produce a UUID.

  2. Cover comparison is area-exact. Token-set intersection cannot see
     ancestor/descendant overlap, which is fatal once covers are normalized and
     therefore multi-level.

  3. The area pre-filter is gone. It gated the whole same_as branch behind
     `area_ratio >= threshold`, and area_ratio is 0 when either area is missing,
     so a field with no recorded area could never resolve however exactly its
     geometry matched.

The correctness of the primitive is asserted through AREA, not through stored
hashes. Conformance vectors are still valuable -- they catch accidental drift --
but a vector can be regenerated to match whatever the code now does, so a vector
alone proves nothing about correctness. The area assertions cannot be satisfied
by a wrong implementation.
"""

import hashlib
import math
import os
import random
import uuid

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Polygon

TESTKIT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "testkit/dev_keys"))
os.environ["AR_TRUSTED_ISSUER_PUBKEY"] = os.path.join(TESTKIT_DIR, "dev_issuer_public.pem")
os.environ["AR_TRUSTED_AUTHORITY_PUBKEY"] = os.path.join(TESTKIT_DIR, "authority_issuer_public.pem")

from app import geoid_v2  # noqa: E402
from app.auth import require_hub_user  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.geo_id_model import GeoID, GeoIDRegimeAlias, ListMemberEdge  # noqa: E402
from app.s2_services import S2Service  # noqa: E402
from app.utils import Utils  # noqa: E402

app.dependency_overrides[require_hub_user] = lambda: {
    "sub": "test@demo.com", "capabilities": ["trace-forward"]
}
client = TestClient(app)


@pytest.fixture(autouse=True)
def set_test_status_list_dir(monkeypatch):
    monkeypatch.setenv("TEST_STATUS_LIST_DIR", TESTKIT_DIR)


def _square(lat: float, lng: float, metres: float) -> str:
    dlat = metres / 111320.0
    dlng = metres / (111320.0 * math.cos(math.radians(lat)))
    return (f"POLYGON(({lng} {lat},{lng + dlng} {lat},{lng + dlng} {lat + dlat},"
            f"{lng} {lat + dlat},{lng} {lat}))")


# A patch of ocean, so these fixtures cannot overlap anything another suite
# registers near a real farm.
BASE_LAT, BASE_LNG = -34.5, -140.5

# Each run takes its own latitude band. A counter starting at zero would make the
# suite pass once and then fail against its own leftovers, because these fixtures
# insert rows directly and geo_id is unique -- identical geometry is, correctly,
# the same field. The tests have to be re-runnable against a database that
# already holds a previous run.
_offset = [random.randint(0, 2000) * 100]


def _fresh_square(metres: float = 200.0) -> str:
    _offset[0] += 1
    return _square(BASE_LAT + (_offset[0] % 200000) * 0.0001, BASE_LNG, metres)


# ==========================================================================
# The primitive: correctness asserted by area
# ==========================================================================

def test_a_hole_is_excluded_from_the_cover():
    """The old code passed only the exterior ring to S2Loop, so a field with a
    pond in it covered the pond and hashed identically to the solid shape."""
    outer = Polygon([(0, 0), (0.002, 0), (0.002, 0.002), (0, 0.002)])
    hole = Polygon([(0.0005, 0.0005), (0.0015, 0.0005), (0.0015, 0.0015), (0.0005, 0.0015)])
    with_hole = Polygon(outer.exterior.coords, [hole.exterior.coords])

    solid_area = geoid_v2.leaf_cells_from_tokens(geoid_v2.cover_tokens(outer.wkt))
    holed_area = geoid_v2.leaf_cells_from_tokens(geoid_v2.cover_tokens(with_hole.wkt))

    geometric_ratio = with_hole.area / outer.area          # 0.75 for this shape
    cover_ratio = holed_area / solid_area

    # A cover is a superset of the region, so boundary cells around the hole make
    # cover_ratio slightly larger than the true ratio. It must be far below 1.0,
    # which is what the old implementation returned.
    assert cover_ratio == pytest.approx(geometric_ratio, abs=0.06), cover_ratio
    assert cover_ratio < 0.9, "the hole is still being covered"
    assert geoid_v2.geo_id(with_hole.wkt) != geoid_v2.geo_id(outer.wkt)


def test_every_part_of_a_multipolygon_contributes():
    """The old code kept geoms[0] and dropped the rest silently, so a multi-part
    field took the identity of one fragment. This is not exotic: make_valid()
    *produces* MultiPolygons from self-intersecting input."""
    a = Polygon([(0, 0), (0.001, 0), (0.001, 0.001), (0, 0.001)])
    b = Polygon([(0.005, 0), (0.006, 0), (0.006, 0.001), (0.005, 0.001)])
    both = f"MULTIPOLYGON((({_ring(a)})),(({_ring(b)})))"

    area_a = geoid_v2.leaf_cells_from_tokens(geoid_v2.cover_tokens(a.wkt))
    area_b = geoid_v2.leaf_cells_from_tokens(geoid_v2.cover_tokens(b.wkt))
    area_both = geoid_v2.leaf_cells_from_tokens(geoid_v2.cover_tokens(both))

    assert area_both == pytest.approx(area_a + area_b, rel=0.02)
    assert area_both > area_a * 1.9, "only one part is being covered"
    assert geoid_v2.geo_id(both) != geoid_v2.geo_id(a.wkt)


def _ring(poly: Polygon) -> str:
    coords = list(poly.exterior.coords)
    return ",".join(f"{x} {y}" for x, y in coords)


def test_a_self_intersecting_polygon_keeps_both_lobes():
    """make_valid turns a bow tie into two triangles. Both are the field."""
    bow_tie = "POLYGON((0 0, 0.002 0.002, 0.002 0, 0 0.002, 0 0))"
    canon = geoid_v2.canonicalize(bow_tie)
    assert canon.geom_type == "MultiPolygon"
    assert len(canon.geoms) == 2

    both = geoid_v2.leaf_cells_from_tokens(geoid_v2.cover_tokens(bow_tie))
    one = geoid_v2.leaf_cells_from_tokens(geoid_v2.cover_tokens(canon.geoms[0].wkt))
    assert both > one * 1.8, "the second lobe is being dropped"


@pytest.mark.parametrize("bad", [
    "POINT(0 0)",
    "LINESTRING(0 0, 1 1)",
    "POLYGON((0 0, 0 0, 0 0, 0 0))",
    "POLYGON EMPTY",
])
def test_unusable_geometry_raises_rather_than_inventing_an_id(bad):
    """The whole point of retiring the UUID fallback: refuse, do not invent.

    A specific exception type, so a caller can distinguish "this geometry cannot
    have an identity" from a bug in the covering code.
    """
    with pytest.raises(geoid_v2.GeometryUnusable):
        geoid_v2.geo_id(bad)


def test_the_two_implementations_agree():
    """app/geoid_v2.py and migration/geoid_v2.py are separate copies -- one runs
    in the service, one in the standalone import pipeline. If they ever disagree,
    a field imported by the migration gets a different identity than the same
    field registered through the API, which is silent and unrecoverable."""
    migration_impl = pytest.importorskip("migration.geoid_v2")
    for metres in (50.0, 200.0, 800.0):
        wkt = _square(36.6, -121.9, metres)
        assert geoid_v2.geo_id(wkt) == migration_impl.geo_id(wkt)
        assert geoid_v2.cover_tokens(wkt) == migration_impl.cover_tokens(wkt)
        assert geoid_v2.blocking_key(geoid_v2.cover_tokens(wkt)) == \
               migration_impl.blocking_key(migration_impl.cover_tokens(wkt))


# ==========================================================================
# Cover comparison
# ==========================================================================

def test_exact_iou_sees_overlap_that_token_sets_cannot():
    """Two 200 m squares offset 10 m have a true IoU near 0.905.

    Token-set intersection scores them near 0.28, because a normalized cover is
    multi-level and one cell's overlap with its own children shares no token.
    This gap is the entire bug: at a 95% threshold both are rejected, but at any
    threshold in between the two methods disagree about whether two surveys of
    one field are the same field.
    """
    a = geoid_v2.cover_tokens(_square(36.6, -121.9, 200))
    b = geoid_v2.cover_tokens(_square(36.6 + 10 / 111320.0, -121.9, 200))

    exact, _ = geoid_v2.iou_and_containment(a, b)
    token_set = len(set(a) & set(b)) / len(set(a) | set(b))

    assert exact == pytest.approx(0.905, abs=0.03), exact
    assert token_set < 0.4
    assert exact > token_set * 2


def test_identical_covers_score_exactly_one():
    tokens = geoid_v2.cover_tokens(_square(36.6, -121.9, 200))
    assert geoid_v2.iou_and_containment(tokens, tokens) == (1.0, 1.0)


def test_containment_detects_nesting_where_iou_does_not():
    outer = geoid_v2.cover_tokens(_square(36.6, -121.9, 400))
    inner = geoid_v2.cover_tokens(_square(36.6005, -121.8995, 100))

    iou, containment = geoid_v2.iou_and_containment(outer, inner)
    assert containment > 0.95, "the small field is inside the large one"
    assert iou < 0.2, "and it is nowhere near the same field"


def test_uniform_level_covers_match_the_old_arithmetic_exactly():
    """The guarantee that makes this change safe to adopt.

    Every v1 cover sits at a single level. On single-level covers, cell counting
    and leaf-weighted union intersection give the same number, so no existing v1
    resolution decision can change.
    """
    for metres in (100.0, 300.0):
        wkt_a = _square(36.6, -121.9, metres)
        wkt_b = _square(36.6 + 20 / 111320.0, -121.9, metres)
        a = S2Service.wkt_to_cell_tokens(wkt_a, 20)
        b = S2Service.wkt_to_cell_tokens(wkt_b, 20)

        old = len(set(a) & set(b)) / len(set(a) | set(b))
        new, _ = geoid_v2.iou_and_containment(a, b)
        assert new == pytest.approx(old, abs=1e-12), f"{metres} m: {new} vs {old}"


def test_leaf_area_is_exact_across_mixed_levels():
    """A cell at level L is exactly 4**(30-L) leaves. Cell counts are not area
    once levels are mixed, which is the second half of the old defect."""
    cover = geoid_v2.cover_tokens(_square(36.6, -121.9, 400))
    levels = {geoid_v2.token_level(t) for t in cover}
    assert len(levels) > 1, "this cover should be multi-level, or the test is moot"

    expected = sum(4 ** (30 - geoid_v2.token_level(t)) for t in cover)
    assert geoid_v2.leaf_cells_from_tokens(cover) == expected


# ==========================================================================
# The area pre-filter, which used to disable resolution silently
# ==========================================================================

def _register_direct(wkt: str, *, area_ha: float | None) -> str:
    """Insert a field the way the importer does, with area possibly absent."""
    db = SessionLocal()
    try:
        tokens, geo_id = geoid_v2.geo_id_with_tokens(wkt)
        # content_hash is String(64) and unique; salt inside the digest rather
        # than by concatenation, so repeated fixtures do not collide.
        salted = hashlib.sha256((wkt + uuid.uuid4().hex).encode()).hexdigest()
        db.add(GeoID(
            geo_id=geo_id,
            geo_id_short=geoid_v2.geo_id_short(geo_id),
            content_hash=salted,
            boundary_type="field",
            area_ha_approx=area_ha,
            s2_cells=tokens,
            geo_data={"wkt": wkt, geoid_v2.COVER_KEY: tokens},
        ))
        db.commit()
        return geo_id
    finally:
        db.close()


@pytest.mark.parametrize("stored_area", [None, 0.0, 4.0])
def test_a_field_with_no_recorded_area_still_resolves(stored_area):
    """The regression this replaces.

    `area_ratio` is 0 whenever either area is missing, and it gated the entire
    same_as branch, so an imported field with absent area could never resolve to
    an existing one however exactly the geometry matched. Every such import
    became a duplicate. Resolution now depends on geometry alone.
    """
    wkt = _fresh_square()
    existing = _register_direct(wkt, area_ha=stored_area)

    db = SessionLocal()
    try:
        tokens = geoid_v2.cover_tokens(wkt)
        matches = Utils.check_percentage_match(
            db, [existing], tokens, 20, threshold=95, area_new=0.0
        )
    finally:
        db.close()

    assert [m[0] for m in matches["same_as"]] == [existing], (
        f"identical geometry must resolve with stored area {stored_area!r} "
        f"and no incoming area"
    )


def test_resolution_still_refuses_a_genuinely_different_field():
    """The counterpart: removing the area gate must not make everything match."""
    near = _fresh_square()
    far = _square(BASE_LAT + 5.0, BASE_LNG + 5.0, 200)
    existing = _register_direct(near, area_ha=None)

    db = SessionLocal()
    try:
        matches = Utils.check_percentage_match(
            db, [existing], geoid_v2.cover_tokens(far), 20, threshold=95
        )
    finally:
        db.close()

    assert matches["same_as"] == []
    assert matches["child_of"] == []


# ==========================================================================
# The live registration path
# ==========================================================================

def test_registration_returns_the_v2_identifier():
    wkt = _fresh_square(120)
    r = client.post("/register-field-boundary",
                    json={"wkt": wkt, "threshold": 95, "return_s2_indices": False})
    assert r.status_code == 200, r.text
    assert r.json()["Geo Id"] == geoid_v2.geo_id(wkt), (
        "the registered identifier must be recomputable from the geometry alone"
    )


def test_registration_never_issues_a_uuid():
    """The cascade's last resort was uuid4(): an identifier derived from nothing,
    indistinguishable downstream from a real one. Registering the same geometry
    repeatedly is what used to trigger it."""
    wkt = _fresh_square(120)
    ids = []
    for _ in range(3):
        r = client.post("/register-field-boundary",
                        json={"wkt": wkt, "threshold": 95, "return_s2_indices": False})
        assert r.status_code == 200, r.text
        ids.append(r.json()["Geo Id"])

    for got in ids:
        with pytest.raises(ValueError):
            uuid.UUID(got)
        assert got == geoid_v2.geo_id(wkt)

    assert len(set(ids)) == 1, "the same field must keep one identity"


def test_two_nearby_fields_get_different_identifiers():
    """The collision the L13 hash produced. An L13 cell is roughly 1.2 km across,
    so two fields 300 m apart shared an identifier and the cascade escaped into a
    UUID."""
    a = _fresh_square(100)
    b = _square(BASE_LAT + (_offset[0] % 200000) * 0.0001 + 0.003, BASE_LNG, 100)

    ra = client.post("/register-field-boundary",
                     json={"wkt": a, "threshold": 95, "return_s2_indices": False})
    rb = client.post("/register-field-boundary",
                     json={"wkt": b, "threshold": 95, "return_s2_indices": False})
    assert ra.status_code == rb.status_code == 200

    assert ra.json()["Geo Id"] != rb.json()["Geo Id"]
    # and they really do share an L13 cell, or the test proves nothing
    la = set(S2Service.wkt_to_cell_tokens(a, 13))
    lb = set(S2Service.wkt_to_cell_tokens(b, 13))
    assert la & lb, "pick closer fixtures: these do not collide at L13"


def test_the_stored_cover_is_the_canonical_one():
    wkt = _fresh_square(120)
    r = client.post("/register-field-boundary",
                    json={"wkt": wkt, "threshold": 95, "return_s2_indices": False})
    geo_id = r.json()["Geo Id"]

    db = SessionLocal()
    try:
        row = db.query(GeoID).filter(GeoID.geo_id == geo_id).one()
        assert row.geo_data[geoid_v2.COVER_KEY] == geoid_v2.cover_tokens(wkt)
        assert row.geo_data[geoid_v2.REGIME_KEY] == "v2"
        # the regime label must not have been splatted into the cell list
        assert "v" not in row.s2_cells and "2" not in row.s2_cells
    finally:
        db.close()


def test_unusable_geometry_is_refused_with_422():
    r = client.post("/register-field-boundary",
                    json={"wkt": "POLYGON((0 0, 0 0, 0 0, 0 0))",
                          "threshold": 95, "return_s2_indices": False})
    assert r.status_code in (400, 422), r.status_code


# ==========================================================================
# v1 identifiers must keep resolving
# ==========================================================================

def _alias_v1(v1_geo_id: str, v2_geo_id: str) -> None:
    db = SessionLocal()
    try:
        db.add(GeoIDRegimeAlias(v1_geo_id=v1_geo_id, v2_geo_id=v2_geo_id,
                                v1_kind="l13", relation="same_as"))
        db.commit()
    finally:
        db.close()


def _list_containing(geo_id: str) -> str:
    r = client.post("/list-artifact", json={"members": [geo_id]})
    assert r.status_code == 200, r.text
    return r.json()["list_id"]


def test_a_v1_identifier_reaches_the_field_it_became():
    """AR 1.0 issued these identifiers to real users over several years, and they
    persist in TerraPipe, in exports and in print. A trace seeded with one must
    still find the field. Without the regime alias it returns an empty result,
    which is the worst failure available: confident, and wrong."""
    from app.routers.traceforward import _equivalence_set

    wkt = _fresh_square(150)
    v2_id = _register_direct(wkt, area_ha=2.0)
    v1_id = "urn:agstack:geoid:LEGACY_" + uuid.uuid4().hex[:12]
    _alias_v1(v1_id, v2_id)

    db = SessionLocal()
    try:
        assert v2_id in _equivalence_set(db, v1_id), "v1 seed must reach its v2 field"
        assert v1_id in _equivalence_set(db, v2_id), (
            "and the reverse, or the answer depends on which identifier the "
            "caller happens to hold"
        )
    finally:
        db.close()


def test_the_equivalence_set_is_symmetric():
    from app.routers.traceforward import _equivalence_set

    wkt = _fresh_square(150)
    v2_id = _register_direct(wkt, area_ha=2.0)
    v1_id = "legacy-" + uuid.uuid4().hex[:12]
    _alias_v1(v1_id, v2_id)

    db = SessionLocal()
    try:
        assert _equivalence_set(db, v1_id) == _equivalence_set(db, v2_id)
    finally:
        db.close()


def test_reverse_lookup_finds_a_list_through_a_v1_identifier():
    """End to end: a list registered against the v2 GeoID must be discoverable
    by the v1 identifier a user still holds."""
    wkt = _fresh_square(150)
    v2_id = _register_direct(wkt, area_ha=2.0)
    list_id = _list_containing(v2_id)

    v1_id = "legacy-" + uuid.uuid4().hex[:12]
    _alias_v1(v1_id, v2_id)

    r = client.get(f"/list-artifact/reverse/{v1_id}")
    assert r.status_code == 200, r.text
    assert list_id in r.json()["list_ids"], (
        "the v1 identifier must reach lists registered against its v2 identity"
    )


def test_child_of_is_not_treated_as_identity():
    """A nested plot is contained by its parent, not the same as it. Collapsing
    child_of into the equivalence set would silently widen every trace to the
    parent field, over-reporting rather than under-reporting."""
    from app.routers.traceforward import _equivalence_set

    parent_wkt = _fresh_square(400)
    parent_id = _register_direct(parent_wkt, area_ha=16.0)
    child_v1 = "legacy-child-" + uuid.uuid4().hex[:12]

    db = SessionLocal()
    try:
        db.add(GeoIDRegimeAlias(v1_geo_id=child_v1, v2_geo_id=parent_id,
                                v1_kind="l13", relation="child_of"))
        db.commit()
        assert parent_id not in _equivalence_set(db, child_v1)
    finally:
        db.close()


def test_a_list_registered_against_a_v1_geoid_is_still_traceable():
    """The other direction of the migration window: a list may already reference
    a v1 identifier, and a trace seeded with the v2 GeoID must find it."""
    wkt = _fresh_square(150)
    v2_id = _register_direct(wkt, area_ha=2.0)
    v1_id = "legacy-" + uuid.uuid4().hex[:12]

    list_id = _list_containing(v1_id)
    _alias_v1(v1_id, v2_id)

    db = SessionLocal()
    try:
        rows = db.query(ListMemberEdge).filter(ListMemberEdge.list_id == list_id).all()
        assert [r.geoid for r in rows] == [v1_id]
    finally:
        db.close()

    r = client.get(f"/list-artifact/reverse/{v2_id}")
    assert r.status_code == 200, r.text
    assert list_id in r.json()["list_ids"]
