"""
Legacy source adapters — AR 1.0 (polygons) and TerraPipe (profiles).

WHY TWO SOURCES. AR 1.0 was deliberately identity-free: the registry stored
polygons and issued GeoIDs and never held the link between a user and their
fields. TerraPipe held that link. So the import reads geometry from one system
and ownership from another, and joins them on the v1 GeoID.

WHAT RAJAT IMPLEMENTS. Exactly the two `Ar1TerraPipeSource` methods marked
NOT IMPLEMENTED below, plus `inventory()`. Everything downstream of this
interface is built and tested against `FixtureSource`, so once these return real
rows the whole pipeline runs unchanged.

Keep both connections READ-ONLY. AR 1.0 and TerraPipe are serving real users.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, Protocol

# v1 identifier kinds. Which one a field got depended on registration order,
# which is the defect v2 removes. UUID is the least trustworthy.
KIND_L13 = "l13_hash"
KIND_L20 = "l20_hash"
KIND_UUID = "uuid"
KIND_UNKNOWN = "unknown"


def classify_v1_id(v1_geo_id: str) -> str:
    """Infer the kind from the shape of the identifier.

    64 hex chars is a SHA-256 (L13 or L20 — indistinguishable without the
    source columns, so the adapter should set this explicitly where it can).
    36 chars with hyphens is the random UUID fallback.
    """
    if len(v1_geo_id) == 36 and v1_geo_id.count("-") == 4:
        return KIND_UUID
    if len(v1_geo_id) == 64:
        try:
            int(v1_geo_id, 16)
            return KIND_L13
        except ValueError:
            return KIND_UNKNOWN
    return KIND_UNKNOWN


@dataclass(frozen=True)
class LegacyField:
    """One registered polygon from AR 1.0."""

    v1_geo_id: str
    wkt: str | None                 # None => geometry not retrievable
    area_ha: float | None
    country: str | None = None
    field_name: str | None = None
    crop: str | None = None
    created_at: datetime | None = None
    v1_kind: str = KIND_UNKNOWN
    # AR 1.0's own L13 GeoID, whether or not it became this field's identifier.
    # Fields sharing it are within ~1 km, which makes it a free blocking key for
    # near-duplicate detection (see sample.py) and the basis of the collision
    # count. Populate it from the source column; leave None only if AR 1.0 does
    # not retain it, in which case near-duplicate sampling degrades to nothing.
    v1_l13_geo_id: str | None = None

    @property
    def has_geometry(self) -> bool:
        return bool(self.wkt and self.wkt.strip())

    @property
    def blocking_key(self) -> str:
        return self.v1_l13_geo_id or self.v1_geo_id


@dataclass(frozen=True)
class LegacyProfile:
    """One TerraPipe user and the v1 GeoIDs attached to their profile."""

    source_key: str                 # TerraPipe primary key, for traceability
    email: str | None
    phone: str | None
    first_name: str | None
    last_name: str | None
    v1_geo_ids: list[str] = field(default_factory=list)
    created_at: datetime | None = None


@dataclass
class Inventory:
    """M1 output. Every number exact; no estimates."""

    total_fields: int = 0
    distinct_l13_geo_ids: int = 0            # gap vs total == AR1 collision count
    kind_counts: dict[str, int] = field(default_factory=dict)
    with_geometry: int = 0
    parseable_geometry: int = 0
    zero_or_invalid_area: int = 0
    area_bands: dict[str, int] = field(default_factory=dict)

    total_profiles: int = 0
    profiles_with_fields: int = 0
    max_fields_per_profile: int = 0
    profiles_missing_email: int = 0
    profiles_missing_phone: int = 0
    duplicate_emails: int = 0
    duplicate_phones: int = 0

    orphan_profile_refs: int = 0             # profile -> GeoID absent from AR1
    unclaimed_fields: int = 0                # AR1 polygon no profile claims
    multi_owner_geo_ids: int = 0             # one v1 GeoID, several profiles

    @property
    def ar1_collision_count(self) -> int:
        return self.total_fields - self.distinct_l13_geo_ids


class LegacySource(Protocol):
    def inventory(self) -> Inventory: ...
    def iter_fields(self, limit: int | None = None) -> Iterator[LegacyField]: ...
    def iter_profiles(self, limit: int | None = None) -> Iterator[LegacyProfile]: ...


# --------------------------------------------------------------------------
# the real adapter — Rajat implements
# --------------------------------------------------------------------------

class Ar1TerraPipeSource:
    """Read-only adapter over AR 1.0 and TerraPipe.

    Both connections MUST be read-only; these are live production systems.
    """

    def __init__(self, ar1_dsn: str, terrapipe_dsn: str):
        self.ar1_dsn = ar1_dsn
        self.terrapipe_dsn = terrapipe_dsn

    def inventory(self) -> Inventory:
        """M1. The one query that matters most:

            SELECT COUNT(*), COUNT(DISTINCT <l13_geoid_column>) FROM <fields>;

        The gap between those two numbers is the AR1 collision count. Populate
        every field of Inventory; `ar1_collision_count` falls out of it.

        Also establish, and record in the handoff notes rather than here: what
        did AR 1.0 do when two fields hashed to the same L13 GeoID -- did the
        second registration fail, or silently return the first field's record?
        If the latter, some users hold a GeoID pointing at someone else's field.
        """
        raise NotImplementedError("F1/M1: implement against the AR1 + TerraPipe schemas")

    def iter_fields(self, limit: int | None = None) -> Iterator[LegacyField]:
        """Stream AR 1.0 registrations.

        Set `v1_kind` explicitly from the source columns where possible rather
        than relying on classify_v1_id(), which cannot tell an L13 hash from an
        L20 hash. Yield rows with wkt=None rather than skipping them -- the
        pipeline counts and quarantines them, and that count is a finding.
        """
        raise NotImplementedError("M2: implement against the AR1 schema")

    def iter_profiles(self, limit: int | None = None) -> Iterator[LegacyProfile]:
        """Stream TerraPipe profiles with their attached v1 GeoIDs."""
        raise NotImplementedError("M2: implement against the TerraPipe schema")


# --------------------------------------------------------------------------
# fixtures — so the pipeline is testable before the adapter exists
# --------------------------------------------------------------------------

D = 1 / 111_320.0  # degrees per metre at the equator


def _square_wkt(lat: float, lng: float, side_m: float) -> str:
    s = side_m * D
    pts = [(lng, lat), (lng + s, lat), (lng + s, lat + s), (lng, lat + s), (lng, lat)]
    return "POLYGON ((" + ", ".join(f"{x:.6f} {y:.6f}" for x, y in pts) + "))"


class FixtureSource:
    """Synthetic AR1 + TerraPipe carrying every pathological case we expect.

    The point is not volume, it is that each hazard is present at least once so
    the pipeline is exercised against it before Monday:

      * v1 identifiers of all three kinds, including UUID fallbacks
      * near-identical polygons owned by DIFFERENT users  -> the M6 case
      * exact-duplicate geometry                          -> content_hash clash
      * a small plot nested inside a large field           -> child_of
      * fields with no geometry, and unparseable geometry
      * a polygon with a hole, and a multi-part field
      * profiles referencing GeoIDs absent from AR1        -> orphan join
      * polygons no profile claims                         -> unclaimed
      * users with no phone, no email, and duplicate phones
    """

    # Several hazards are attached to specific filler fields, so a smaller
    # fixture would drop them silently and the suite would pass while testing
    # less. Refuse rather than shrink.
    MIN_FILLER = 41

    def __init__(self, n_filler: int = 60, seed: int = 17):
        if n_filler < self.MIN_FILLER:
            raise ValueError(
                f"n_filler must be >= {self.MIN_FILLER}: hazard profiles are "
                f"attached to filler fields up to index {self.MIN_FILLER - 1}")
        self.rng = random.Random(seed)
        self.n_filler = n_filler
        self._fields: list[LegacyField] = []
        self._profiles: list[LegacyProfile] = []
        self._build()

    # -- construction ----------------------------------------------------

    def _add(self, v1_id, wkt, area, kind, **kw) -> LegacyField:
        f = LegacyField(
            v1_geo_id=v1_id, wkt=wkt, area_ha=area, v1_kind=kind,
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc)
            + timedelta(days=len(self._fields)),
            **kw,
        )
        self._fields.append(f)
        return f

    def _hash_id(self, n: int | None = None) -> str:
        if n is None:
            n, self._n = self._n, self._n + 1
        return f"{n:064x}"

    def _uuid_id(self, n: int | None = None) -> str:
        if n is None:
            n, self._n = self._n, self._n + 1
        h = f"{n:032x}"
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

    def _build(self) -> None:
        self._n = 1

        # --- the M6 case: two users, near-identical polygons ----------------
        # 2000 m squares offset 40 m -> true IoU 0.961, so they merge at 95%.
        #
        # The field has to be this large for the case to be constructible at all.
        # An L20 cell is ~8.1 m across, so on a 200 m field any offset small
        # enough to reach 95% IoU is smaller than a single cell and the two
        # covers come out IDENTICAL -- they then merge at *every* threshold and
        # the knob does nothing. Worth remembering when tuning against real
        # smallholder captures: below roughly 200 m, agreement is quantised by
        # cell size rather than by the threshold.
        self.near_dup_a = self._hash_id()
        self.near_dup_b = self._hash_id()
        # Both carry the SAME AR1 L13 GeoID -- a 40 m offset is far below L13
        # granularity. That shared value is what makes this pair findable in the
        # source without covering anything, and it is why near_dup_b fell through
        # to the L20 identifier in AR 1.0.
        shared_l13 = self._hash_id(900_001)
        self._add(self.near_dup_a, _square_wkt(1.0, 1.0, 2000), 400.0, KIND_L13,
                  country="KEN", v1_l13_geo_id=shared_l13)
        self._add(self.near_dup_b, _square_wkt(1.0, 1.0 + 40 * D, 2000), 400.0, KIND_L20,
                  country="KEN", v1_l13_geo_id=shared_l13)

        # --- exact duplicate geometry: content_hash is UNIQUE in AR2 --------
        self.exact_dup_a = self._hash_id()
        self.exact_dup_b = self._uuid_id()
        dup_wkt = _square_wkt(2.0, 2.0, 150)
        shared_l13_dup = self._hash_id(900_002)
        self._add(self.exact_dup_a, dup_wkt, 2.25, KIND_L13, country="KEN",
                  v1_l13_geo_id=shared_l13_dup)
        self._add(self.exact_dup_b, dup_wkt, 2.25, KIND_UUID, country="KEN",
                  v1_l13_geo_id=shared_l13_dup)

        # --- nesting: small plot inside a large field -> child_of ------------
        self.big_field = self._hash_id()
        self.small_plot = self._hash_id()
        shared_l13_nest = self._hash_id(900_003)
        self._add(self.big_field, _square_wkt(3.0, 3.0, 500), 25.0, KIND_L13,
                  country="IND", v1_l13_geo_id=shared_l13_nest)
        self._add(self.small_plot, _square_wkt(3.0005, 3.0005, 80), 0.64, KIND_L13,
                  country="IND", v1_l13_geo_id=shared_l13_nest)

        # --- geometry problems ----------------------------------------------
        self.no_geom = self._hash_id()
        self._add(self.no_geom, None, None, KIND_L13, country="KEN")

        self.bad_geom = self._hash_id()
        self._add(self.bad_geom, "POLYGON ((0 0, 0 0, 0 0, 0 0))", 0.0, KIND_L13, country="KEN")

        self.unparseable = self._hash_id()
        self._add(self.unparseable, "NOT WKT AT ALL", None, KIND_UNKNOWN, country="KEN")

        # --- shapes the first implementation got wrong -----------------------
        self.holed = self._hash_id()
        self._add(
            self.holed,
            "POLYGON ((0 10, 0.01 10, 0.01 10.01, 0 10.01, 0 10), "
            "(0.003 10.003, 0.007 10.003, 0.007 10.007, 0.003 10.007, 0.003 10.003))",
            8.4, KIND_L13, country="BRA",
        )
        self.multipart = self._hash_id()
        self._add(
            self.multipart,
            "MULTIPOLYGON (((0 20, 0.002 20, 0.002 20.002, 0 20.002, 0 20)), "
            "((0.01 20, 0.012 20, 0.012 20.002, 0.01 20.002, 0.01 20)))",
            9.8, KIND_L13, country="BRA",
        )

        # --- a UUID-fallback field that v2 will give a real identity ---------
        self.uuid_field = self._uuid_id()
        self._add(self.uuid_field, _square_wkt(4.0, 4.0, 120), 1.44, KIND_UUID, country="KEN")

        # --- unclaimed polygon: no profile references it ---------------------
        self.unclaimed = self._hash_id()
        self._add(self.unclaimed, _square_wkt(5.0, 5.0, 300), 9.0, KIND_L13, country="KEN")

        # --- filler across area bands ---------------------------------------
        self.filler: list[str] = []
        for i in range(self.n_filler):
            gid = self._hash_id()
            side = self.rng.choice([40, 90, 150, 400, 900, 1800])
            lat = 10.0 + i * 0.05
            self._add(gid, _square_wkt(lat, 30.0, side), (side * side) / 10_000.0,
                      KIND_L13, country=self.rng.choice(["KEN", "IND", "BRA"]))
            self.filler.append(gid)

        # ---------------- profiles ------------------------------------------
        # the M6 pair, deliberately split across two different users
        self._profiles.append(LegacyProfile("tp-1", "amina@example.com", "+254700000001",
                                            "Amina", "Wanjiru", [self.near_dup_a]))
        self._profiles.append(LegacyProfile("tp-2", "joseph@example.com", "+254700000002",
                                            "Joseph", "Kimani", [self.near_dup_b]))

        # same physical field registered twice by the same person, two accounts
        self._profiles.append(LegacyProfile("tp-3", "ravi@example.com", "+919000000003",
                                            "Ravi", "Kumar", [self.exact_dup_a]))
        self._profiles.append(LegacyProfile("tp-4", "ravi.alt@example.com", "+919000000004",
                                            "Ravi", "Kumar", [self.exact_dup_b]))

        # co-op holding many fields, including the nesting pair
        self._profiles.append(LegacyProfile(
            "tp-5", "coop@example.com", "+919000000005", "Green", "Coop",
            [self.big_field, self.small_plot, self.uuid_field] + self.filler[:12]))

        # profile pointing at a GeoID that does not exist in AR1 -> orphan
        self._profiles.append(LegacyProfile("tp-6", "ghost@example.com", "+254700000006",
                                            "Grace", "Otieno",
                                            [self._hash_id(999_001), self.filler[12]]))

        # profile whose ONLY field has no geometry
        self._profiles.append(LegacyProfile("tp-7", "nogeom@example.com", "+254700000007",
                                            "Peter", "Mwangi", [self.no_geom]))

        # hub constraint hazards: missing phone, missing email, duplicate phone
        self._profiles.append(LegacyProfile("tp-8", "nophone@example.com", None,
                                            "Sara", "Ali", [self.filler[13]]))
        self._profiles.append(LegacyProfile("tp-9", None, "+254700000009",
                                            "NoEmail", "User", [self.filler[14]]))
        self._profiles.append(LegacyProfile("tp-10", "dup1@example.com", "+254700000002",
                                            "Dup", "Phone", [self.filler[15]]))
        # no name at all -- hub requires first_name and last_name NOT NULL
        self._profiles.append(LegacyProfile("tp-11", "noname@example.com", "+254700000011",
                                            None, None, [self.filler[16]]))

        for i, gid in enumerate(self.filler[17:40]):
            self._profiles.append(LegacyProfile(
                f"tp-{100+i}", f"user{i}@example.com", f"+2547010{i:05d}",
                f"User{i}", "Test", [gid]))

    # -- LegacySource ----------------------------------------------------

    def iter_fields(self, limit: int | None = None) -> Iterator[LegacyField]:
        for i, f in enumerate(self._fields):
            if limit is not None and i >= limit:
                return
            yield f

    def iter_profiles(self, limit: int | None = None) -> Iterator[LegacyProfile]:
        for i, p in enumerate(self._profiles):
            if limit is not None and i >= limit:
                return
            yield p

    def inventory(self) -> Inventory:
        from shapely.wkt import loads as load_wkt

        inv = Inventory()
        known = {f.v1_geo_id for f in self._fields}
        inv.total_fields = len(self._fields)
        inv.distinct_l13_geo_ids = len({f.blocking_key for f in self._fields})

        for f in self._fields:
            inv.kind_counts[f.v1_kind] = inv.kind_counts.get(f.v1_kind, 0) + 1
            if f.has_geometry:
                inv.with_geometry += 1
                try:
                    geom = load_wkt(f.wkt)
                    inv.parseable_geometry += 1
                    if geom.is_empty or geom.area <= 0:
                        inv.zero_or_invalid_area += 1
                except Exception:
                    pass
            band = _area_band(f.area_ha)
            inv.area_bands[band] = inv.area_bands.get(band, 0) + 1

        claimed: dict[str, set[str]] = {}
        inv.total_profiles = len(self._profiles)
        emails: dict[str, int] = {}
        phones: dict[str, int] = {}
        for p in self._profiles:
            if p.v1_geo_ids:
                inv.profiles_with_fields += 1
            inv.max_fields_per_profile = max(inv.max_fields_per_profile, len(p.v1_geo_ids))
            if not p.email:
                inv.profiles_missing_email += 1
            else:
                emails[p.email] = emails.get(p.email, 0) + 1
            if not p.phone:
                inv.profiles_missing_phone += 1
            else:
                phones[p.phone] = phones.get(p.phone, 0) + 1
            for gid in p.v1_geo_ids:
                if gid not in known:
                    inv.orphan_profile_refs += 1
                claimed.setdefault(gid, set()).add(p.source_key)

        inv.duplicate_emails = sum(1 for c in emails.values() if c > 1)
        inv.duplicate_phones = sum(1 for c in phones.values() if c > 1)
        inv.unclaimed_fields = len(known - set(claimed))
        inv.multi_owner_geo_ids = sum(1 for owners in claimed.values() if len(owners) > 1)
        return inv


def _area_band(area_ha: float | None) -> str:
    if area_ha is None:
        return "unknown"
    if area_ha <= 0:
        return "zero"
    if area_ha < 1:
        return "<1"
    if area_ha < 5:
        return "1-5"
    if area_ha < 50:
        return "5-50"
    if area_ha < 500:
        return "50-500"
    return ">500"
