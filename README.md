# Asset Registry Node (ar2) - v0.9-review

The Asset Registry Node (`ar2`) acts as the **Data Holder and Verifier** in the AgStack Digital Public Infrastructure (DPI) architecture. 

It stores high-resolution field boundary geometries and verifies field access credentials (ODRL Grants) before exposing sensitive data. It strictly adheres to two fundamental design rules:
1. **The Hub routes but never authorizes**: The `ar2-hub` gateway forwards requests, but the Node is the ultimate authority that verifies the cryptographic grant.
2. **L1 requires a grant credential**: High-resolution spatial data (Level 1 Masking) is never exposed without a valid ODRL JWT credential. The owner of the field is automatically the first grantee.

### Key Features
* **Identity Resolution Engine**: Prevents duplicated fields in the global registry. If a farm is registered twice (overlapping by a configurable `threshold`, defaulting to 95%), the node seamlessly aliases the new registration to the existing `Geo Id`. Furthermore, it supports hierarchical **`child_of`** relationship mapping for sub-plots completely contained within a larger farm boundary.
* **Trace-Back and Trace-Forward**: Content-derived **ListArtifacts** (Merkle-root `ListID`s over GeoIDs, nested lists, and regions) and **RegionArtifacts** (`RegionID`s over an S2 cover, from WKT or member fields) turn the registry into a recall graph. Neither direction is open: the **node** verifies the caller's credential on every query. Trace-back (product to fields) requires a grant for the product being traced; **trace-forward** (a contaminated field to every downstream product) additionally requires a Hub-issued capability and seed authorization, and revealing downstream *identities* requires an accredited authority credential and is audited. See [TRACEABILITY.md](./TRACEABILITY.md) for the full reasoning, a worked cyclospora recall example, and a contrast with existing traceability platforms.

## Architecture

Please see [ARCHITECTURE.md](./ARCHITECTURE.md) for a detailed diagram of the Hub-Node-Pancake flow and a deeper dive into the DPI Trust architecture. For the recall/traceability model built on top of it, see [TRACEABILITY.md](./TRACEABILITY.md).

## Quickstart (10 Minutes to Run)

Follow these steps to run the Node locally alongside the `ar2-hub` and `Pancake` issuer.

1. **Create and activate a virtual environment**:
   ```bash
   python3 -m venv ar-env
   source ar-env/bin/activate
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables**:
   Copy the example environment file and update variables if necessary.
   ```bash
   cp .env.example .env
   ```

4. **Run the Uvicorn Server**:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
   ```

*(Note: The `ar2-hub` gateway proxy should be run on a separate port, e.g., 8000, and this Node should typically be bound to an internal port like 8001 or shielded from public access).*

## Environment Variables Reference

| Variable | Description | Default | Demo-Only? |
|----------|-------------|---------|------------|
| `DATABASE_URL` | PostgreSQL connection string. Defaults to local postgres if omitted. | `postgresql://postgres:postgres@localhost:5432/postgres` | No |
| `world_shp_file_PATH` | Path to the `.shp` file used for point-in-polygon country resolution. | *None* | No |
| `JWKS_URL` | URL to fetch the `ar2-hub` JSON Web Key Set for L0 access token validation. | `http://127.0.0.1:8000/.well-known/jwks.json` | No |


## Endpoints Overview

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| `POST` | `/register-field-boundary` | Registers a single WKT geometry. | Yes (Hub JWT) |
| `POST` | `/register-field-boundaries-geojson` | Bulk registers from a GeoJSON feature collection. | Yes (Hub JWT) |
| `POST` | `/register-points-geojson` | Bulk registers points. | Yes (Hub JWT) |
| `GET`  | `/resolve/{geoid}` | Returns L0 masked data (S2 indices / Bounding Box) for a GeoID. | No |
| `GET`  | `/fetch-field-wkt/{geoid}` | Returns L1 high-resolution geometry. | Yes (Grant) |
| `GET`  | `/fetch-field-centroid/{geoid}` | Returns exact centroid of a field. | Yes (Grant) |
| `GET`  | `/geoid/{geoid}/eudr-export` | Exports an EUDR-compliant GeoJSON artifact. | Yes (Grant) |
| `POST` | `/list-artifact` | Registers a content-derived list (a lot/case/pallet/retail item) over GeoIDs, nested lists (`L:`), or regions (`R:`). Append-only. | Yes (Hub JWT) |
| `POST` | `/region-artifact` | Registers a content-derived region from a WKT boundary (no acreage cap) or from member GeoIDs/lists. | Yes (Hub JWT) |
| `GET`  | `/list-artifact/{list_id}` | Returns the members of a list artifact. | Yes (Hub JWT) |
| `GET`  | `/list-artifact/reverse/{geoid}` | Trace-forward: every list/region containing a GeoID, climbed recursively to terminal products. | Yes (Hub JWT + `trace-forward` capability + seed authorization; accredited authority credential for the identity tier) |

See [TRACEABILITY.md](./TRACEABILITY.md) for how these endpoints compose into trace-back and trace-forward.

## Testing

A comprehensive API guide for interacting with the Node is provided in [app/test_curls.txt](./app/test_curls.txt).
To run the automated test suite (which overrides the JWKS hub auth for isolation):
```bash
python -m pytest app/tests/test_api.py -v
```

## Automated End-to-End Demos

There are two separate end-to-end demo scripts, each covering a different capability. Both require the Hub and Pancake servers to be running locally.

1. **Grant Lifecycle (`./demo_e2e.sh`)**
   Covers the full lifecycle of a standard field grant (registration, issuance, retrieving L1 geometry, verification, and revocation).

2. **Trace-forward (`./scripts/e2e_traceforward.sh`)**
   Covers the trace-forward recursive graph capability. It registers fields, builds a supply chain list structure, issues an authority credential, and then performs trace-forward queries to demonstrate Tier 1 vs Tier 3 identity disclosure rules, including negative assertions for missing or revoked authority.
