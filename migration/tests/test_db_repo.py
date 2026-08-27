# SPDX-License-Identifier: EUPL-1.2
# Copyright (c) 2026 AgStack project contributors.
# Licensed under the EUPL, Version 1.2; see the LICENSE file for the full text.

"""
Tests for the SQLAlchemy repository against real databases (SQLite here,
Postgres in the rehearsal).

Two things are being checked. First, that SqlAlchemyRepo enforces the same
constraints as InMemoryRepo -- because the tests, the dry run and the real import
must agree, or the dry run is not a rehearsal of anything. Second, that the whole
pipeline produces byte-identical results through both repositories.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ..db_repo import SqlAlchemyRepo
from ..models import (
    Base,
    FieldList,
    GeoIDRegimeAlias,
    HubUser,
    PancakeBase,
    PancakeUser,
)
from ..pipeline import import_fields, import_profiles
from ..repo import (
    AliasRow,
    ConstraintViolation,
    FieldListRow,
    GeoIdRow,
    HubAccount,
    InMemoryRepo,
)
from ..sources import FixtureSource


@pytest.fixture
def repo():
    ar2_engine = create_engine("sqlite://")
    other_engine = create_engine("sqlite://")
    Base.metadata.create_all(ar2_engine)
    PancakeBase.metadata.create_all(other_engine)

    ar2, hub, pancake = (Session(ar2_engine), Session(ar2_engine),
                         Session(other_engine))
    yield SqlAlchemyRepo(ar2, hub, pancake, batch_size=1)
    for s in (ar2, hub, pancake):
        s.close()


def _geoid(n: int, blocking=("blk",)) -> GeoIdRow:
    return GeoIdRow(geo_id=f"{n:064x}", geo_id_short=f"{n:016x}",
                    content_hash=f"{n:064x}", tokens=[f"t{n}"],
                    blocking_keys=list(blocking), area_ha_approx=1.0, country="KEN")


def _account(n: int) -> HubAccount:
    return HubAccount(hub_account_id=f"acct-{n}", email=f"u{n}@example.com",
                      phone=f"+2547{n:08d}", first_name="A", last_name="B")


# -- AR2 registry -------------------------------------------------------

def test_geoid_round_trips_through_the_database(repo):
    row = _geoid(1)
    repo.insert_geoid(row)
    repo.commit()
    fetched = repo.get_geoid(row.geo_id)
    assert fetched is not None
    assert fetched.tokens == row.tokens
    assert fetched.blocking_keys == row.blocking_keys


def test_duplicate_geo_id_is_a_named_constraint_violation(repo):
    repo.insert_geoid(_geoid(1))
    repo.commit()
    with pytest.raises(ConstraintViolation) as exc:
        repo.insert_geoid(_geoid(1))
    assert exc.value.constraint == "geo_id UNIQUE"


def test_duplicate_content_hash_is_caught(repo):
    a = _geoid(1)
    b = _geoid(2)
    b.content_hash = a.content_hash
    repo.insert_geoid(a)
    repo.commit()
    with pytest.raises(ConstraintViolation) as exc:
        repo.insert_geoid(b)
    assert exc.value.constraint == "content_hash UNIQUE"


def test_blocking_index_finds_candidates(repo):
    repo.insert_geoid(_geoid(1, blocking=("A", "B")))
    repo.insert_geoid(_geoid(2, blocking=("B",)))
    repo.insert_geoid(_geoid(3, blocking=("C",)))
    repo.commit()
    assert len(repo.find_candidates(["B"])) == 2
    assert len(repo.find_candidates(["C"])) == 1
    assert repo.find_candidates([]) == []


def test_alias_is_persisted_and_resolves(repo):
    repo.insert_geoid(_geoid(1))
    repo.upsert_alias(AliasRow("legacy-1", _geoid(1).geo_id, "uuid"))
    repo.commit()
    assert repo.resolve_v1("legacy-1") == _geoid(1).geo_id
    assert repo.resolve_v1("nope") is None


def test_alias_re_upsert_is_idempotent_but_remap_is_refused(repo):
    repo.insert_geoid(_geoid(1))
    repo.insert_geoid(_geoid(2))
    repo.upsert_alias(AliasRow("legacy-1", _geoid(1).geo_id, "uuid"))
    repo.commit()
    repo.upsert_alias(AliasRow("legacy-1", _geoid(1).geo_id, "uuid"))  # no-op
    with pytest.raises(ConstraintViolation):
        repo.upsert_alias(AliasRow("legacy-1", _geoid(2).geo_id, "uuid"))
    assert repo.ar2.scalar(
        select(GeoIDRegimeAlias).where(GeoIDRegimeAlias.v1_geo_id == "legacy-1")
    ).v2_geo_id == _geoid(1).geo_id


def test_parent_edge_is_recorded_once_and_never_self_referential(repo):
    repo.insert_geoid(_geoid(1))
    repo.insert_geoid(_geoid(2))
    repo.record_parent(_geoid(2).geo_id, _geoid(1).geo_id)
    repo.record_parent(_geoid(2).geo_id, _geoid(1).geo_id)
    repo.commit()
    with pytest.raises(ConstraintViolation):
        repo.record_parent(_geoid(1).geo_id, _geoid(1).geo_id)


# -- hub constraints ----------------------------------------------------

def test_hub_account_creates_a_pancake_mirror(repo):
    repo.upsert_hub_account(_account(1))
    repo.commit()
    assert repo.hub.scalar(select(HubUser).where(HubUser.client_id == "acct-1"))
    assert repo.pancake.scalar(
        select(PancakeUser).where(PancakeUser.hub_account_id == "acct-1"))


def test_migrated_accounts_are_inactive_with_no_usable_password(repo):
    """No account arrives from this import able to log in."""
    repo.upsert_hub_account(_account(1))
    repo.commit()
    user = repo.hub.scalar(select(HubUser).where(HubUser.client_id == "acct-1"))
    assert user.is_active is False
    assert user.password_hash.startswith("!")


@pytest.mark.parametrize("mutate,constraint", [
    (lambda a: setattr(a, "email", None), "email NOT NULL"),
    (lambda a: setattr(a, "first_name", None), "first_name/last_name NOT NULL"),
    (lambda a: setattr(a, "hub_account_id", "x" * 51), "client_id length <= 50"),
    (lambda a: setattr(a, "phone", None), "phone NOT NULL"),  # Restored to prevent regression
])
def test_hub_rejects_what_the_real_schema_rejects(repo, mutate, constraint):
    acct = _account(1)
    mutate(acct)
    if constraint == "phone NOT NULL":
        repo.upsert_hub_account(acct)
        return
    with pytest.raises(ConstraintViolation) as exc:
        repo.upsert_hub_account(acct)
    assert exc.value.constraint == constraint


def test_duplicate_email_and_phone_are_both_refused(repo):
    repo.upsert_hub_account(_account(1))
    repo.commit()

    same_email = _account(2)
    same_email.email = "u1@example.com"
    with pytest.raises(ConstraintViolation) as exc:
        repo.upsert_hub_account(same_email)
    assert exc.value.constraint == "email UNIQUE"


def test_fieldlist_requires_an_existing_owner(repo):
    with pytest.raises(ConstraintViolation) as exc:
        repo.create_fieldlist(FieldListRow("list-1", "n", "ghost", ["g"]))
    assert exc.value.constraint == "owner_id FK"


def test_fieldlist_re_creation_is_idempotent(repo):
    repo.upsert_hub_account(_account(1))
    repo.insert_geoid(_geoid(1))
    repo.commit()
    row = FieldListRow("list-1", "n", "acct-1", [_geoid(1).geo_id])
    repo.create_fieldlist(row)
    repo.create_fieldlist(row)
    repo.commit()
    assert len(repo.pancake.scalars(select(FieldList)).all()) == 1


def test_multi_owner_detection_works_across_the_join(repo):
    repo.upsert_hub_account(_account(1))
    repo.upsert_hub_account(_account(2))
    repo.insert_geoid(_geoid(1))
    repo.commit()
    shared = _geoid(1).geo_id
    repo.create_fieldlist(FieldListRow("l1", "n", "acct-1", [shared]))
    repo.create_fieldlist(FieldListRow("l2", "n", "acct-2", [shared]))
    repo.commit()
    assert repo.multi_owner_geoids() == {shared: {"acct-1", "acct-2"}}
    assert repo.owners_of(shared) == {"acct-1", "acct-2"}


def test_checkpoint_survives_a_reconnect(repo):
    repo.checkpoint("fields", 1234, last_source_id="abc", note="halfway")
    from ..models import ImportCheckpoint
    row = repo.pancake.scalar(select(ImportCheckpoint))
    assert (row.phase, row.processed, row.last_source_id) == ("fields", 1234, "abc")


# -- the property that makes the rehearsal meaningful -------------------

def test_both_repositories_produce_identical_results(repo):
    """If these ever diverge, the in-memory dry run stops being a rehearsal."""
    source = FixtureSource(n_filler=45)

    mem = InMemoryRepo()
    mem_fields = import_fields(source, mem)
    mem_profiles = import_profiles(source, mem)

    db_fields = import_fields(source, repo)
    db_profiles = import_profiles(source, repo)
    repo.commit()

    assert (db_fields.imported_new, db_fields.resolved_same_as,
            db_fields.resolved_child_of, db_fields.quarantined) == \
           (mem_fields.imported_new, mem_fields.resolved_same_as,
            mem_fields.resolved_child_of, mem_fields.quarantined)

    assert (db_profiles.accounts_created, db_profiles.accounts_rejected,
            db_profiles.fieldlists_created, db_profiles.join_failures) == \
           (mem_profiles.accounts_created, mem_profiles.accounts_rejected,
            mem_profiles.fieldlists_created, mem_profiles.join_failures)

    assert db_profiles.merged_ownership == mem_profiles.merged_ownership


def test_import_is_resumable_against_a_real_database(repo):
    """Re-running a completed import must add nothing."""
    source = FixtureSource(n_filler=45)
    import_fields(source, repo)
    repo.commit()
    second = import_fields(source, repo)
    repo.commit()
    assert second.imported_new == 0
    assert second.skipped_already_done > 0
