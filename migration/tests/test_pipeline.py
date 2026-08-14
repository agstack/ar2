"""End-to-end import rehearsal on synthetic AR1 + TerraPipe fixtures.

Run: python -m pytest migration/tests/test_pipeline.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from migration import geoid_v2 as g2
from migration.pipeline import (
    QUARANTINE_DUPLICATE_CONTENT,
    QUARANTINE_NO_GEOMETRY,
    QUARANTINE_UNUSABLE,
    OutOfOrder,
    import_fields,
    import_profiles,
)
from migration.repo import InMemoryRepo
from migration.sources import FixtureSource


@pytest.fixture
def src():
    return FixtureSource()


@pytest.fixture
def imported(src):
    repo = InMemoryRepo()
    fields = import_fields(src, repo)
    profiles = import_profiles(src, repo)
    return repo, fields, profiles


# ------------------------------------------------------------------ inventory

def test_inventory_is_self_consistent(src):
    inv = src.inventory()
    assert inv.total_fields == len(list(src.iter_fields()))
    # the area bands must account for EVERY field -- the check that would have
    # caught 13,343 missing rows on day one
    assert sum(inv.area_bands.values()) == inv.total_fields
    assert sum(inv.kind_counts.values()) == inv.total_fields
    assert inv.orphan_profile_refs >= 1
    assert inv.unclaimed_fields >= 1
    assert inv.profiles_missing_phone >= 1
    assert inv.duplicate_phones >= 1


# ------------------------------------------------------------------ phase 1

def testfields_imported_and_aliased(imported):
    repo, fields, _ = imported
    assert fields.considered > 0
    assert fields.imported_new > 0
    # every field that was not quarantined must be resolvable from its v1 id.
    # child_of fields register in their own right, so they are inside
    # imported_new rather than a separate bucket.
    assert len(repo.aliases) == (
        fields.imported_new + fields.resolved_same_as
        + len(fields.quarantined.get(QUARANTINE_DUPLICATE_CONTENT, []))
    )


def test_v1_identifiers_resolve_forever(imported, src):
    """P7: every usable v1 GeoID must map to a live v2 GeoID."""
    repo, fields, _ = imported
    quarantined = {v for ids in fields.quarantined.values() for v in ids}
    checked = 0
    for legacy in src.iter_fields():
        if legacy.v1_geo_id in quarantined:
            continue
        v2 = repo.resolve_v1(legacy.v1_geo_id)
        assert v2 is not None, f"{legacy.v1_geo_id} lost"
        assert repo.get_geoid(v2) is not None, f"{legacy.v1_geo_id} -> dangling {v2}"
        checked += 1
    assert checked > 0


def test_uuid_fields_gain_content_derived_identity(imported, src):
    """The headline benefit: surrogate keys become recomputable identities."""
    repo, _, _ = imported
    v2 = repo.resolve_v1(src.uuid_field)
    assert v2 is not None
    assert len(v2) == 64 and int(v2, 16) >= 0        # a real hash, not a UUID
    assert repo.get_geoid(v2).geo_id == g2.geo_id(
        next(f.wkt for f in src.iter_fields() if f.v1_geo_id == src.uuid_field))


def test_degenerate_geometry_quarantined_not_invented(imported, src):
    repo, fields, _ = imported
    assert src.no_geom in fields.quarantined[QUARANTINE_NO_GEOMETRY]
    unusable = fields.quarantined[QUARANTINE_UNUSABLE]
    assert src.bad_geom in unusable and src.unparseable in unusable
    # and they must NOT have received an identity
    for bad in (src.no_geom, src.bad_geom, src.unparseable):
        assert repo.resolve_v1(bad) is None


def test_exact_duplicate_geometry_aliases_rather_than_failing(imported, src):
    """content_hash is UNIQUE in ar2; the second copy must alias, not crash."""
    repo, _fields, _ = imported
    a = repo.resolve_v1(src.exact_dup_a)
    b = repo.resolve_v1(src.exact_dup_b)
    assert a is not None and b is not None
    assert a == b, "identical geometry must converge on one v2 GeoID"


def test_nested_plot_keeps_its_own_identity(imported, src):
    """P6. A plot inside a field is a distinct, separately-owned field.

    Aliasing it to its parent would erase it and hand its owner's grant scope to
    the parent's owner -- so child_of must register, then record a parent edge.
    """
    repo, fields, _ = imported
    big = repo.resolve_v1(src.big_field)
    small = repo.resolve_v1(src.small_plot)
    assert big is not None and small is not None
    assert big != small, "a plot inside a field must not be merged into it"
    assert fields.resolved_child_of >= 1
    assert repo.parent_edges.get(small) == big


def test_holes_and_multipart_get_distinct_identities(imported, src):
    repo, _, _ = imported
    for v1 in (src.holed, src.multipart):
        v2 = repo.resolve_v1(v1)
        assert v2 is not None and repo.get_geoid(v2) is not None


# ------------------------------------------------------------------ idempotency

def test_second_run_is_a_no_op(src):
    repo = InMemoryRepo()
    import_fields(src, repo)
    snapshot = (dict(repo.geoids), dict(repo.aliases))

    second = import_fields(src, repo)
    assert second.imported_new == 0
    assert second.skipped_already_done == len(repo.aliases)
    assert repo.geoids.keys() == snapshot[0].keys()
    assert repo.aliases.keys() == snapshot[1].keys()


def test_resume_after_interruption_reaches_same_state(src):
    """Simulate a kill by importing a prefix, then resuming."""
    full = InMemoryRepo()
    import_fields(src, full)

    partial = InMemoryRepo()
    import_fields(src, partial, limit=15)          # "killed" after 15
    assert len(partial.geoids) < len(full.geoids)
    import_fields(src, partial)                    # resume

    assert partial.geoids.keys() == full.geoids.keys()
    assert partial.aliases.keys() == full.aliases.keys()


def test_dry_run_writes_nothing(src):
    repo = InMemoryRepo()
    report = import_fields(src, repo, dry_run=True)
    assert report.considered > 0
    assert not repo.geoids and not repo.aliases


# ------------------------------------------------------------------ ordering

def test_profiles_refuse_to_run_beforefields(src):
    repo = InMemoryRepo()
    with pytest.raises(OutOfOrder):
        import_profiles(src, repo)


# ------------------------------------------------------------------ phase 2

def test_accounts_and_lists_created(imported):
    _, _, profiles = imported
    assert profiles.accounts_created > 0
    assert profiles.fieldlists_created > 0
    assert profiles.associations_mapped > 0


def test_hub_constraints_reject_rather_than_corrupt(imported):
    """Missing phone, missing email, no name, duplicate phone must all be caught."""
    _, _, profiles = imported
    reasons = set(profiles.accounts_rejected)
    assert "phone NOT NULL" in reasons
    assert "email NOT NULL" in reasons
    assert "first_name/last_name NOT NULL" in reasons
    assert "phone UNIQUE" in reasons


def test_orphan_profile_reference_is_reported_not_silent(imported):
    _, _, profiles = imported
    assert "v1_not_in_alias_table" in profiles.join_failures
    assert profiles.join_failure_total >= 1


def test_user_field_set_matches_source(imported, src):
    """A migrated user's fields must equal their TerraPipe fields, modulo
    quarantined geometry and merges."""
    repo, fields, _ = imported
    quarantined = {v for ids in fields.quarantined.values() for v in ids
                   if repo.resolve_v1(v) is None}

    for profile in src.iter_profiles():
        hub_id = f"tp:{profile.source_key}"
        if hub_id not in repo.hub_accounts:
            continue
        expected = {repo.resolve_v1(v) for v in profile.v1_geo_ids
                    if v not in quarantined}
        expected.discard(None)
        actual = set()
        for fl in repo.fieldlists.values():
            if fl.owner_hub_account_id == hub_id:
                actual |= set(fl.geoids)
        assert actual == expected, f"{profile.source_key}: {actual} != {expected}"


# ------------------------------------------------------------------ the M6 case

def test_mergedfields_produce_shared_ownership_and_it_is_surfaced(imported, src):
    """Two users, near-identical polygons -> one v2 GeoID -> two owners.

    Legal in the data model, since the registry records no ownership. But it
    means user A can see user B's field data, so it MUST be reported.
    """
    repo, _fields, profiles = imported

    a = repo.resolve_v1(src.near_dup_a)
    b = repo.resolve_v1(src.near_dup_b)
    assert a is not None and b is not None

    if a == b:
        # they merged: both owners must appear, and it must be in the report
        owners = repo.owners_of(a)
        assert owners == {"tp:tp-1", "tp:tp-2"}, owners
        assert a in profiles.merged_ownership
        assert profiles.merged_ownership[a] == ["tp:tp-1", "tp:tp-2"]
    else:
        # they stayed distinct at this threshold: then no shared ownership
        assert repo.owners_of(a) == {"tp:tp-1"}
        assert repo.owners_of(b) == {"tp:tp-2"}


def test_threshold_changes_merge_behaviour(src):
    """The tuning knob must actually move: strict keeps them apart, loose merges.

    This is why the threshold has to be re-tuned against real captures -- it now
    means 95% of true geometry, not 95% of bounding boxes.
    """
    outcomes = {}
    for threshold in (99.9, 90.0):
        repo = InMemoryRepo()
        import_fields(src, repo, threshold_pct=threshold)
        outcomes[threshold] = (
            repo.resolve_v1(src.near_dup_a) == repo.resolve_v1(src.near_dup_b)
        )
    assert outcomes[99.9] is False, "99.9% should keep 4 m-offset fields distinct"
    assert outcomes[90.0] is True, "90% should merge them"


def test_listid_is_merkle_root_of_sorted_members(imported):
    from migration.pipeline import _merkle_root
    repo, _, _ = imported
    for fl in repo.fieldlists.values():
        assert fl.list_id == _merkle_root(fl.geoids)
        assert fl.geoids == sorted(set(fl.geoids))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "--tb=short"]))
