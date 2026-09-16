"""GeoID v2: content-derived field identity, and area-exact comparison of covers.

The regime, per doc/current/dpi_architecture_20260812.md S10:

  1. Canonicalize the geometry.
  2. Cover the *polygon* -- not its bounding box -- down to a fixed fine level.
  3. Normalize the cell union to its canonical compact form.
  4. Sort the tokens, SHA-256 -> one GeoID.

This module is the single implementation for the live registration path. It
replaces two things.

WHAT IT REPLACES: THE IDENTIFIER CASCADE. Registration used to hash the L13
cover, and on collision fall back to hashing L20, and on a second collision fall
back to `uuid4()`. An L13 cell is roughly 1.2 km across, so two genuinely
different fields inside one cell collided routinely -- and the fallback resolved
that by minting an identifier derived from nothing at all. A "content-derived"
ID that silently stops being content-derived is worse than an opaque one,
because downstream code cannot tell which it holds. Under v2 a collision means
the canonicalized geometry is identical, which is a `same_as`, not a new ID.

WHAT IT REPLACES: TOKEN-SET OVERLAP. `Utils.check_percentage_match` compared
covers by set intersection over tokens. That has two independent defects once
covers are normalized and therefore multi-level:

  1. Token equality cannot see ancestor/descendant overlap. One L16 cell and its
     own four L17 children cover the identical region and share no token, so set
     intersection scores them as zero overlap. This is the dominant error, and
     leaf-weighting alone does not repair it.
  2. Cell counts stop being area once levels are mixed.

Measured on two 200 m squares offset 10 m, true IoU 0.9048:

    set cardinality (the old arithmetic)        0.3171
    set intersection + leaf weighting           0.2975   <- weighting alone: no help
    cell-union intersection + leaf weighting    0.8889   <- correct

On uniform-level v1 covers the corrected form is bit-identical to the old
arithmetic, so adopting it cannot change any v1 resolution decision.

CORRECTNESS NOTES CARRIED OVER FROM THE FIRST v2 DRAFT:

  * Holes were discarded. Only the exterior ring reached S2Loop, so a polygon
    with a hole covered its hole and hashed identically to the solid shape.
    Interior rings are now passed to InitNested as additional loops.
  * MultiPolygons kept only the first part. Every other part was dropped
    silently, so a multi-part field took the identity of one fragment. This
    matters on real data because make_valid() *produces* MultiPolygons and
    GeometryCollections from self-intersecting input.

These bindings do not expose S2CellUnion::LeafCellsCovered(), so leaf-cell area
is computed as 4**(30 - level). That is exact integer arithmetic, not an estimate.

Kept byte-for-byte consistent with migration/geoid_v2.py; the two are pinned
together by app/tests/test_geoid_v2_agreement.py.
"""

from __future__ import annotations

import hashlib

import s2geometry as s2g
from shapely import ops
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient
from shapely.validation import make_valid
from shapely.wkt import loads as load_wkt

COORD_PRECISION = 6      # ~11 cm at the equator
MAX_LEVEL = 20           # ~65.6 m^2 per cell, ~8.1 m edge
MIN_LEVEL = 1
MAX_CELLS = 1_000_000    # effectively unbounded: never truncate, never approximate

LEAF_LEVEL = 30
BLOCKING_LEVEL = 13
REGIME_VERSION = "v2"

# Keys under which the canonical cover and the regime label are stored in
# GeoID.geo_data. Deliberately not numeric: geo_data's integer-like keys mean
# "the fixed-level cover at that level", and the v2 cover is multi-level, so
# filing it under "20" would be a lie that the comparison code would believe.
COVER_KEY = "v2_cover"
REGIME_KEY = "regime"

KIND_KEY = "geometry_kind"
"""Whether the plot was registered as a boundary or as a coordinate.

Derivable from the cover -- a point's is one cell at the leaf level, a
polygon's is not -- and recorded anyway, because every consumer that has to
treat the two differently would otherwise re-derive it, and one of them would
get it wrong. ``kind_of`` is the single reader, and it falls back to the
derivation for rows written before this key existed."""

DECLARED_AREA_KEY = "declared_area_ha"
"""The area a point stands for, as declared by the registrant.

On the wire at L1 as well as in the masked view, because the node that screens
the plot needs it: without it a point is one 36 m sample being called a farm.
Meaningless for a polygon, whose area is computed from its boundary."""

KIND_POINT = "point"
KIND_POLYGON = "polygon"


class GeometryUnusable(ValueError):
    """The geometry cannot yield a content-derived identity.

    Raised for empty, zero-area or non-polygonal input. Callers decide policy --
    quarantine, flag, reject. This module refuses to invent an identifier, which
    is the whole point of retiring the UUID fallback.
    """


# --------------------------------------------------------------------------
# canonicalization
# --------------------------------------------------------------------------

def canonicalize(wkt_string: str) -> Polygon | MultiPolygon:
    """Repair, flatten to 2D, round coordinates, and orient consistently.

    Rounding before covering is what makes the identity stable under trivial
    coordinate noise. It does not make it stable under GPS jitter: a re-survey
    resolves through IoU, not through the hash.
    """
    geom = load_wkt(wkt_string)
    geom = make_valid(geom)

    # force_2d: drop any Z/M and round in one pass
    geom = ops.transform(
        lambda x, y, *_: (round(x, COORD_PRECISION), round(y, COORD_PRECISION)), geom
    )

    parts = _polygonal_parts(geom)
    if not parts:
        raise GeometryUnusable(f"no polygonal component in {geom.geom_type}")

    parts = [p for p in parts if not p.is_empty and p.area > 0]
    if not parts:
        raise GeometryUnusable("all polygonal components are empty or zero-area")

    # S2 decides shell vs hole by nesting depth, so all loops go in with the
    # same winding.
    parts = [orient(p, sign=1.0) for p in parts]

    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def _polygonal_parts(geom) -> list[Polygon]:
    """Flatten any geometry to its polygonal parts, recursing into collections."""
    t = geom.geom_type
    if t == "Polygon":
        return [geom]
    if t == "MultiPolygon":
        return list(geom.geoms)
    if t in ("GeometryCollection", "MultiLineString", "MultiPoint"):
        out: list[Polygon] = []
        for g in getattr(geom, "geoms", []):
            out.extend(_polygonal_parts(g))
        return out
    return []


# --------------------------------------------------------------------------
# covering
# --------------------------------------------------------------------------

def _s2_loop(ring_coords) -> s2g.S2Loop:
    pts = [
        s2g.S2LatLng.FromDegrees(lat, lng).ToPoint()
        for lng, lat in list(ring_coords)[:-1]  # drop the repeated closing vertex
    ]
    loop = s2g.S2Loop(pts)
    loop.Normalize()
    return loop


def _s2_polygon(geom) -> s2g.S2Polygon:
    """Build an S2Polygon preserving holes and all parts."""
    parts = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
    loops: list[s2g.S2Loop] = []
    for part in parts:
        loops.append(_s2_loop(part.exterior.coords))
        for interior in part.interiors:
            loops.append(_s2_loop(interior.coords))

    poly = s2g.S2Polygon()
    poly.InitNested(loops)
    return poly


def cover_tokens(wkt_string: str) -> list[str]:
    """Canonical, compact, sorted S2 token cover of the polygon."""
    geom = canonicalize(wkt_string)
    poly = _s2_polygon(geom)

    coverer = s2g.S2RegionCoverer()
    coverer.set_min_level(MIN_LEVEL)
    coverer.set_max_level(MAX_LEVEL)
    coverer.set_max_cells(MAX_CELLS)
    covering = coverer.GetCovering(poly)

    union = s2g.S2CellUnion()
    union.Init([cid.id() for cid in covering])   # Init sorts and compacts

    if not union.cell_ids():
        raise GeometryUnusable("cover is empty")

    return sorted(cid.ToToken() for cid in union.cell_ids())


def digest(tokens: list[str]) -> str:
    """SHA-256 over the concatenated sorted cover tokens.

    The one place a cover becomes a name. Points and polygons both go through
    here, so a one-token cover is named by the same arithmetic as a 4,000-token
    one and nothing has to be kept in step by hand.
    """
    h = hashlib.sha256()
    for t in tokens:
        h.update(t.encode())
    return h.hexdigest()


def geo_id_with_tokens(wkt_string: str) -> tuple[list[str], str]:
    tokens = cover_tokens(wkt_string)
    return tokens, digest(tokens)


def geo_id(wkt_string: str) -> str:
    """The GeoID: SHA-256 over the concatenated sorted cover tokens."""
    return geo_id_with_tokens(wkt_string)[1]


def geo_id_short(full_geo_id: str) -> str:
    """16-char short form, matching the GeoID.geo_id_short column."""
    return hashlib.sha256(full_geo_id.encode("utf-8")).hexdigest()[:16]


def content_hash(wkt_string: str) -> str:
    """SHA-256 of canonical WKB, matching the unique GeoID.content_hash column."""
    from shapely import wkb
    geom = canonicalize(wkt_string)
    return hashlib.sha256(wkb.dumps(geom, output_dimension=2)).hexdigest()


# --------------------------------------------------------------------------
# points
# --------------------------------------------------------------------------

POINT_LEVEL = LEAF_LEVEL
"""The cell a point is named by: its leaf cell, about 1 cm, the finest S2 has.

A point keeps the precision it was surveyed at, exactly as a polygon keeps its
vertices. Convergence is not the name's job here; it is the resolver's, below.

This was level 20 (~8 m) for part of 2026-09-15, on the reasoning that a fix
cannot reliably leave an 8 m cell so quantizing to it would make a re-survey
converge. Measured over 20,000 random fixes in the Honduran coffee belt, that
held 86.3% of the time at 1 m of jitter and 60.5% at 3 m: a fix near a cell
boundary crosses it, and the grid has no way to know it is near one. The same
grid called two genuinely different plots 5 m apart one plot 40.0% of the time.
Both errors are the grid's arbitrariness, not the data's, and a finer or coarser
grid only moves them. A distance threshold has neither failure: it is the same
answer wherever the plot happens to sit."""

POINT_BLOCKING_LEVEL = 20
"""Candidate pre-filter for point resolution, the analogue of BLOCKING_LEVEL for
polygons: a fix's L20 cell *and its eight neighbours*, so a candidate on the far
side of a cell boundary is still considered. Blocking that ignored the
neighbours would reintroduce the cliff it is there to remove."""

POINT_SAME_AS_METRES = 10.0
"""Two fixes this far apart or closer are the same plot re-registered.

Ten metres is inside the error of a handheld GNSS fix under coffee shade and
well below the span of a plot a point is allowed to stand for (4 ha is about
200 m across). It is a policy number, not a fact: it is exposed here so an
operator can move it, and the verdict records which value was applied."""

EARTH_RADIUS_M = 6371010.0
"""Mean Earth radius, the value S2 uses for its own kilometre conversions."""

POINT_MAX_AREA_HA = 4.0
"""Regulation (EU) 2023/1115 Article 2(28): a plot of land of at most four
hectares may be described by a single point. Above that the regulation wants a
polygon, and so does this registry."""


def point_geo_id_with_tokens(lat: float, lng: float) -> tuple[list[str], str]:
    """Identity for a point registration: a one-cell cover of its leaf cell.

    The same shape as a polygon's identity -- a sorted list of cover tokens run
    through digest() -- with a list of one. That is what lets a List hold points
    and fields together without knowing which is which, and what lets the
    ancestor probe relate a point to the field it sits in: its leaf cell is a
    descendant of a cell in that field's cover.

    Two fixes of the same tree get two names here, because they are two
    different coordinates. They are brought together by resolution at
    registration, the way two redraws of one field are: see point_same_as.
    """
    # The S2CellId constructor takes an S2LatLng and yields a leaf cell directly;
    # these bindings expose no FromLatLng classmethod.
    leaf = s2g.S2CellId(s2g.S2LatLng.FromDegrees(lat, lng))
    tokens = [leaf.parent(POINT_LEVEL).ToToken()] if POINT_LEVEL != LEAF_LEVEL else [leaf.ToToken()]
    return tokens, digest(tokens)


def is_point_cover(tokens: list[str] | None) -> bool:
    """Whether a stored cover is a point's: exactly one cell, at POINT_LEVEL."""
    if not tokens or len(tokens) != 1:
        return False
    return s2g.S2CellId.FromToken(tokens[0]).level() == POINT_LEVEL


def kind_of(geo_data: dict | None) -> str:
    """Whether a stored registration is a point or a polygon.

    Reads the recorded kind, and derives it from the cover for rows written
    before the key existed. One reader, so a consumer cannot disagree with the
    registry about what it is holding.
    """
    geo_data = geo_data or {}
    kind = geo_data.get(KIND_KEY)
    if kind in (KIND_POINT, KIND_POLYGON):
        return kind
    return KIND_POINT if is_point_cover(geo_data.get(COVER_KEY)) else KIND_POLYGON


def point_of_cover(tokens: list[str]) -> tuple[float, float]:
    """The (lat, lng) a point cover stands for: the centre of its one cell.

    At leaf level that is the fix itself to within about a centimetre, so a
    candidate's coordinates can be recovered from its cover without reading a
    geometry the caller may not be entitled to see.
    """
    ll = s2g.S2CellId.FromToken(tokens[0]).ToLatLng()
    return ll.lat().degrees(), ll.lng().degrees()


def metres_between(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres, on S2's sphere."""
    a = s2g.S2LatLng.FromDegrees(lat1, lng1)
    b = s2g.S2LatLng.FromDegrees(lat2, lng2)
    return a.GetDistance(b).radians() * EARTH_RADIUS_M


def point_same_as(
    lat1: float, lng1: float, lat2: float, lng2: float, threshold_m: float = POINT_SAME_AS_METRES
) -> bool:
    """Whether two fixes name the same plot.

    The point regime's counterpart to IoU over covers. Unlike a grid, this gives
    the same answer wherever the plot sits: there is no boundary to be near.
    """
    return metres_between(lat1, lng1, lat2, lng2) <= threshold_m


def point_blocking_key(lat: float, lng: float) -> list[str]:
    """Candidate pre-filter tokens for a fix: its L20 cell and the eight around it.

    Returned for the s2_cells overlap query, which is how the polygon path finds
    candidates too. The neighbours are the point that matters: a fix a metre
    inside one cell has its re-survey a metre inside the next one, and blocking
    on the single cell would never show them to each other.
    """
    cell = s2g.S2CellId(s2g.S2LatLng.FromDegrees(lat, lng)).parent(POINT_BLOCKING_LEVEL)
    return sorted({cell.ToToken(), *(n.ToToken() for n in cell.GetAllNeighbors(POINT_BLOCKING_LEVEL))})


def point_area_declared(area_ha: float | None) -> float:
    """Validate the area a point registration declares for the plot it stands for.

    A point carries no area of its own, so the registrant declares one. Zero or
    None is accepted (the AR1 behaviour: an area not stated), but a declaration
    above the regulation's four hectares is refused, because that plot needs a
    polygon and a point for it would be a claim this registry cannot check.
    """
    if area_ha is None:
        return 0.0
    if area_ha < 0:
        raise GeometryUnusable("declared area cannot be negative")
    if area_ha > POINT_MAX_AREA_HA:
        raise GeometryUnusable(
            f"a point may stand for a plot of at most {POINT_MAX_AREA_HA:g} ha "
            f"(Regulation (EU) 2023/1115 Art. 2(28)); {area_ha:g} ha needs a polygon"
        )
    return float(area_ha)


# --------------------------------------------------------------------------
# area, in exact leaf-cell units
# --------------------------------------------------------------------------

def token_level(token: str) -> int:
    cid = int(token.ljust(16, "0"), 16)
    lsb = cid & (-cid)
    return LEAF_LEVEL - ((lsb.bit_length() - 1) // 2)


def leaf_cells_from_tokens(tokens) -> int:
    """Exact area of a token cover in leaf-cell units.

    A cell at level L contains exactly 4**(30-L) leaf cells, so this is exact
    integer arithmetic across mixed levels with no equal-area assumption.
    """
    total = 0
    for token in tokens:
        cid = int(token.ljust(16, "0"), 16)
        if cid == 0:
            continue
        lsb = cid & (-cid)
        level = LEAF_LEVEL - ((lsb.bit_length() - 1) // 2)
        total += 4 ** (LEAF_LEVEL - level)
    return total


def _ancestor_token(token: str, level: int) -> str:
    """The token of a cell's ancestor at the given level."""
    cid = int(token.ljust(16, "0"), 16)
    lsb = 1 << (2 * (LEAF_LEVEL - level))
    return format((cid & ~(lsb - 1)) | lsb, "016x").rstrip("0")


def blocking_key(tokens) -> list[str]:
    """L13 ancestors of a cover, used as the candidate-lookup blocking key.

    Identity is the fine cover; this is only an index. Retained because it is a
    cheap, effective pre-filter and is what the existing s2_cells index already
    supports.
    """
    keys = set()
    for token in tokens:
        if token_level(token) <= BLOCKING_LEVEL:
            keys.add(token)
        else:
            keys.add(_ancestor_token(token, BLOCKING_LEVEL))
    return sorted(keys)


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def _union_from_tokens(tokens) -> s2g.S2CellUnion:
    cu = s2g.S2CellUnion()
    cu.Init([s2g.S2CellId.FromToken(t).id() for t in tokens])   # Init normalizes
    return cu


def _tokens_of(cu: s2g.S2CellUnion) -> list[str]:
    return [cid.ToToken() for cid in cu.cell_ids()]


def iou_and_containment(tokens_a, tokens_b) -> tuple[float, float]:
    """Area-exact (IoU, containment) between two covers.

    containment is intersection over the smaller region -- the nesting test
    behind child_of. Empty input yields (0.0, 0.0) rather than raising, because
    a cover we failed to store is a data problem, not a comparison error.
    """
    if not tokens_a or not tokens_b:
        return 0.0, 0.0

    a = _union_from_tokens(tokens_a)
    b = _union_from_tokens(tokens_b)

    # Cell-union intersection subdivides as needed, so ancestor/descendant
    # overlap is counted. Plain token-set intersection cannot see it.
    intersection = a.Intersection(b)

    union = _union_from_tokens(set(_tokens_of(a)) | set(_tokens_of(b)))

    inter_leaves = leaf_cells_from_tokens(_tokens_of(intersection))
    union_leaves = leaf_cells_from_tokens(_tokens_of(union))
    if union_leaves == 0:
        return 0.0, 0.0

    smaller = min(
        leaf_cells_from_tokens(_tokens_of(a)),
        leaf_cells_from_tokens(_tokens_of(b)),
    )

    iou = inter_leaves / float(union_leaves)
    containment = inter_leaves / float(smaller) if smaller else 0.0

    if not (0.0 <= iou <= 1.0 and 0.0 <= containment <= 1.0):
        raise AssertionError(
            f"cover comparison out of range: iou={iou}, containment={containment}"
        )
    return iou, containment
