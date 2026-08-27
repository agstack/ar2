# SPDX-License-Identifier: EUPL-1.2
# Copyright (c) 2026 AgStack project contributors.
# Licensed under the EUPL, Version 1.2; see the LICENSE file for the full text.

"""
Adversarial stratified sampling (M2).

A random few thousand rows out of 28,000 will contain none of the cases that
break an import, and will produce a clean result that means nothing. This module
selects *for* the hazards instead, then fills the remainder proportionally so the
sample is still usable for throughput estimates.

THE NEAR-DUPLICATE PROBLEM, AND WHY IT IS CHEAP TO SOLVE HERE.

The most valuable stratum is "near-identical polygons registered by different
users", because that is what produces one v2 GeoID with two owners. Finding it
would ordinarily mean covering every field first, which is the expensive step.

It is free instead: AR 1.0's own L13 GeoID is already a blocking key. Two fields
sharing an L13 GeoID are within roughly a kilometre of each other, which is
exactly the candidate set. That column is already in the source data, so the
clusters fall out of a group-by with no geometry work at all. The same grouping
also yields the AR1 collision count.

Strata are prioritised: when the budget is smaller than the union of all strata,
hazards are kept and filler is dropped.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from .sources import KIND_L20, KIND_UUID, LegacyField, LegacyProfile

# Ordered by priority. Earlier strata survive a tight budget.
STRATUM_MULTI_OWNER = "multi_owner_cluster"
STRATUM_L13_COLLISION = "l13_collision_cluster"
STRATUM_UUID = "uuid_fallback"
STRATUM_L20 = "l20_fallback"
STRATUM_NO_GEOMETRY = "no_geometry"
STRATUM_UNPARSEABLE = "unparseable_geometry"
STRATUM_ZERO_AREA = "zero_or_missing_area"
STRATUM_ORPHAN = "profile_with_orphan_ref"
STRATUM_UNCLAIMED = "unclaimed_field"
FINDING_ORPHAN_REFS = "orphan_refs"
STRATUM_MANY_FIELDS = "profile_with_many_fields"
STRATUM_AREA_PREFIX = "area_"

PRIORITY = [
    STRATUM_MULTI_OWNER,
    STRATUM_L13_COLLISION,
    STRATUM_UUID,
    STRATUM_L20,
    STRATUM_NO_GEOMETRY,
    STRATUM_UNPARSEABLE,
    STRATUM_ZERO_AREA,
    STRATUM_ORPHAN,
    STRATUM_UNCLAIMED,
    STRATUM_MANY_FIELDS,
]

MANY_FIELDS_THRESHOLD = 10


@dataclass
class Sample:
    """A selected sample plus the justification for its composition."""

    field_ids: set[str] = field(default_factory=set)
    profile_keys: set[str] = field(default_factory=set)
    strata: dict[str, list[str]] = field(default_factory=dict)
    # Evidence that cannot be *selected* because it is not a field: an orphan
    # reference points at a GeoID AR 1.0 does not have. Reported, not sampled.
    findings: dict[str, list[str]] = field(default_factory=dict)
    budget: int = 0
    filler_added: int = 0
    budget_exhausted: bool = False

    def counts(self) -> dict[str, int]:
        return {name: len(ids) for name, ids in sorted(self.strata.items())}

    def justification(self) -> str:
        lines = [
            (f"sample of {len(self.field_ids)} fields and {len(self.profile_keys)} "
             f"profiles (budget {self.budget})"),
            "",
            "selected for these hazards:",
        ]
        for name, ids in sorted(self.strata.items()):
            if ids:
                lines.append(f"  {name:<28} {len(ids):>6}")
        lines.append(f"  {'proportional filler':<28} {self.filler_added:>6}")
        if self.budget_exhausted:
            lines += ["", "BUDGET EXHAUSTED: lower-priority strata were dropped.",
                      "Raise the budget if the dropped ones matter."]
        for name, ids in sorted(self.findings.items()):
            lines += ["", f"{name} (reported, not selectable): {len(ids)}"]
        missing = [s for s in PRIORITY if not self.strata.get(s)]
        if missing:
            lines += ["", "strata with NO members in the source (verify this is real,"]
            lines += ["not a query bug):"]
            lines += [f"  {m}" for m in missing]
        return "\n".join(lines)


def _parse_ok(wkt: str | None) -> bool:
    if not wkt or not wkt.strip():
        return False
    try:
        from shapely.wkt import loads
        geom = loads(wkt)
        return not geom.is_empty
    except Exception:  # noqa: BLE001
        return False


def _area_band(area_ha: float | None) -> str:
    if area_ha is None:
        return f"{STRATUM_AREA_PREFIX}unknown"
    if area_ha <= 0:
        return f"{STRATUM_AREA_PREFIX}zero"
    for limit, name in ((1, "<1"), (5, "1-5"), (50, "5-50"), (500, "50-500")):
        if area_ha < limit:
            return f"{STRATUM_AREA_PREFIX}{name}"
    return f"{STRATUM_AREA_PREFIX}>500"


def build_sample(
    fields: Iterable[LegacyField],
    profiles: Iterable[LegacyProfile],
    *,
    budget: int = 3000,
    per_stratum_cap: int = 200,
) -> Sample:
    """Select an adversarial sample and explain its composition.

    `per_stratum_cap` bounds any single hazard so one pathology cannot consume
    the whole budget; raise it when a stratum is the thing under investigation.
    """
    fields = list(fields)
    profiles = list(profiles)
    by_id = {f.v1_geo_id: f for f in fields}

    sample = Sample(budget=budget)
    strata: dict[str, list[str]] = defaultdict(list)

    # -- ownership map: which profiles claim which v1 GeoID -----------------
    claims: dict[str, set[str]] = defaultdict(set)
    for p in profiles:
        for gid in p.v1_geo_ids:
            claims[gid].add(p.source_key)

    # -- L13 clusters, free from AR1's own identifier ------------------------
    # Fields sharing an L13 GeoID are within ~1 km: the near-duplicate candidates.
    l13_groups: dict[str, list[str]] = defaultdict(list)
    for f in fields:
        key = getattr(f, "v1_l13_geo_id", None) or f.v1_geo_id
        l13_groups[key].append(f.v1_geo_id)

    for key, members in l13_groups.items():
        if len(members) < 2:
            continue
        owners = set()
        for gid in members:
            owners |= claims.get(gid, set())
        target = STRATUM_MULTI_OWNER if len(owners) > 1 else STRATUM_L13_COLLISION
        strata[target].extend(members)

    # -- identifier-kind hazards --------------------------------------------
    for f in fields:
        if f.v1_kind == KIND_UUID:
            strata[STRATUM_UUID].append(f.v1_geo_id)
        elif f.v1_kind == KIND_L20:
            strata[STRATUM_L20].append(f.v1_geo_id)

    # -- geometry hazards -----------------------------------------------------
    for f in fields:
        if not f.has_geometry:
            strata[STRATUM_NO_GEOMETRY].append(f.v1_geo_id)
        elif not _parse_ok(f.wkt):
            strata[STRATUM_UNPARSEABLE].append(f.v1_geo_id)
        if f.area_ha is None or f.area_ha <= 0:
            strata[STRATUM_ZERO_AREA].append(f.v1_geo_id)

    # -- join hazards ---------------------------------------------------------
    orphan_refs: list[str] = []
    for p in profiles:
        missing = [g for g in p.v1_geo_ids if g not in by_id]
        if missing:
            orphan_refs.extend(missing)
            # The orphan itself has no AR1 row, so it cannot be sampled. Select
            # the profile's *resolvable* fields instead: that pulls the profile
            # into the run, which is what exercises the join failure.
            strata[STRATUM_ORPHAN].extend(g for g in p.v1_geo_ids if g in by_id)
        if len(p.v1_geo_ids) >= MANY_FIELDS_THRESHOLD:
            strata[STRATUM_MANY_FIELDS].extend(p.v1_geo_ids[:per_stratum_cap])
    if orphan_refs:
        sample.findings[FINDING_ORPHAN_REFS] = orphan_refs

    for f in fields:
        if f.v1_geo_id not in claims:
            strata[STRATUM_UNCLAIMED].append(f.v1_geo_id)

    # -- every area band must be represented ---------------------------------
    for f in fields:
        strata[_area_band(f.area_ha)].append(f.v1_geo_id)

    # -- assemble under the budget, hazards first -----------------------------
    chosen: set[str] = set()
    ordered = PRIORITY + sorted(k for k in strata if k.startswith(STRATUM_AREA_PREFIX))

    for name in ordered:
        members = strata.get(name, [])
        if not members:
            sample.strata.setdefault(name, [])
            continue
        # de-duplicate while preserving order, then cap
        seen, unique = set(), []
        for gid in members:
            if gid not in seen and gid in by_id:
                seen.add(gid)
                unique.append(gid)
        take = unique[:per_stratum_cap]
        room = budget - len(chosen)
        if room < len(take):
            sample.budget_exhausted = True
        take = take[:max(room, 0)]
        chosen |= set(take)
        sample.strata[name] = take

    # Filler pads a sample that came in UNDER budget, so throughput numbers stay
    # meaningful. It is never added after a stratum was cut for lack of room --
    # backfilling arbitrary rows over dropped hazards is the failure this whole
    # module exists to avoid.
    if not sample.budget_exhausted:
        for f in fields:
            if len(chosen) >= budget:
                break
            if f.v1_geo_id not in chosen:
                chosen.add(f.v1_geo_id)
                sample.filler_added += 1

    orphan_holders = {
        p.source_key for p in profiles
        if any(g not in by_id for g in p.v1_geo_ids)
    }
    sample.field_ids = chosen
    sample.profile_keys = {
        p.source_key for p in profiles
        if any(g in chosen for g in p.v1_geo_ids)
        or not p.v1_geo_ids
        or p.source_key in orphan_holders
    }
    return sample


class SampledSource:
    """Restricts any LegacySource to a Sample. Inventory still reports the whole."""

    def __init__(self, source, sample: Sample):
        self._source = source
        self._sample = sample

    def inventory(self):
        return self._source.inventory()

    def iter_fields(self, limit: int | None = None):
        count = 0
        for f in self._source.iter_fields():
            if f.v1_geo_id not in self._sample.field_ids:
                continue
            if limit is not None and count >= limit:
                return
            count += 1
            yield f

    def iter_profiles(self, limit: int | None = None):
        count = 0
        for p in self._source.iter_profiles():
            if p.source_key not in self._sample.profile_keys:
                continue
            if limit is not None and count >= limit:
                return
            count += 1
            yield p
