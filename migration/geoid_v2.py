# SPDX-License-Identifier: EUPL-1.2
# Copyright (c) 2026 AgStack project contributors.
# Licensed under the EUPL, Version 1.2; see the LICENSE file for the full text.

"""
GeoID v2 — content-derived field identity.

The regime (see doc/current/dpi_architecture_20260812.md S10):

  1. Canonicalize the geometry.
  2. Cover the *polygon* -- not its bounding box -- down to a fixed fine level.
  3. S2CellUnion::Normalize() to get the canonical compact union.
  4. Sort the tokens, SHA-256 -> one GeoID.

WHAT THIS FIXES relative to the first implementation on the `rajat` branch:

  * HOLES WERE DISCARDED. Only `geom.exterior.coords` reached S2Loop, so a
    polygon with a hole covered its hole. Interior rings are now passed to
    InitNested as additional loops; S2 resolves shell-vs-hole by nesting depth.
    Verified against GetArea(): a square with a hole occupying 16% of its area
    returns exactly 84% of the solid square's area.

  * MULTIPOLYGONS KEPT ONLY geoms[0]. Every other part was dropped silently, so
    a multi-part field received the identity of its first fragment. All parts now
    contribute. This matters on real data because make_valid() *produces*
    MultiPolygons and GeometryCollections from self-intersecting input.

Requires s2geometry>=0.14.0 (prebuilt abi3 wheels, Python 3.10+) and shapely 2.x.

NOTE: these bindings do NOT expose S2CellUnion::LeafCellsCovered(), so leaf-cell
area is computed arithmetically as 4**(30-level). That is exact, not an estimate.
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
_EARTH_RADIUS_M = 6_371_010.0   # IUGG mean radius, as used by S2Earth
REGIME_VERSION = "v2"


class GeometryUnusable(ValueError):
    """The geometry cannot yield a content-derived identity.

    Raised for empty, zero-area or non-polygonal input. Callers must decide
    policy (quarantine / flag / reject); this module refuses to invent an ID.
    """


# --------------------------------------------------------------------------
# canonicalization
# --------------------------------------------------------------------------

def canonicalize(wkt_string: str) -> Polygon | MultiPolygon:
    """Repair, flatten to 2D, round coordinates, and orient consistently.

    Rounding before covering is what makes the identity stable under trivial
    coordinate noise. It does not make it stable under GPS jitter -- see the
    note in the architecture doc; re-survey resolves through IoU, not the hash.
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

    # Orient every ring consistently. S2 decides shell vs hole by nesting depth,
    # so all loops go in with the same winding.
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
    """Build an S2Polygon preserving holes and all parts.

    Every ring of every part becomes a loop. InitNested resolves shells and
    holes from the nesting, so disjoint parts and interior rings are both
    handled by the same call.
    """
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


def geo_id(wkt_string: str) -> str:
    """The GeoID: SHA-256 over the concatenated sorted cover tokens."""
    return geo_id_with_tokens(wkt_string)[1]


def geo_id_with_tokens(wkt_string: str) -> tuple[list[str], str]:
    tokens = cover_tokens(wkt_string)
    h = hashlib.sha256()
    for t in tokens:
        h.update(t.encode())
    return tokens, h.hexdigest()


def geo_id_short(full_geo_id: str) -> str:
    """16-char short form. Matches the existing GeoID.geo_id_short column."""
    return hashlib.sha256(full_geo_id.encode("utf-8")).hexdigest()[:16]


def content_hash(wkt_string: str) -> str:
    """SHA-256 of canonical WKB. Matches GeoID.content_hash (unique)."""
    from shapely import wkb
    geom = canonicalize(wkt_string)
    return hashlib.sha256(wkb.dumps(geom, output_dimension=2)).hexdigest()


# --------------------------------------------------------------------------
# points
# --------------------------------------------------------------------------

def point_geo_id_with_tokens(lat: float, lng: float) -> tuple[list[str], str]:
    """Identity for a point: the single leaf cell containing it.

    Byte-for-byte the same rule as app.geoid_v2.point_geo_id_with_tokens, and
    test_the_two_implementations_agree_on_points fails if that stops being true.
    A point registered through AR2's API and the same point arriving through the
    legacy import have to land on one identifier, or the import manufactures a
    second identity for a place that already has one.

    Why this exists in the importer at all: AR 1.0 accepted point registrations,
    and a great many of its rows are points rather than boundaries. The polygon
    coverer cannot describe a point -- it has no area -- so without this path the
    importer rejects every one of them as unusable geometry. That is not a data
    quality problem to write a policy about, it is a missing code path.
    """
    cell = s2g.S2CellId(s2g.S2LatLng.FromDegrees(lat, lng))
    tokens = [cell.ToToken()]
    return tokens, hashlib.sha256(tokens[0].encode()).hexdigest()


def point_content_hash(lat: float, lng: float) -> str:
    """Content hash for a point, from its canonical WKB.

    Rounded to COORD_PRECISION first, exactly as canonicalize() rounds a polygon,
    so two submissions of the same pin within ~11 cm are one row rather than two.
    """
    from shapely import wkb
    from shapely.geometry import Point
    point = Point(round(lng, COORD_PRECISION), round(lat, COORD_PRECISION))
    return hashlib.sha256(wkb.dumps(point, output_dimension=2)).hexdigest()


def point_coords(wkt_string: str) -> tuple[float, float] | None:
    """(lat, lng) if this WKT denotes a single position, else None.

    Accepts a bare POINT, and also a polygon or line whose vertices are all the
    same position -- AR 1.0 stored pins both ways, and a collapsed ring is a pin
    written as a boundary rather than a broken boundary.
    """
    try:
        geom = load_wkt(wkt_string)
    except Exception:  # noqa: BLE001
        return None
    if geom.is_empty:
        return None

    # Dispatch on the type rather than probing for .coords: shapely defines the
    # attribute on Polygon and raises when it is read, so getattr finds it and
    # then blows up.
    if geom.geom_type == "Polygon":
        coords = list(geom.exterior.coords)
    elif geom.geom_type in ("Point", "LineString", "LinearRing"):
        coords = list(geom.coords)
    else:
        return None

    if not coords:
        return None

    unique = {(round(c[0], COORD_PRECISION), round(c[1], COORD_PRECISION))
              for c in coords}
    if len(unique) != 1:
        return None

    lng, lat = next(iter(unique))
    return lat, lng


def area_ha(wkt_string: str) -> float | None:
    """Geodesic area in hectares, or None if the geometry has no area.

    Computed from the geometry rather than read from a legacy column. AR 1.0's
    stored areas are of unknown provenance and cannot be checked from here,
    whereas this is derived from the same boundary that produces the GeoID and
    is therefore consistent with it by construction. Points return None: a pin
    has no area, and reporting 0 would put pins in the same bucket as collapsed
    boundaries, which are a different problem.

    S2Polygon.GetArea returns steradians on the unit sphere; scaling by the
    Earth's mean radius squared gives m2, and 1 ha is 10,000 m2. Exact on the
    sphere, so no projection is chosen and none is wrong.
    """
    try:
        geom = canonicalize(wkt_string)
        return _s2_polygon(geom).GetArea() * _EARTH_RADIUS_M ** 2 / 10_000.0
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# area, in exact leaf-cell units
# --------------------------------------------------------------------------

def leaf_cells_from_tokens(tokens) -> int:
    """Exact area of a token cover in leaf-cell units.

    A cell at level L contains exactly 4**(30-L) leaf cells, so this is exact
    integer arithmetic across mixed levels with no equal-area assumption.
    Level is recovered from the token without constructing an S2 object.
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


def token_level(token: str) -> int:
    cid = int(token.ljust(16, "0"), 16)
    lsb = cid & (-cid)
    return LEAF_LEVEL - ((lsb.bit_length() - 1) // 2)


BLOCKING_LEVEL = 13


def _ancestor_token(token: str, level: int) -> str:
    """The token of a cell's ancestor at the given level."""
    cid = int(token.ljust(16, "0"), 16)
    lsb = 1 << (2 * (LEAF_LEVEL - level))
    return format((cid & ~(lsb - 1)) | lsb, "016x").rstrip("0")


def blocking_key(tokens) -> list[str]:
    """L13 ancestors of a cover, used as the candidate-lookup blocking key.

    Identity is the fine cover; this is only an index. Retained because it is a
    cheap, effective pre-filter and is what the existing s2_cells GIN index
    already supports.
    """
    keys = set()
    for token in tokens:
        if token_level(token) <= BLOCKING_LEVEL:
            keys.add(token)
        else:
            keys.add(_ancestor_token(token, BLOCKING_LEVEL))
    return sorted(keys)
