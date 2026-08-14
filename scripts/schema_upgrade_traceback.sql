-- Adds the two columns the hop-structured trace-back needs.
--
-- Required because AR2 creates its schema with Base.metadata.create_all, which
-- adds missing TABLES but never missing COLUMNS. A database that already has
-- list_artifact therefore keeps the old shape, and every trace-back query fails
-- on the missing column -- while CI stays green, because CI starts from an empty
-- database on every run. Run this once against any environment that predates the
-- change.
--
--   psql "$DATABASE_URL" -f scripts/schema_upgrade_traceback.sql
--
-- Idempotent and additive: safe to run repeatedly, and safe to run before the
-- new code is deployed, since both columns are nullable and the old code ignores
-- them.

BEGIN;

-- Where this lot was created: the FSMA 204 traceability lot code source
-- (21 CFR 1.1330(a)(14), 1.1350(a)(2)(ii)). A GeoID, so a packhouse is
-- identified exactly as a field is.
ALTER TABLE list_artifact
    ADD COLUMN IF NOT EXISTS location_geo_id VARCHAR;

-- Which of the 21 CFR 1.1320(a) events created this lot: initial_pack,
-- first_land_based_receiving, transformation.
ALTER TABLE list_artifact
    ADD COLUMN IF NOT EXISTS event_type VARCHAR(32);

-- Trace-back reads locations per hop, and reverse lookups filter on them.
CREATE INDEX IF NOT EXISTS ix_list_artifact_location_geo_id
    ON list_artifact (location_geo_id);

COMMIT;
