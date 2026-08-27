# SPDX-License-Identifier: EUPL-1.2
# Copyright (c) 2026 AgStack project contributors.
# Licensed under the EUPL, Version 1.2; see the LICENSE file for the full text.

"""
Tests for adversarial sampling (M2).

The property that matters is not "returns N rows" -- it is that a small budget
still contains every hazard. A sampler that quietly drops the multi-owner cluster
under pressure produces a clean dry run that proves nothing, which is the exact
failure this module exists to prevent.
"""

from __future__ import annotations

from ..sample import (
    STRATUM_L13_COLLISION,
    STRATUM_MANY_FIELDS,
    STRATUM_MULTI_OWNER,
    STRATUM_NO_GEOMETRY,
    STRATUM_ORPHAN,
    STRATUM_UNCLAIMED,
    STRATUM_UNPARSEABLE,
    STRATUM_UUID,
    SampledSource,
    build_sample,
)
from ..sources import FixtureSource


def _sample(budget=3000, cap=200):
    src = FixtureSource()
    return src, build_sample(list(src.iter_fields()), list(src.iter_profiles()),
                             budget=budget, per_stratum_cap=cap)


def test_multi_owner_cluster_is_found_from_the_l13_key_alone():
    """No covering, no geometry work: the cluster falls out of AR1's own column."""
    src, sample = _sample()
    found = set(sample.strata[STRATUM_MULTI_OWNER])
    assert src.near_dup_a in found
    assert src.near_dup_b in found


def test_same_owner_l13_cluster_is_separated_from_the_multi_owner_one():
    """Nesting pair shares an L13 key but one co-op owns both: lower priority."""
    src, sample = _sample()
    collisions = set(sample.strata[STRATUM_L13_COLLISION])
    assert src.big_field in collisions and src.small_plot in collisions
    assert src.big_field not in set(sample.strata[STRATUM_MULTI_OWNER])


def test_every_hazard_stratum_is_represented():
    _src, sample = _sample()
    for stratum in (STRATUM_MULTI_OWNER, STRATUM_UUID, STRATUM_NO_GEOMETRY,
                    STRATUM_UNPARSEABLE, STRATUM_ORPHAN, STRATUM_UNCLAIMED,
                    STRATUM_MANY_FIELDS):
        assert sample.strata[stratum], f"{stratum} empty"


def test_hazards_survive_a_budget_far_smaller_than_the_source():
    """The whole point. Highest-priority hazards keep their place under pressure."""
    src, sample = _sample(budget=12, cap=4)
    assert len(sample.field_ids) <= 12
    assert src.near_dup_a in sample.field_ids
    assert src.near_dup_b in sample.field_ids


def test_no_filler_is_added_once_the_budget_has_cut_a_stratum():
    """Backfilling arbitrary rows over dropped hazards is the failure to avoid."""
    _, sample = _sample(budget=12, cap=4)
    assert sample.budget_exhausted
    assert sample.filler_added == 0
    assert "BUDGET EXHAUSTED" in sample.justification()


def test_a_generous_budget_covers_the_whole_source():
    src, sample = _sample(budget=3000)
    assert not sample.budget_exhausted
    assert sample.field_ids == {f.v1_geo_id for f in src.iter_fields()}


def test_orphan_refs_are_reported_but_never_selected_as_fields():
    """An orphan has no AR1 row, so it cannot be a sampled field -- only a finding."""
    src, sample = _sample()
    ids = {f.v1_geo_id for f in src.iter_fields()}
    assert all(gid in ids for gid in sample.field_ids)
    assert sample.findings["orphan_refs"]
    assert all(gid not in ids for gid in sample.findings["orphan_refs"])


def test_the_profile_holding_an_orphan_ref_is_still_in_the_sample():
    """Otherwise the join failure never gets exercised."""
    _, sample = _sample(budget=12, cap=4)
    assert "tp-6" in sample.profile_keys


def test_area_bands_are_all_present():
    _src, sample = _sample()
    bands = [k for k in sample.strata if k.startswith("area_") and sample.strata[k]]
    assert len(bands) >= 4


def test_profiles_follow_their_fields():
    src, sample = _sample(budget=12, cap=4)
    profiles = {p.source_key: p for p in src.iter_profiles()}
    for key in sample.profile_keys:
        p = profiles[key]
        assert not p.v1_geo_ids or any(g in sample.field_ids for g in p.v1_geo_ids)


def test_sampled_source_restricts_iteration_but_not_inventory():
    """Inventory must always describe the WHOLE source, or the findings are wrong."""
    src, sample = _sample(budget=12, cap=4)
    sampled = SampledSource(src, sample)
    assert len(list(sampled.iter_fields())) == len(sample.field_ids)
    assert sampled.inventory().total_fields == len(list(src.iter_fields()))


def test_sample_is_deterministic():
    a = _sample(budget=20, cap=5)[1]
    b = _sample(budget=20, cap=5)[1]
    assert a.field_ids == b.field_ids


def test_justification_names_any_stratum_with_no_members():
    """An empty stratum is either a clean source or a broken query. Say so."""
    fields = list(FixtureSource().iter_fields())
    sample = build_sample(fields, [], budget=500)
    text = sample.justification()
    assert "strata with NO members" in text
    assert STRATUM_ORPHAN in text


def test_collision_count_comes_from_the_l13_column():
    inv = FixtureSource().inventory()
    assert inv.ar1_collision_count == 3
