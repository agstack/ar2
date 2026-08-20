-- Schema changes for the trace-back hops and the v2 GeoID regime.
--
-- WHY THIS FILE EXISTS. AR2 creates its schema with Base.metadata.create_all,
-- which adds missing TABLES but never missing COLUMNS. A database that already
-- has list_artifact therefore keeps the old shape and every trace-back query
-- fails on the missing column -- while CI stays green, because CI starts from an
-- empty database on every run. There is no Alembic in this repo, so the upgrade
-- is explicit.
--
--   psql "$DATABASE_URL" -f scripts/schema_upgrade_20260814.sql
--
-- Idempotent and additive: safe to run repeatedly, and safe to run BEFORE
-- deploying the new code, since every column is nullable and the old code
-- ignores all of it. Run it on the node database and, if the hub shares a
-- schema, there too.

BEGIN;

-- ---------------------------------------------------------------------------
-- trace-back hops
-- ---------------------------------------------------------------------------

-- Where this lot was created. A GeoID, so a packhouse is identified exactly as
-- a field is, and facility locations need no separate registry. Nullable on
-- purpose: an unrecorded location is a real state, and the trace-back reports
-- the gap rather than refusing to answer.
ALTER TABLE list_artifact
    ADD COLUMN IF NOT EXISTS location_geo_id VARCHAR;

-- Which event created this lot: initial_pack, first_land_based_receiving,
-- transformation.
ALTER TABLE list_artifact
    ADD COLUMN IF NOT EXISTS event_type VARCHAR(32);

-- Trace-back reads locations per hop, and reverse lookups filter on them.
CREATE INDEX IF NOT EXISTS ix_list_artifact_location_geo_id
    ON list_artifact (location_geo_id);

-- ---------------------------------------------------------------------------
-- v1 -> v2 identifier mapping
-- ---------------------------------------------------------------------------

-- create_all WOULD create this table, since it is new. It is spelled out anyway
-- so that one command brings any database fully up to date, and so the column
-- widths are reviewable: v1_geo_id is 128 rather than 64 because v1 identifiers
-- include the 36-character uuid4() values the old collision cascade minted.
--
-- Read by _equivalence_set in both directions. Without it, a trace seeded with
-- an identifier AR 1.0 issued years ago returns an empty result for a field that
-- is present under its v2 identifier.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'regime_relation_enum') THEN
        CREATE TYPE regime_relation_enum AS ENUM ('same_as', 'child_of');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS geo_id_regime_alias (
    id          UUID PRIMARY KEY,
    v1_geo_id   VARCHAR(128) NOT NULL UNIQUE,
    v2_geo_id   VARCHAR(64)  NOT NULL REFERENCES geo_ids (geo_id) ON DELETE CASCADE,
    v1_kind     VARCHAR(16),
    v1_regime   VARCHAR(8) DEFAULT 'v1',
    v2_regime   VARCHAR(8) DEFAULT 'v2',
    relation    regime_relation_enum NOT NULL DEFAULT 'same_as',
    created_at  TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_geo_id_regime_alias_v1_geo_id
    ON geo_id_regime_alias (v1_geo_id);
CREATE INDEX IF NOT EXISTS ix_geo_id_regime_alias_v2_geo_id
    ON geo_id_regime_alias (v2_geo_id);

COMMIT;

-- ---------------------------------------------------------------------------
-- NOT DONE HERE: existing rows keep their v1 identifiers.
--
-- Registrations made before this change carry L13-derived, L20-derived or uuid4
-- identifiers. Nothing in this script rewrites them, and nothing should: those
-- identifiers are in circulation. They become reachable by loading
-- geo_id_regime_alias, which is the import pipeline's job, not a DDL step.
-- Until that runs, v1 rows stay queryable by their own identifiers and simply
-- have no v2 identity yet.
-- ---------------------------------------------------------------------------
