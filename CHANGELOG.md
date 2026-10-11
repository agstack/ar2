# Changelog

## [Unreleased]

### Changed
- **The reverse lot index carries the same gate as trace-forward**: `GET /list-artifact/reverse/{geoid}`
  answered "which lots is this field in" for any hub login, with no grant and no audit entry;
  measured live, that was five of the six lots the gated `/traceforward` returns (review
  ledger AG-015). It now requires a grant for the seed or an authority credential with a
  scope, exactly as `/traceforward` does, and an authority answer is written to the MEAL
  ledger before it is returned. Callers holding only a login receive 403. Pancake's audit
  route, which calls this index, already forwards the caller's authority token.
- `docs/MCP_SURFACE.md` gating note for `get_list_artifact_reverse`; the MCP tool takes
  `grant_token`, `authority_token` and `scope`. README endpoint table corrected: it had
  described a gate the code did not have.

### Added
- `app/tests/testkit/grants.py`: one shared minter for field-access grants, replacing the
  copy in `test_traceforward.py`. Six direct tests of the reverse index, including the
  control that a bare login is refused and the proof that the same check can fire.

## [v0.9-review]

### Added
- **Plots declared by a coordinate, as peers of boundaries**: `/register-point` takes
  `declared_area_ha` (capped at four hectares, Regulation (EU) 2023/1115 Art. 2(28));
  a point is named by a one-cell cover of its leaf cell and a re-survey within 10 m
  resolves onto the existing plot, the counterpart of IoU resolution for boundaries.
  Point features in a GeoJSON upload take the same path rather than a lesser one.
  `GeometryKind` and `AreaHa` are published at L0 and L1, and `/geoid/{id}/eudr-export`
  files a Point with its `Area` — or refuses, because the EU schema rejects a Point
  without one. See "Two kinds of plot" in the README.
- **JWT Identity & Routing (Hub Gateway)**: Requests are now proxied through the `ar2-hub`, establishing identity via JWKS validation for write paths.
- **Grant-Based Authorization (L1 Data)**: High-resolution field boundaries and EUDR exports strictly require a valid ODRL Verifiable Credential (`SD-JWT` format) signed by the trusted issuer.
- **Anonymous L0 Access**: Data exists publically at a masked level (S2 indexing/Centroid bounding box limits) to preserve fundamental data utility without violating owner privacy.
- **Revocation Checking**: Full integration with W3C `StatusList2021` to allow Data Owners to instantly revoke previously issued Buyer credentials, demoting their access back to L0.
- **EUDR Compliance Export**: Dedicated `/geoid/{id}/eudr-export` endpoint generating EUDR-profile GeoJSONs for supply chain due diligence.
- **Zero-to-Hero Testing Framework**: Complete Jupyter Notebook (`dpi_demo_e2e.ipynb`), Bash script (`demo_e2e.sh`), and markdown guides validating the end-to-end multi-persona architecture.
- **SQLite Fallback Removed**: Repositories now default to local PostgreSQL.
