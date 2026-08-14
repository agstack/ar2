# `migration` — AR 1.0 + TerraPipe → AR2 + Hub + Pancake

Import tooling that reads legacy registrations from AR 1.0 and legacy profiles
from TerraPipe, computes GeoID v2 identities at ingest, and lands the result in
the AR2 registry, the Hub account store and Pancake.

AR2 owns the import because it is the only component that has to touch all three
systems. Exercised in CI by the `migration` job in `.github/workflows/ci.yml`.

## Requirements

Python 3.10+ — `s2geometry`'s prebuilt `abi3` wheels do not resolve on 3.9.

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r migration/requirements.txt
pip install pytest
```

## Usage

```bash
pytest migration/tests -q                     # 78 tests

python -m migration.run --source fixture      # rehearsal on synthetic data
python -m migration.run --source fixture --dry-run
python -m migration.run --source fixture --threshold 90

# adversarial sample, writing to real databases
python -m migration.run --source ar1 \
    --ar1-dsn ... --terrapipe-dsn ... --sample 3000 \
    --ar2-url postgresql://... --hub-url postgresql://... --pancake-url sqlite:///...
```

`run.py` prints the inventory, the sample justification, the field-import report,
the profile-import report and the shared-ownership report. **It exits non-zero
when a finding requires a human decision**, so it can gate a pipeline rather than
being read by eye.

Omitting the three `--*-url` flags runs entirely in memory. Supplying them writes
to real databases; all three are required together, because a run that persisted
geometry but not ownership leaves a registry whose ListIDs cannot be recomputed.
The runner creates only the tables the import itself owns and refuses to start if
`geo_ids`, hub `users` or pancake `fieldlists` are absent — those belong to the
services' own migrations. `--create-all` overrides this for throwaway stacks.

Prefer `--sample` over `--limit`. A prefix of the table is ordered by insertion
and will contain none of the cases that break an import.

## Modules

| Module | Responsibility |
|---|---|
| `geoid_v2.py` | The v2 primitive: canonicalize → cover the polygon at L20 → normalize → sort tokens → SHA-256. Also leaf-cell area and the L13 blocking key. |
| `resolve.py` | Area-exact IoU and containment over token covers; `resolve()` returns `new` / `same_as` / `child_of`. |
| `sources.py` | `LegacySource` protocol, the `Ar1TerraPipeSource` adapter, and `FixtureSource`. |
| `sample.py` | Adversarial stratified sampling, and `SampledSource` to restrict any source to a sample. |
| `repo.py` | `TargetRepo` protocol and the in-memory implementation, which enforces the real constraints of the shipped schemas. |
| `models.py` | SQLAlchemy models mirroring the target tables, plus the new `geo_id_regime_alias`. |
| `db_repo.py` | `TargetRepo` over three real databases, with identical semantics to the in-memory one. |
| `pipeline.py` | `import_fields()` then `import_profiles()`. Idempotent, resumable, order-enforcing. |
| `run.py` | CLI and reporting. |

## Sampling

A random sample of a few thousand rows out of tens of thousands will contain none
of the cases that break an import, and will produce a clean result that means
nothing. `build_sample()` selects *for* the hazards, in priority order, so that a
budget smaller than the union of all strata keeps them and drops filler rather
than the reverse. It never backfills arbitrary rows after cutting a stratum.

The most valuable stratum is near-identical polygons held by *different* users,
because that is what produces one v2 GeoID with two owners. Finding it looks like
it should require covering every field first — but AR 1.0's own L13 GeoID is
already a blocking key, so the clusters fall out of a group-by on a column that is
already there, at no geometric cost. The same grouping yields the AR 1.0 collision
count.

Orphan references are reported rather than sampled: an orphan has no AR 1.0 row,
so it cannot be selected as a field. The profile holding it is selected instead,
which is what exercises the join failure.

## Implementing a source

`Ar1TerraPipeSource` has three unimplemented methods. Everything downstream is
written against the `LegacySource` protocol, so no other module changes when they
are filled in.

| Method | Returns | Notes |
|---|---|---|
| `inventory()` | `Inventory` | Must include `COUNT(*)` and `COUNT(DISTINCT <l13 column>)`; the difference is the AR 1.0 collision count. |
| `iter_fields()` | `LegacyField` | Yield rows with `wkt=None` rather than skipping them — the pipeline counts and quarantines them, and that count is a finding. |
| `iter_profiles()` | `LegacyProfile` | A TerraPipe user and the v1 GeoIDs attached to their profile. |

Both connections must be **read-only**; AR 1.0 and TerraPipe are live systems.

Populate `LegacyField.v1_l13_geo_id` from AR 1.0's L13 column. Sampling degrades
to nothing without it — near-duplicate detection has no blocking key and falls
back to treating every field as its own cluster.

Set `LegacyField.v1_kind` from the source columns wherever possible.
`classify_v1_id()` infers it from the identifier's shape and cannot distinguish
an L13 hash from an L20 hash, since both are 64 hex characters.

## Implementing a source

`Ar1TerraPipeSource` has three unimplemented methods. Everything downstream is
written against the `LegacySource` protocol, so no other module changes when they
are filled in.

| Method | Returns | Notes |
|---|---|---|
| `inventory()` | `Inventory` | Must include `COUNT(*)` and `COUNT(DISTINCT <l13 column>)`; the difference is the AR 1.0 collision count. |
| `iter_fields()` | `LegacyField` | Yield rows with `wkt=None` rather than skipping them — the pipeline counts and quarantines them, and that count is a finding. |
| `iter_profiles()` | `LegacyProfile` | A TerraPipe user and the v1 GeoIDs attached to their profile. |

Both connections must be **read-only**; AR 1.0 and TerraPipe are live systems.

Set `LegacyField.v1_kind` from the source columns wherever possible.
`classify_v1_id()` infers it from the identifier's shape and cannot distinguish
an L13 hash from an L20 hash, since both are 64 hex characters.

## Schema requirement

The import needs a table that does not exist yet. `GeoIDAlias` in `ar2` maps
`(canonical_geo_id, alias_content_hash, relation)` — a *content hash* of
resubmitted WKT to a canonical GeoID, for deduplicating resubmissions. There is
no column anywhere recording that a v1 identifier is now a given v2 identifier,
and that mapping is the join key between the two sources, because TerraPipe's
profile rows reference v1 GeoIDs.

It is declared in `models.py` and created by the runner. Two constraints on it
are easy to get wrong:

- `v1_geo_id` is `String(128)`, not 64. v1 identifiers include 36-character
  UUIDs from the fallback cascade alongside 64-character hashes.
- The table must resolve **permanently**. AR 1.0 issued these identifiers to real
  users over several years, and they persist in TerraPipe, in exports, and in
  anything that was printed.

Two further additive tables come with it: `geo_id_blocking_cell`, a normalised
L13 index (AR2 keeps the cover in `geo_ids.s2_cells` behind a GIN index, which is
fast on Postgres but not expressible on SQLite), and `geo_id_parent_edge` for
nesting. Nothing outside the import reads either.

`models.py` mirrors the shipped schemas rather than importing them, so this
package runs without AR2's app config, the hub's settings or a Pancake service
import. `tests/test_schema_drift.py` parses the real model files and fails if a
mirrored table or a constraint the import depends on has changed. It skips when
the sibling checkouts are absent, so it only really guards in CI.

## Design notes

**Two phases, and the order is enforced.** A ListID is a Merkle root over sorted
GeoIDs, so importing ownership before geometry produces ListIDs that are wrong
but structurally valid. `import_profiles()` raises `OutOfOrder` if no aliases
exist rather than allowing it.

**`child_of` registers; it does not alias.** A plot inside a field is a distinct,
separately-owned field (P6). Aliasing a nested field to its parent would erase it
and transfer its owner's grant scope to the parent's owner. Nested fields receive
their own identity and a parent edge.

**Resolution is area-exact, not cell-count based.** Covers are normalized and
therefore multi-level, so comparing them as token sets fails twice over: token
equality cannot see ancestor/descendant overlap at all — one cell and its own
four children share no token while covering the identical region — and cell
counts stop being proportional to area. Intersection uses cell-union semantics
and both sides are weighted by `4 ** (30 - level)`.

**Leaf area is computed arithmetically.** These bindings expose no
`S2CellUnion::LeafCellsCovered()`. `4 ** (30 - level)` is exact integer
arithmetic, not an approximation.

**Degenerate geometry is refused, never assigned a surrogate identity.**
`GeometryUnusable` is raised for empty, zero-area and non-polygonal input; the
pipeline quarantines those rows and reports them by reason.

**Dry-run is the same code path with writes suppressed**, rather than a separate
script, so what is exercised is what will run.

**Threshold sensitivity is quantised by cell size on small fields.** An L20 cell
is roughly 8.1 m across, so on a field of a couple of hundred metres any offset
small enough to reach 95% IoU is smaller than one cell and the two covers come
out identical — they then merge at every threshold setting.

**Boundary bias is small, and over-reports rather than under-reports.** A cell on
the boundary is counted for both shapes, so cell IoU sits slightly above the true
geometric IoU. Measured at a geometric 0.9048: 0.9231 on a 200 m field, 0.9216 at
400 m, 0.9055 at 1000 m, 0.9051 at 2000 m. A 95% threshold therefore fires just
under 95% true overlap, by under two points on smallholder plots and by nothing
measurable above a kilometre — small enough that no centre-in-polygon correction
is warranted. Quantisation, above, is the effect that actually constrains tuning.

## Fixtures

`FixtureSource` is adversarial rather than representative. It carries UUID-fallback
and L20-fallback identifiers, exact-duplicate geometry, a nested plot, missing and
unparseable geometry, a polygon with a hole, a multi-part field, orphaned profile
references, unclaimed polygons, profiles missing an email or a phone, two
profiles sharing a phone number, and two different profiles holding near-identical
polygons.

The last of these is the case where a merge produces one GeoID with two owners.

## Test coverage

78 tests. Run `pytest migration/tests -q -rs`; any skip means a checkout is
missing and a guard is not actually running.

`tests/test_primitive.py` (17) — determinism, token sorting, absence of un-merged
siblings, holes, multi-part geometry, part ordering, winding order, duplicate
vertices, the L13 collision fix, shape versus bounding box, refusal on degenerate
input, IoU tracking geometry, ancestor/descendant overlap, IoU invariants,
nesting, and the blocking key.

`tests/test_pipeline.py` (19) — inventory self-consistency, aliasing, permanent v1
resolution, UUID promotion, quarantine, duplicate-content aliasing, nesting,
idempotency, resume after interruption, dry run writing nothing, phase ordering,
hub constraint rejection, orphaned joins, per-user field-set equality, shared
ownership after merge, threshold sensitivity, and ListID correctness.

`tests/test_sample.py` (14) — cluster detection from the L13 key alone,
separation of multi-owner from same-owner clusters, presence of every hazard
stratum, survival of hazards under a budget far smaller than the source, refusal
to backfill after a cut, orphan reporting, profile follow-through, determinism,
and the collision count.

`tests/test_db_repo.py` (20) — round-tripping through the database, each unique
and not-null constraint by name, the blocking index, alias idempotency and refusal
to remap, parent edges, the Pancake mirror, migrated accounts being inactive with
no usable password, field-list ownership and idempotency, multi-owner detection
across the join, checkpointing, resumability, and — the one that matters most —
that the in-memory and database repositories produce identical reports, without
which the dry run is not a rehearsal of anything.

`tests/test_schema_drift.py` (8) — mirrored columns against the real model files,
and the specific constraints the import's rejection logic depends on.
