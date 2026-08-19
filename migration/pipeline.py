"""
The import: AR 1.0 polygons -> AR2, TerraPipe profiles -> Hub + Pancake.

Two phases, and the order is load-bearing:

  Phase 1  import_fields()    geometry -> AR2, v2 GeoIDs at ingest, alias written
  Phase 2  import_profiles()  ownership -> Hub + Pancake, joined via the alias

Phase 2 REFUSES to run against an unimported registry. A ListID is a Merkle root
over sorted GeoIDs, so running them out of order produces ListIDs that are wrong
but structurally valid -- the hardest class of defect to detect later.

Both phases are idempotent and resumable: they key off what is already in the
target, so a second run does nothing and a killed run resumes correctly. Because
the target starts empty, a full reset is also always available; resumability is
here to make repeated dry runs cheap, not to rescue a live registry.

DRY RUN IS THE SAME CODE PATH with writes suppressed. A separate dry-run script
tests code you are not going to run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from . import geoid_v2 as g2
from .repo import (
    AliasRow,
    ConstraintViolation,
    FieldListRow,
    GeoIdRow,
    HubAccount,
    InMemoryRepo,
)
from .resolve import CHILD_OF, SAME_AS, resolve
from .sources import LegacyProfile, LegacySource

DEFAULT_THRESHOLD_PCT = 95.0


# --------------------------------------------------------------------------
# outcomes
# --------------------------------------------------------------------------

QUARANTINE_NO_GEOMETRY = "no_geometry"
QUARANTINE_UNUSABLE = "unusable_geometry"
QUARANTINE_DUPLICATE_CONTENT = "duplicate_content_hash"
QUARANTINE_CONSTRAINT = "constraint_violation"


def _reason_of(exc: Exception) -> str:
    """A short, groupable reason for a rejected geometry.

    Quarantine counts are only useful if they can be acted on, and "14,594
    unusable" supports no action at all. Broken down, the same number becomes a
    work list: one reason may be a missing code path, another a genuinely
    corrupt row, and they need opposite responses.
    """
    text = str(exc).strip() or exc.__class__.__name__
    # Trim the specifics so the same fault groups: "no polygonal component in
    # LineString" and "... in Point" are one reason with two shapes, and the
    # shape is what the message ends with.
    return text.split(":")[0][:60]


@dataclass
class FieldReport:
    considered: int = 0
    imported_new: int = 0
    # Of imported_new, how many were pins rather than boundaries. Reported
    # separately because a registry that is half points is a different thing to
    # plan around than one that is all fields, and the total hides that.
    imported_points: int = 0
    resolved_same_as: int = 0
    resolved_child_of: int = 0
    skipped_already_done: int = 0
    quarantined: dict[str, list[str]] = field(default_factory=dict)
    uuid_promoted: list[str] = field(default_factory=list)
    canonicalization_changed: list[str] = field(default_factory=list)

    @property
    def quarantined_total(self) -> int:
        return sum(len(v) for v in self.quarantined.values())

    def quarantine(self, reason: str, v1_geo_id: str) -> None:
        self.quarantined.setdefault(reason, []).append(v1_geo_id)


@dataclass
class ProfileReport:
    considered: int = 0
    accounts_created: int = 0
    accounts_rejected: dict[str, list[str]] = field(default_factory=dict)
    fieldlists_created: int = 0
    associations_mapped: int = 0
    join_failures: dict[str, list[str]] = field(default_factory=dict)
    merged_ownership: dict[str, list[str]] = field(default_factory=dict)

    @property
    def join_failure_total(self) -> int:
        return sum(len(v) for v in self.join_failures.values())

    def reject(self, reason: str, key: str) -> None:
        self.accounts_rejected.setdefault(reason, []).append(key)

    def join_failure(self, reason: str, detail: str) -> None:
        self.join_failures.setdefault(reason, []).append(detail)


# --------------------------------------------------------------------------
# phase 1 — polygons
# --------------------------------------------------------------------------

def import_fields(
    source: LegacySource,
    repo: InMemoryRepo,
    *,
    limit: int | None = None,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    dry_run: bool = False,
    checkpoint_every: int = 500,
    on_checkpoint: Callable[[int, FieldReport], None] | None = None,
) -> FieldReport:
    report = FieldReport()

    for legacy in source.iter_fields(limit=limit):
        report.considered += 1

        # resumability: already aliased means already done
        if repo.resolve_v1(legacy.v1_geo_id) is not None:
            report.skipped_already_done += 1
            continue

        if not legacy.has_geometry:
            report.quarantine(QUARANTINE_NO_GEOMETRY, legacy.v1_geo_id)
            continue

        # A pin, however it was written. AR 1.0 accepted point registrations and
        # stored some of them as collapsed rings, and the polygon coverer cannot
        # describe a point -- it has no area. Without this branch every one of
        # them is rejected as unusable geometry, which is not a data quality
        # problem to write a policy about but a missing code path. AR2's own
        # registration has always handled points; only the importer did not.
        position = g2.point_coords(legacy.wkt)
        if position is not None:
            lat, lng = position
            tokens, v2_geo_id = g2.point_geo_id_with_tokens(lat, lng)
            content_hash = g2.point_content_hash(lat, lng)
            report.imported_points += 1
        else:
            try:
                tokens, v2_geo_id = g2.geo_id_with_tokens(legacy.wkt)
                content_hash = g2.content_hash(legacy.wkt)
            except Exception as exc:  # noqa: BLE001
                # A field whose geometry cannot be re-derived cannot be
                # re-identified, so it is quarantined rather than given a
                # surrogate key. The reason is recorded: one undifferentiated
                # bucket tells whoever reads the report how many are broken and
                # nothing whatever about what to do, which is the difference
                # between a finding and an actionable one.
                report.quarantine(
                    f"{QUARANTINE_UNUSABLE}:{_reason_of(exc)}", legacy.v1_geo_id)
                continue

        if _canonicalization_altered(legacy.wkt):
            report.canonicalization_changed.append(legacy.v1_geo_id)

        # genuinely new (or nested, which still registers)
        existing = repo.find_by_content_hash(content_hash)
        if existing is not None:
            # Identical canonical geometry already registered. content_hash is
            # UNIQUE in ar2, so this cannot be inserted -- alias to the existing
            # row instead of failing the run.
            report.quarantine(QUARANTINE_DUPLICATE_CONTENT, legacy.v1_geo_id)
            if not dry_run:
                repo.upsert_alias(AliasRow(legacy.v1_geo_id, existing.geo_id,
                                           legacy.v1_kind, SAME_AS))
            continue


        blocking = g2.blocking_key(tokens)
        candidates = repo.find_candidates(blocking)
        decision = resolve(tokens, candidates, threshold_pct=threshold_pct)

        if decision.outcome == SAME_AS:
            # Same field: it surrenders its identity to the canonical row.
            report.resolved_same_as += 1
            if not dry_run:
                repo.upsert_alias(AliasRow(
                    v1_geo_id=legacy.v1_geo_id,
                    v2_geo_id=decision.canonical_geo_id,
                    v1_kind=legacy.v1_kind,
                    relation=SAME_AS,
                ))
            _maybe_checkpoint(report, on_checkpoint, checkpoint_every)
            continue

        # child_of is NOT same_as. A plot inside a field is a DISTINCT field that
        # happens to be nested (P6). It keeps its own identity and gains a parent
        # edge; aliasing it to the parent would erase a real, separately-owned
        # field and hand its owner's grant scope to the parent's owner.
        parent_geo_id = decision.canonical_geo_id if decision.outcome == CHILD_OF else None
        if parent_geo_id is not None:
            report.resolved_child_of += 1

        row = GeoIdRow(
            geo_id=v2_geo_id,
            geo_id_short=g2.geo_id_short(v2_geo_id),
            content_hash=content_hash,
            tokens=tokens,
            blocking_keys=blocking,
            area_ha_approx=legacy.area_ha,
            country=legacy.country,
            field_name=legacy.field_name,
            crop=legacy.crop,
        )

        if not dry_run:
            try:
                repo.insert_geoid(row)
                repo.upsert_alias(AliasRow(legacy.v1_geo_id, v2_geo_id,
                                           legacy.v1_kind, SAME_AS))
                if parent_geo_id is not None:
                    repo.record_parent(v2_geo_id, parent_geo_id)
            except ConstraintViolation as exc:
                report.quarantine(f"{QUARANTINE_CONSTRAINT}:{exc.constraint}",
                                  legacy.v1_geo_id)
                continue

        report.imported_new += 1
        if legacy.v1_kind == "uuid":
            # headline benefit: a surrogate key becomes content-derived
            report.uuid_promoted.append(legacy.v1_geo_id)

        _maybe_checkpoint(report, on_checkpoint, checkpoint_every)

    return report


def _maybe_checkpoint(report, on_checkpoint, every) -> None:
    if on_checkpoint and report.considered % every == 0:
        on_checkpoint(report.considered, report)


def _canonicalization_altered(wkt: str) -> bool:
    """True when repair or rounding changed the geometry.

    These are latent data-quality problems in AR 1.0 worth surfacing, not
    failures -- the import proceeds.
    """
    try:
        from shapely.wkt import loads as load_wkt
        original = load_wkt(wkt)
        canonical = g2.canonicalize(wkt)
        if original.geom_type != canonical.geom_type:
            return True
        if original.area == 0:
            return False
        return abs(original.area - canonical.area) / original.area > 1e-9
    except Exception:  # noqa: BLE001
        return True


# --------------------------------------------------------------------------
# phase 2 — profiles
# --------------------------------------------------------------------------

class OutOfOrder(RuntimeError):
    """Phase 2 attempted before phase 1. See the module docstring."""


def import_profiles(
    source: LegacySource,
    repo: InMemoryRepo,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    list_name: str = "Imported from TerraPipe",
    merkle_root: Callable[[list[str]], str] | None = None,
) -> ProfileReport:
    if not repo.aliases:
        raise OutOfOrder(
            "no v1->v2 aliases present: run import_fields() first. A ListID is a "
            "Merkle root over sorted GeoIDs, so importing ownership before "
            "geometry yields ListIDs that are wrong but structurally valid."
        )

    root_of = merkle_root or _merkle_root
    report = ProfileReport()

    for profile in source.iter_profiles(limit=limit):
        report.considered += 1
        hub_id = _hub_account_id(profile)

        acct = HubAccount(
            hub_account_id=hub_id,
            email=profile.email or "",
            phone=profile.phone or "",
            first_name=profile.first_name or "",
            last_name=profile.last_name or "",
            is_active=False,     # migrated accounts start inactive
        )

        if not dry_run:
            try:
                repo.upsert_hub_account(acct)
            except ConstraintViolation as exc:
                report.reject(exc.constraint, f"{profile.source_key}: {exc.detail}")
                continue
        report.accounts_created += 1

        # join every v1 GeoID through the alias table
        v2_geoids: list[str] = []
        for v1 in profile.v1_geo_ids:
            v2 = repo.resolve_v1(v1)
            if v2 is None:
                report.join_failure("v1_not_in_alias_table",
                                    f"{profile.source_key} -> {v1}")
                continue
            if repo.get_geoid(v2) is None:
                report.join_failure("alias_points_at_missing_geoid",
                                    f"{profile.source_key} -> {v1} -> {v2}")
                continue
            v2_geoids.append(v2)
            report.associations_mapped += 1

        if not v2_geoids:
            continue

        unique_sorted = sorted(set(v2_geoids))
        list_id = root_of(unique_sorted)
        row = FieldListRow(list_id=list_id, name=list_name,
                           owner_hub_account_id=hub_id, geoids=unique_sorted)
        if not dry_run:
            try:
                repo.create_fieldlist(row)
            except ConstraintViolation as exc:
                report.reject(exc.constraint, f"{profile.source_key}: {exc.detail}")
                continue
        report.fieldlists_created += 1

    # M6: one v2 GeoID, several owners. Legal in the data model -- the registry
    # records no ownership -- but it means one user can see another's field data.
    # Surface it; do not resolve it.
    if not dry_run:
        for geo_id, owners in repo.multi_owner_geoids().items():
            report.merged_ownership[geo_id] = sorted(owners)

    return report


def _hub_account_id(profile: LegacyProfile) -> str:
    """Deterministic account id, so re-runs are idempotent."""
    return f"tp:{profile.source_key}"


def _merkle_root(geoids: list[str]) -> str:
    """ListID. Mirrors pancake_services/grants/merkle.py exactly.

    Leaves are SHA-256 of the GeoID string; pairs are hashed left||right; an odd
    node is promoted unchanged. Pass pancake's own merkle_root in production
    rather than relying on this copy staying in step.
    """
    import hashlib

    def sha(b: bytes) -> bytes:
        return hashlib.sha256(b).digest()

    members = sorted(set(geoids))
    if not members:
        raise ValueError("cannot compute a ListID for an empty member set")

    level = [sha(g.encode("utf-8")) for g in members]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level) - 1, 2):
            nxt.append(sha(level[i] + level[i + 1]))
        if len(level) % 2 == 1:
            nxt.append(level[-1])
        level = nxt
    return level[0].hex()
