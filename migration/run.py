"""
Import rehearsal runner.

    python -m migration.run --source fixture                 # works today
    python -m migration.run --source fixture --dry-run
    python -m migration.run --source ar1 --limit 5000        # once the adapter lands
    python -m migration.run --source fixture --threshold 90  # threshold sweep

    # adversarial sample instead of a prefix, and write to real databases
    python -m migration.run --source ar1 --sample 3000 \
        --ar2-url postgresql://... --hub-url postgresql://... --pancake-url sqlite:///...

--limit takes a naive prefix and is for smoke tests only. Use --sample for
anything whose result you intend to believe: a prefix of the table is ordered by
insertion and will contain none of the cases that break an import.

Prints the M1 inventory, the M2 sample justification, the M3 field-import report
and the M4 profile-import report. Exit code is non-zero when a finding needs a
decision, so this can gate a pipeline rather than being read by eye.
"""

from __future__ import annotations

import argparse
import sys
import time

from .pipeline import import_fields, import_profiles
from .repo import InMemoryRepo
from .sample import SampledSource, build_sample
from .sources import Ar1TerraPipeSource, FixtureSource

BAR = "=" * 78


def _h(title: str) -> None:
    print(f"\n{BAR}\n{title}\n{BAR}")


def _row(label: str, value, indent: int = 2) -> None:
    print(f"{' ' * indent}{label:<44}{value:>12}")


def print_inventory(inv) -> None:
    _h("M1 — SOURCE INVENTORY")
    print("  AR 1.0 registry")
    _row("total fields", f"{inv.total_fields:,}")
    _row("distinct L13 GeoIDs", f"{inv.distinct_l13_geo_ids:,}")
    _row("=> AR1 COLLISION COUNT", f"{inv.ar1_collision_count:,}")
    for kind, count in sorted(inv.kind_counts.items()):
        _row(f"  kind: {kind}", f"{count:,}", indent=4)
    _row("with geometry", f"{inv.with_geometry:,}")
    _row("parseable geometry", f"{inv.parseable_geometry:,}")
    _row("zero or invalid area", f"{inv.zero_or_invalid_area:,}")
    print("  area bands")
    band_total = 0
    for band, count in sorted(inv.area_bands.items()):
        _row(f"  {band}", f"{count:,}", indent=4)
        band_total += count
    _row("  bands sum", f"{band_total:,}", indent=4)
    if band_total != inv.total_fields:
        print(f"  *** BANDS DO NOT SUM: {inv.total_fields - band_total:,} fields "
              f"unaccounted for ***")

    print("\n  TerraPipe profiles")
    _row("total profiles", f"{inv.total_profiles:,}")
    _row("with at least one field", f"{inv.profiles_with_fields:,}")
    _row("max fields per profile", f"{inv.max_fields_per_profile:,}")
    _row("missing email", f"{inv.profiles_missing_email:,}")
    _row("missing phone", f"{inv.profiles_missing_phone:,}")
    _row("duplicate emails", f"{inv.duplicate_emails:,}")
    _row("duplicate phones", f"{inv.duplicate_phones:,}")

    print("\n  join health")
    _row("orphan profile refs (GeoID not in AR1)", f"{inv.orphan_profile_refs:,}")
    _row("unclaimed fields (no profile)", f"{inv.unclaimed_fields:,}")
    _row("v1 GeoIDs claimed by >1 profile", f"{inv.multi_owner_geo_ids:,}")


def print_field_report(r, elapsed: float) -> None:
    _h("M3 — FIELD IMPORT (AR 1.0 -> AR2, v2 GeoIDs at ingest)")
    _row("considered", f"{r.considered:,}")
    _row("imported new", f"{r.imported_new:,}")
    _row("  of which nested (child_of)", f"{r.resolved_child_of:,}", indent=4)
    _row("resolved same_as (merged)", f"{r.resolved_same_as:,}")
    _row("skipped, already imported", f"{r.skipped_already_done:,}")
    _row("UUID -> content-derived identity", f"{len(r.uuid_promoted):,}")
    _row("canonicalization altered geometry", f"{len(r.canonicalization_changed):,}")
    _row("quarantined", f"{r.quarantined_total:,}")
    for reason, ids in sorted(r.quarantined.items()):
        _row(f"  {reason}", f"{len(ids):,}", indent=4)
    if r.considered:
        _row("seconds / 1k fields", f"{elapsed / r.considered * 1000:.2f}")


def print_profile_report(r) -> None:
    _h("M4 — PROFILE IMPORT (TerraPipe -> Hub + Pancake)")
    _row("considered", f"{r.considered:,}")
    _row("accounts created", f"{r.accounts_created:,}")
    _row("field lists created", f"{r.fieldlists_created:,}")
    _row("associations mapped", f"{r.associations_mapped:,}")
    _row("accounts rejected", f"{sum(len(v) for v in r.accounts_rejected.values()):,}")
    for reason, keys in sorted(r.accounts_rejected.items()):
        _row(f"  {reason}", f"{len(keys):,}", indent=4)
        for k in keys[:3]:
            print(f"        e.g. {k}")
    _row("join failures", f"{r.join_failure_total:,}")
    for reason, details in sorted(r.join_failures.items()):
        _row(f"  {reason}", f"{len(details):,}", indent=4)
        for d in details[:3]:
            print(f"        e.g. {d}")

    _h("M6 — SHARED OWNERSHIP AFTER MERGE  (review by hand)")
    if not r.merged_ownership:
        print("  none in this run.")
    else:
        print(f"  {len(r.merged_ownership)} v2 GeoID(s) owned by more than one account.\n")
        print("  Legal in the data model: the registry records no ownership, so")
        print("  multiple grants over one GeoID are valid. But each of these means")
        print("  one user can see another user's field data. Inspect them.\n")
        for geo_id, owners in list(r.merged_ownership.items())[:20]:
            print(f"    {geo_id[:24]}...  <- {', '.join(owners)}")


def _open_repo(args):
    """Return (repo, closer). In-memory unless all three URLs are supplied.

    All three or none: a run that persisted geometry but not ownership would
    leave a registry whose ListIDs cannot be recomputed.
    """
    urls = (args.ar2_url, args.hub_url, args.pancake_url)
    if not any(urls):
        return InMemoryRepo(), lambda: None
    if not all(urls):
        raise SystemExit("--ar2-url, --hub-url and --pancake-url must be given together")

    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import Session

    from .db_repo import SqlAlchemyRepo
    from .models import Base, PancakeBase

    ar2_engine = create_engine(args.ar2_url)
    hub_engine = create_engine(args.hub_url)
    pancake_engine = create_engine(args.pancake_url)

    if args.create_all:
        # Scratch stacks only. Against a real deployment the services own these
        # tables and creating them here would diverge from their migrations.
        Base.metadata.create_all(ar2_engine)
        Base.metadata.create_all(hub_engine)
        PancakeBase.metadata.create_all(pancake_engine)
    else:
        # Only the tables this import owns.
        for table in ("geo_id_blocking_cell", "geo_id_regime_alias",
                      "geo_id_parent_edge"):
            Base.metadata.tables[table].create(ar2_engine, checkfirst=True)
        PancakeBase.metadata.tables["import_checkpoint"].create(
            pancake_engine, checkfirst=True)

        for engine, table, owner in ((ar2_engine, "geo_ids", "ar2"),
                                     (hub_engine, "users", "ar2-hub"),
                                     (pancake_engine, "fieldlists", "pancake")):
            if not inspect(engine).has_table(table):
                raise SystemExit(
                    f"table '{table}' is missing from the {owner} database.\n"
                    f"Run {owner}'s own migrations first -- this import must not "
                    f"create tables that {owner} owns, or the schema will diverge "
                    f"from its migrations.\n"
                    f"For a throwaway stack, pass --create-all.")

    sessions = [Session(ar2_engine), Session(hub_engine), Session(pancake_engine)]
    repo = SqlAlchemyRepo(*sessions)

    def closer() -> None:
        repo.commit()
        for s in sessions:
            s.close()

    return repo, closer


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AR1 + TerraPipe -> AR2 + Pancake import rehearsal")
    ap.add_argument("--source", choices=["fixture", "ar1"], default="fixture")
    ap.add_argument("--ar1-dsn", default="")
    ap.add_argument("--terrapipe-dsn", default="")
    ap.add_argument("--limit", type=int, default=None,
                    help="naive prefix; smoke tests only, prefer --sample")
    ap.add_argument("--sample", type=int, default=None, metavar="N",
                    help="adversarial stratified sample of N fields")
    ap.add_argument("--sample-cap", type=int, default=200,
                    help="max fields drawn from any one stratum")
    ap.add_argument("--threshold", type=float, default=95.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="same code path, writes suppressed")
    ap.add_argument("--ar2-url", default="", help="SQLAlchemy URL; omit for in-memory")
    ap.add_argument("--hub-url", default="")
    ap.add_argument("--pancake-url", default="")
    ap.add_argument("--create-all", action="store_true",
                    help="create service-owned tables too; throwaway stacks only")
    args = ap.parse_args(argv)

    if args.source == "fixture":
        source = FixtureSource()
    else:
        if not args.ar1_dsn or not args.terrapipe_dsn:
            print("--ar1-dsn and --terrapipe-dsn are required for --source ar1",
                  file=sys.stderr)
            return 2
        source = Ar1TerraPipeSource(args.ar1_dsn, args.terrapipe_dsn)

    print(f"source={args.source}  threshold={args.threshold}%  "
          f"limit={args.limit or 'none'}  dry_run={args.dry_run}")

    inv = source.inventory()
    print_inventory(inv)

    if args.sample:
        _h("M2 — ADVERSARIAL SAMPLE")
        sample = build_sample(list(source.iter_fields()), list(source.iter_profiles()),
                              budget=args.sample, per_stratum_cap=args.sample_cap)
        print(sample.justification())
        source = SampledSource(source, sample)

    repo, closer = _open_repo(args)

    started = time.time()
    fields = import_fields(source, repo, limit=args.limit,
                           threshold_pct=args.threshold, dry_run=args.dry_run)
    elapsed = time.time() - started
    print_field_report(fields, elapsed)

    if args.dry_run:
        print("\n(dry run: profile phase needs aliases, which dry run does not write)")
        closer()
        return 0

    profiles = import_profiles(source, repo)
    closer()
    print_profile_report(profiles)

    _h("DECISIONS REQUIRED")
    decisions = []
    if inv.ar1_collision_count:
        decisions.append(
            f"{inv.ar1_collision_count:,} AR1 L13 collisions — establish what AR 1.0 "
            f"did on collision: fail, or return the existing record?")
    if fields.quarantined_total:
        decisions.append(
            f"{fields.quarantined_total:,} fields quarantined — policy needed "
            f"(quarantine / flag / reject).")
    if profiles.merged_ownership:
        decisions.append(
            f"{len(profiles.merged_ownership)} merged GeoID(s) with multiple owners — "
            f"inspect by hand and set a policy.")
    if profiles.accounts_rejected:
        decisions.append(
            f"{sum(len(v) for v in profiles.accounts_rejected.values())} accounts "
            f"rejected on hub constraints — decide how to admit them.")
    if profiles.join_failure_total:
        decisions.append(
            f"{profiles.join_failure_total} associations failed to join — each one is "
            f"a user who will not see one of their fields.")

    if not decisions:
        print("  none.")
        return 0
    for i, d in enumerate(decisions, 1):
        print(f"  {i}. {d}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
