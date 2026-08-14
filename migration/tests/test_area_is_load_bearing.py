"""area_ha is not decoration. A NULL area silently disables same_as resolution.

AR2's live resolution path guards the same_as branch on an area ratio before it
ever computes an overlap (`app/utils.py`, check_percentage_match):

    cand_area  = cand_area or 0.0
    min_area   = min(area_new, cand_area) if area_new > 0 and cand_area > 0 else 0
    max_area   = max(area_new, cand_area) if area_new > 0 or cand_area > 0 else 1
    area_ratio = min_area / float(max_area) if max_area > 0 else 0
    if area_ratio >= (threshold / 100.0):
        ...   # <- the only place same_as can be reached

If either side's area is missing, area_ratio is 0, the guard fails, and same_as
is unreachable no matter how well the geometry agrees. An importer that leaves
area_ha_approx NULL therefore makes every imported field permanently
unresolvable against future registrations of the same boundary -- they land as
child_of, or as a fresh duplicate GeoID.

The importer does not derive area from the WKT; it passes source area through
(pipeline.py: area_ha_approx=legacy.area_ha). So this is a source contract, and
these tests hold the contract.
"""

from __future__ import annotations

import dataclasses as dc
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from migration.pipeline import import_fields
from migration.repo import InMemoryRepo
from migration.sources import FixtureSource

THRESHOLD = 95


def ar2_area_guard(area_new: float, cand_area: float | None, threshold: int = THRESHOLD) -> float:
    """The guard from app/utils.py check_percentage_match, verbatim in effect."""
    cand_area = cand_area or 0.0
    min_area = min(area_new, cand_area) if area_new > 0 and cand_area > 0 else 0
    max_area = max(area_new, cand_area) if area_new > 0 or cand_area > 0 else 1
    return min_area / float(max_area) if max_area > 0 else 0


# ------------------------------------------------- why it is load-bearing

def test_populated_area_lets_same_as_be_reached():
    assert ar2_area_guard(4.0, 4.0) >= THRESHOLD / 100.0


@pytest.mark.parametrize("missing", [None, 0.0])
def test_missing_area_makes_same_as_unreachable(missing):
    """Identical geometry, but the branch that could match it is never entered."""
    assert ar2_area_guard(4.0, missing) == 0.0
    assert ar2_area_guard(4.0, missing) < THRESHOLD / 100.0


def test_the_guard_ignores_geometry_entirely():
    """No overlap value can rescue a missing area: the guard runs first."""
    for cand_area in (None, 0.0):
        assert ar2_area_guard(1e-9, cand_area) < THRESHOLD / 100.0
        assert ar2_area_guard(1e9, cand_area) < THRESHOLD / 100.0


# ------------------------------------------------- the source contract

def test_source_area_reaches_ar2_unchanged():
    """The importer passes area through; it does not compute it."""
    src = FixtureSource()
    repo = InMemoryRepo()
    import_fields(src, repo)

    by_area = {f.v1_geo_id: f.area_ha for f in src.iter_fields()}
    checked = 0
    for v1, area in by_area.items():
        v2 = repo.resolve_v1(v1)
        if v2 is None:
            continue
        row = repo.get_geoid(v2)
        if row is None or area is None:
            continue
        assert row.area_ha_approx == area, f"{v1}: area not carried through"
        checked += 1
    assert checked > 0, "fixture yielded no areas to check"


def test_an_adapter_that_drops_area_is_detected():
    """A source yielding area_ha=None for everything -- the failure mode to catch.

    This is what an adapter that never reads AR1's area column produces. Every
    imported row lands with a NULL area, and by the tests above every one of
    them is then unreachable via same_as.
    """
    src = FixtureSource()
    dropped = [dc.replace(f, area_ha=None) for f in src.iter_fields()]

    class AreaBlindSource:
        def inventory(self):
            return src.inventory()

        def iter_fields(self, limit=None):
            return iter(dropped if limit is None else dropped[:limit])

        def iter_profiles(self, limit=None):
            return src.iter_profiles(limit=limit)

    repo = InMemoryRepo()
    report = import_fields(AreaBlindSource(), repo)

    imported = [repo.get_geoid(g) for g in repo.geoids]
    assert imported, "nothing imported"
    null_area = [r for r in imported if r is not None and r.area_ha_approx is None]

    assert len(null_area) == len([r for r in imported if r is not None]), (
        "expected every row to carry a NULL area under an area-blind adapter"
    )
    assert report.imported_new > 0

    # and that is exactly the population same_as can no longer reach
    for row in null_area:
        assert ar2_area_guard(4.0, row.area_ha_approx) < THRESHOLD / 100.0


def test_area_bands_collapse_when_area_is_dropped():
    """Secondary cost: the adversarial sample loses its size coverage."""
    from migration.sample import build_sample

    src = FixtureSource()
    fields = list(src.iter_fields())
    profiles = list(src.iter_profiles())

    with_area = build_sample(fields, profiles, budget=3000, per_stratum_cap=200)
    without = build_sample([dc.replace(f, area_ha=None) for f in fields],
                           profiles, budget=3000, per_stratum_cap=200)

    def bands(s):
        return {k for k, v in s.strata.items() if k.startswith("area_") and v}

    assert len(bands(with_area)) >= 4
    assert bands(without) == {"area_unknown"}, (
        "dropping area collapses every size band into one, so the rehearsal "
        "cannot surface size-dependent behaviour"
    )
