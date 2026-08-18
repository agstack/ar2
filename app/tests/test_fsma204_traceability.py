"""FSMA 204 end-to-end traceability scenario: romaine lettuce, field to retail.

This file does two jobs. It is a regression harness for the trace-back and
trace-forward paths, and it is the demonstration scenario for the International
Fresh Produce Association -- so every assertion is written against a clause of
the FDA Food Traceability Rule (21 CFR part 1, subpart S) rather than against our
own internal expectations. Run with `-s` to print the investigator's-eye
walkthrough.

WHY ROMAINE. Leafy greens are on the Food Traceability List, and the 2018 E.coli
outbreaks are the case FSMA 204 was written to prevent. The scenario below is the
shape that made those outbreaks expensive: two growers in different regions feed
one processor, the processor blends their material into a single shred lot, and
from that point on every downstream case contains material from every field. A
recall that cannot distinguish which fields fed which lots has to take all of it.

HOW THE RULE MAPS ONTO AR2 AND PANCAKE

  21 CFR clause                          construct
  -------------------------------------  ------------------------------------
  1.1315(a)(5)(i)  farm map, each field  AR2 registry: GeoID + WKT. The rule
                   with "geographic      asks for a map with coordinates; the
                   coordinates"          registry IS that map, machine-readable.
  1.1325(a)(1)(v)  harvest location "at  GeoID. The rule's own fallback clause
  1.1330(a)(5)     least as precisely    -- "or other information identifying
                   as the field ... name" the harvest location at least as
                                          precisely" -- is the slot a GeoID
                                          fills, and it fills it better than a
                                          grower-chosen name, which 1.1325
                                          admits must "correspond to the name
                                          used by the grower" and therefore
                                          requires asking the grower.
  1.1320(a)        new lot code assigned  A new ListArtifact. One lot, one hop.
                   at initial packing,
                   first land-based
                   receiving, or
                   transformation
  1.1330(a)(14)    location description   ListArtifact.location_geo_id -- the
  1.1350(a)(2)(ii) for the lot code       hop's own location, itself a GeoID, so
                   source                 a packhouse is identified exactly as a
                                          field is and needs no second registry.
  1.1350(a)(1)     new lot linked to      ListParentEdge. The ListID is a Merkle
                   each input lot         root over its members, so a lot code
                                          COMMITS to its inputs and any party
                                          can verify the link arithmetically.
  1.1340 / 1.1345  shipping / receiving   No new lot code (1.1320(b)), so no new
                   keep the same lot      artifact. Access travels as a Pancake
                                          grant instead.
  1.1455(c)(1)     records in 24 hours,   One API call. And critically: the rule
                   plus "information      requires a glossary because every firm
                   needed to understand   uses private lot codes. GeoIDs and
                   these records, such    ListIDs are content-derived, so they
                   as internal or         need no glossary -- anyone holding the
                   external coding        boundary recomputes the identifier and
                   systems, glossaries"   gets the same answer.

WHAT THIS BUYS OVER THE STATUS QUO. The rule permits private field names and
private lot codes, then asks for a glossary so FDA can reconcile them. That
reconciliation across dozens of firms is why traces take weeks. Content-derived
identifiers remove the reconciliation step: two firms that never spoke derive the
same GeoID for the same field, and a ListID can be recomputed from its members to
prove a lot code is authentic and complete.

WHAT IT DOES NOT DO. AR2 records identity and structure, not the commercial KDEs
-- quantities, dates, reference document numbers, product descriptions. Those
live with the firm. The claim is narrower and stronger than "FSMA compliance in a
box": AR2 supplies the two identifiers the rule needs to be verifiable, and the
link structure between them.
"""

import itertools
import os
import uuid
from pathlib import Path

import pytest
import s2sphere as s2
from fastapi.testclient import TestClient

TESTKIT = os.path.join(os.path.dirname(__file__), "testkit", "dev_keys")

# Set before app import: both accessors are lru_cached, so a None on first call
# would be cached for the session. Set here rather than relied upon from
# test_api.py, so this file passes when run alone -- which is how it will be run
# when it is used as a demonstration.
os.environ["AR_TRUSTED_ISSUER_PUBKEY"] = os.path.join(TESTKIT, "dev_issuer_public.pem")
os.environ["AR_TRUSTED_AUTHORITY_PUBKEY"] = os.path.join(TESTKIT, "authority_issuer_public.pem")


@pytest.fixture(autouse=True)
def set_test_status_list_dir(monkeypatch):
    monkeypatch.setenv("TEST_STATUS_LIST_DIR", TESTKIT)


@pytest.fixture(autouse=True)
def meal():
    """Capture MEAL audit packets instead of shipping them to Pancake.

    Both trace directions fail closed when the audit append fails: an
    unrecordable regulatory search must not succeed silently. That is deliberate,
    so the scenario stubs the transport and asserts on the packets rather than
    disabling the audit.
    """
    from unittest.mock import patch
    packets = []
    with patch("app.meal_logger._append_to_meal_chain", side_effect=packets.append):
        yield packets


from app.auth import require_hub_user
from app.database import SessionLocal
from app.main import app
from app.merkle import canonical_members, merkle_root
from app.models.geo_id_model import GeoID

app.dependency_overrides[require_hub_user] = lambda: {
    "sub": "investigator@fda.test",
    "capabilities": ["trace-forward"],
}

client = TestClient(app)

DEV_KEYS = Path(__file__).parent / "testkit" / "dev_keys"


def _authority() -> dict:
    """A food-safety authority credential. The regulator's path, per Gate B."""
    return {"X-Authority-Token": (DEV_KEYS / "valid_authority.sdjwt").read_text().strip()}


def _grant_for(geoid: str) -> dict:
    """A field-access grant: the grower's or receiver's own view of their field.

    Trace-forward with a grant stays at Tier 1 -- lot codes and counts, no holder
    identities -- which is the disclosure level a commercial partner gets. The
    regulator's authority credential escalates to Tier 3.
    """
    import base64
    import hashlib
    import json
    import secrets
    import time

    import jwt

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    salt = b64(secrets.token_bytes(16))
    disclosure = b64(json.dumps([salt, "fields.0", geoid]).encode("utf-8"))
    claims = {
        "iss": "did:web:pancake.test",
        "sub": "grower@demo.com",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "vct": "agstack.org/credentials/field-access-grant/v1",
        "status": {"status_list": {"uri": "http://localhost:8100/grants/status-list", "idx": 1}},
        "_sd": [b64(hashlib.sha256(disclosure.encode("ascii")).digest())],
        "_sd_alg": "sha-256",
    }
    token = jwt.encode(
        claims,
        (DEV_KEYS / "dev_issuer_private.pem").read_bytes(),
        algorithm="EdDSA",
        headers={"typ": "vc+sd-jwt", "kid": "pancake-test-1"},
    )
    return {"X-Grant-Token": f"{token}~{disclosure}~"}


def _recall_scope(geoid: str) -> set[str]:
    """Every lot that must be recalled if this field is contaminated."""
    r = client.post("/traceforward",
                    json={"seed_geoid": geoid, "scope": "demo-recall"},
                    headers=_grant_for(geoid))
    assert r.status_code == 200, r.text
    return set(r.json()["list_ids"])


# S2 cells are shared state. /traceforward resolves region containment by
# probing a seed's cells and every ancestor against RegionCoverCell, so any test
# that registers a region makes its cells visible to every other test in the
# session. Reusing a token another test uses silently adds matches to that test's
# results -- which is a false positive in a recall. Every place therefore gets
# its own leaf cell, from a counter that no other test draws on.
_place_seq = itertools.count()

# Far from the tokens the other suites hardcode ("89c259", "111", "3f1a9f0f").
_ORIGIN_LAT, _ORIGIN_LNG = 36.6002, -121.8947      # Salinas Valley
_CELL_LEVEL = 20                                    # ~10 m, so 0.002 deg cannot collide


def _fresh_cell() -> str:
    """A leaf cell no other place in this session has used."""
    n = next(_place_seq)
    lat = _ORIGIN_LAT + (n // 32) * 0.002
    lng = _ORIGIN_LNG + (n % 32) * 0.002
    cell = s2.CellId.from_lat_lng(s2.LatLng.from_degrees(lat, lng)).parent(_CELL_LEVEL)
    return cell.to_token()


def _register_place(label: str, cells: list[str], boundary_type: str) -> str:
    """Register a field or a facility. Both are GeoIDs; only the type differs.

    That sameness is the point: 1.1330(a)(14) wants a location for the packhouse
    and 1.1325(a)(1)(v) wants one for the field, and one namespace answers both.
    """
    db = SessionLocal()
    geo_id = f"urn:agstack:geoid:FSMA_{label}_{uuid.uuid4().hex[:8]}"
    db.add(GeoID(
        geo_id=geo_id,
        geo_id_short=f"S{uuid.uuid4().hex[:15]}",
        content_hash=uuid.uuid4().hex,
        field_name=label,
        boundary_type=boundary_type,
        s2_cells=cells,
        area_ha_approx=4.0,
    ))
    db.commit()
    db.close()
    return geo_id


def _create_lot(members: list[str], *, at: str | None, event: str) -> str:
    """One lot code, per 1.1320(a). `at` is the lot code source location."""
    body = {"members": members, "event_type": event}
    if at is not None:
        body["location_geo_id"] = at
    r = client.post("/list-artifact", json=body)
    assert r.status_code == 200, r.text
    return r.json()["list_id"]


class Chain:
    """The romaine supply chain, built through the public API only.

    Salinas fields A and B  ->  LOT_PACK_1  (initial packing, Packhouse 1)
    Yuma field C            ->  LOT_PACK_2  (initial packing, Packhouse 2)
    both pack lots          ->  LOT_SHRED   (transformation, Processor)
    the shred lot           ->  LOT_KIT     (transformation, Kitting plant)
    """

    def __init__(self, record_locations: bool = True):
        loc = (lambda x: x) if record_locations else (lambda _: None)

        # 1.1315(a)(5)(i): the farm map. Three fields, two growers, two regions.
        self.field_a = _register_place("SalinasFieldA", [_fresh_cell()], "field")
        self.field_b = _register_place("SalinasFieldB", [_fresh_cell()], "field")
        self.field_c = _register_place("YumaFieldC", [_fresh_cell()], "field")

        # Facilities are GeoIDs too -- 1.1330(a)(14), 1.1350(a)(2)(ii).
        self.packhouse_1 = _register_place("Packhouse1", [_fresh_cell()], "facility")
        self.packhouse_2 = _register_place("Packhouse2", [_fresh_cell()], "facility")
        self.processor = _register_place("Processor", [_fresh_cell()], "facility")
        self.kitting = _register_place("KittingPlant", [_fresh_cell()], "facility")

        # 1.1320(a): a lot code at initial packing.
        self.lot_pack_1 = _create_lot(
            [self.field_a, self.field_b], at=loc(self.packhouse_1), event="initial_pack")
        self.lot_pack_2 = _create_lot(
            [self.field_c], at=loc(self.packhouse_2), event="initial_pack")

        # 1.1320(a) + 1.1350(a)(1): transformation makes a new lot, linked to
        # every input lot. This is the blending event that spreads contamination.
        self.lot_shred = _create_lot(
            [f"L:{self.lot_pack_1}", f"L:{self.lot_pack_2}"],
            at=loc(self.processor), event="transformation")

        self.lot_kit = _create_lot(
            [f"L:{self.lot_shred}"], at=loc(self.kitting), event="transformation")

    @property
    def all_fields(self) -> set[str]:
        return {self.field_a, self.field_b, self.field_c}


@pytest.fixture
def chain():
    return Chain()


# ==========================================================================
# TRACE-BACK. The FDA holds a retail case and asks where it came from.
# ==========================================================================

def traceback(list_id: str, **params):
    r = client.get(f"/list-artifact/{list_id}/traceback",
                   params=params, headers=_authority())
    assert r.status_code == 200, r.text
    return r.json()


def test_traceback_reaches_every_field_from_the_retail_case(chain):
    """The FSMA answer: which fields could have contaminated this case?"""
    out = traceback(chain.lot_kit)
    assert set(out["geoids"]) == chain.all_fields
    assert out["truncated"] is False


def test_traceback_reports_one_hop_per_lot(chain):
    """1.1320(a) creates a lot at each packing and transformation, so the trace
    must show four lots, not a flat set and not a count of depth levels."""
    out = traceback(chain.lot_kit)
    assert [h["list_id"] for h in out["hops"]] != []
    assert {h["list_id"] for h in out["hops"]} == {
        chain.lot_kit, chain.lot_shred, chain.lot_pack_1, chain.lot_pack_2
    }
    # exactly one hop per lot -- no lot reported twice, even though pack_1 and
    # pack_2 are both reachable only through the shred lot
    assert len(out["hops"]) == 4


def test_each_hop_carries_the_location_where_that_lot_was_created(chain):
    """1.1330(a)(14) and 1.1350(a)(2)(ii): the lot code source, per hop."""
    out = traceback(chain.lot_kit)
    by_lot = {h["list_id"]: h for h in out["hops"]}

    assert by_lot[chain.lot_kit]["location_geo_id"] == chain.kitting
    assert by_lot[chain.lot_shred]["location_geo_id"] == chain.processor
    assert by_lot[chain.lot_pack_1]["location_geo_id"] == chain.packhouse_1
    assert by_lot[chain.lot_pack_2]["location_geo_id"] == chain.packhouse_2

    assert all(h["location_recorded"] for h in out["hops"])
    assert out["hops_missing_location"] == []


def test_the_hop_order_is_the_supply_chain_order(chain):
    """Depth increases away from the point of sale, so an investigator reads the
    chain in the direction the food travelled, reversed."""
    out = traceback(chain.lot_kit)
    depth = {h["list_id"]: h["depth"] for h in out["hops"]}
    assert depth[chain.lot_kit] == 0
    assert depth[chain.lot_shred] == 1
    assert depth[chain.lot_pack_1] == depth[chain.lot_pack_2] == 2
    assert out["max_depth"] == 2


def test_each_hop_names_the_lots_that_fed_it(chain):
    """1.1350(a)(1): the new lot must be linked to each input lot."""
    out = traceback(chain.lot_kit)
    by_lot = {h["list_id"]: h for h in out["hops"]}

    assert by_lot[chain.lot_kit]["input_list_ids"] == [chain.lot_shred]
    assert set(by_lot[chain.lot_shred]["input_list_ids"]) == {
        chain.lot_pack_1, chain.lot_pack_2}
    # the packing lots consume fields, not lots -- they are the origin
    assert by_lot[chain.lot_pack_1]["input_list_ids"] == []
    assert set(by_lot[chain.lot_pack_1]["geoids"]) == {chain.field_a, chain.field_b}
    assert by_lot[chain.lot_pack_2]["geoids"] == [chain.field_c]


def test_the_edge_list_reconstructs_the_whole_graph(chain):
    """Structure an investigator can load into anything, including a spreadsheet
    -- 1.1455(c) contemplates an electronic sortable export."""
    out = traceback(chain.lot_kit)
    edges = {(e["parent_list_id"], e["child_id"], e["kind"]) for e in out["edges"]}

    assert (chain.lot_kit, chain.lot_shred, "list") in edges
    assert (chain.lot_shred, chain.lot_pack_1, "list") in edges
    assert (chain.lot_shred, chain.lot_pack_2, "list") in edges
    assert (chain.lot_pack_1, chain.field_a, "geoid") in edges
    assert (chain.lot_pack_2, chain.field_c, "geoid") in edges


def test_a_partial_trace_from_the_middle_of_the_chain(chain):
    """Tracing from the shred lot must not reach back past it, and must not
    invent the kit lot -- a processor may only see its own inputs."""
    out = traceback(chain.lot_shred)
    assert set(out["geoids"]) == chain.all_fields
    assert chain.lot_kit not in {h["list_id"] for h in out["hops"]}
    assert out["max_depth"] == 1


# ==========================================================================
# TRACE-FORWARD. Contamination is found in one field; what must be recalled?
# ==========================================================================

def test_contamination_in_one_field_reaches_every_downstream_lot(chain):
    """The 2018 shape: one Yuma field, and the blend carries it to retail."""
    reached = _recall_scope(chain.field_c)

    assert chain.lot_pack_2 in reached      # its own packing lot
    assert chain.lot_shred in reached       # the blend at the processor
    assert chain.lot_kit in reached         # the retail kit
    # and not the lot that never contained its material
    assert chain.lot_pack_1 not in reached


def test_the_recall_scope_stops_where_the_material_stops(chain):
    """Field A's material never entered pack lot 2, so a Field A recall must not
    sweep it in. This is the commercial argument: precision limits scope, and
    scope is cost."""
    reached = _recall_scope(chain.field_a)
    assert chain.lot_pack_1 in reached
    assert chain.lot_shred in reached
    assert chain.lot_kit in reached
    assert chain.lot_pack_2 not in reached


def test_traceforward_and_traceback_agree(chain):
    """Round trip. Every lot trace-forward reports from a field must, traced
    back, contain that field. Disagreement between the two directions is the
    defect class that makes a traceability system useless in an investigation,
    because the two answers would license different recalls."""
    scope = _recall_scope(chain.field_c)
    assert len(scope) == 3, "guard against the loop below passing vacuously"
    for list_id in scope:
        back = traceback(list_id)
        assert chain.field_c in back["geoids"], (
            f"{list_id} was reported forward from field C, but tracing it back "
            f"does not contain field C"
        )


def test_a_partner_grant_gets_lot_codes_but_no_identities(chain):
    """Tier 1. A commercial partner learns which lots are affected -- enough to
    act on a recall -- without learning who else grows or packs. 1.1455 obliges
    firms to produce records to FDA, not to each other."""
    r = client.post("/traceforward",
                    json={"seed_geoid": chain.field_c, "scope": "demo-recall"},
                    headers=_grant_for(chain.field_c))
    assert r.status_code == 200
    assert r.json()["tier"] == 1
    assert "holder_account" not in r.text


def test_a_regulator_credential_escalates_to_identities_and_is_audited(chain, meal):
    """Tier 3. FDA needs the firms, not just the lot codes, and that escalation
    is recorded in the MEAL ledger -- so the disclosure itself is auditable."""
    from unittest.mock import patch
    with patch("app.routers.traceforward._resolve_holders") as holders:
        holders.return_value = {chain.lot_shred: "processor@demo.test"}
        r = client.post("/traceforward",
                        json={"seed_geoid": chain.field_c, "scope": "demo-recall"},
                        headers=_authority())
    assert r.status_code == 200, r.text
    assert r.json()["tier"] == 3

    audited = [p for p in meal if p.get("event") == "traceforward.invoked"]
    assert audited, "a Tier 3 disclosure must leave a MEAL entry"
    assert audited[-1]["seed_geoid"] == chain.field_c


# ==========================================================================
# FSMA-SPECIFIC PROPERTIES. The arguments IFPA will actually be shown.
# ==========================================================================

def test_the_lot_code_is_verifiable_without_a_glossary(chain):
    """1.1455(c)(1) requires "information needed to understand these records,
    such as internal or external coding systems, glossaries". A content-derived
    lot code needs none: recompute it from the members and compare."""
    out = traceback(chain.lot_shred)
    by_lot = {h["list_id"]: h for h in out["hops"]}
    inputs = by_lot[chain.lot_shred]["input_list_ids"]

    recomputed = merkle_root(canonical_members([f"L:{i}" for i in inputs]))
    assert recomputed == chain.lot_shred, (
        "the lot code must be recomputable from its declared inputs, or the "
        "no-glossary claim is false"
    )


def test_a_forged_membership_claim_fails_recomputation(chain):
    """The other half of the same property: a firm cannot claim a lot contained
    something it did not, because the claim would not hash to the lot code."""
    forged = [f"L:{chain.lot_pack_1}"]          # omit the Yuma lot
    assert merkle_root(canonical_members(forged)) != chain.lot_shred


def test_the_whole_trace_is_one_request(chain):
    """1.1455(c)(1): records within 24 hours of request. The relevant property
    is that no cross-firm reconciliation stands between the request and the
    answer -- the trace is a single call over a shared namespace."""
    import time
    started = time.time()
    out = traceback(chain.lot_kit)
    elapsed = time.time() - started
    assert set(out["geoids"]) == chain.all_fields
    assert elapsed < 5.0, f"single-call trace took {elapsed:.2f}s"


def test_facilities_and_fields_share_one_namespace(chain):
    """Every hop location resolves in the same registry as the fields, so an
    investigator needs one lookup mechanism rather than one per firm."""
    db = SessionLocal()
    try:
        out = traceback(chain.lot_kit)
        # asserted first, or the loops below pass by iterating nothing
        assert len(out["locations"]) == 4
        assert len(out["geoids"]) == 3
        for location in out["locations"]:
            row = db.query(GeoID).filter(GeoID.geo_id == location).one_or_none()
            assert row is not None, f"hop location {location} is not registered"
            assert row.boundary_type == "facility"
        for field in out["geoids"]:
            row = db.query(GeoID).filter(GeoID.geo_id == field).one_or_none()
            assert row is not None and row.boundary_type == "field"
    finally:
        db.close()


# ==========================================================================
# AUTHORIZATION. A regulator may do this; nobody else may.
# ==========================================================================

def test_traceback_refuses_without_a_credential(chain):
    r = client.get(f"/list-artifact/{chain.lot_kit}/traceback")
    assert r.status_code == 404, (
        "an unauthorized caller must not learn that the lot exists; 404, not 403"
    )


def test_traceback_refuses_a_revoked_authority_credential(chain):
    revoked = (DEV_KEYS / "revoked_authority.sdjwt").read_text().strip()
    r = client.get(f"/list-artifact/{chain.lot_kit}/traceback",
                   headers={"X-Authority-Token": revoked})
    assert r.status_code in (401, 404), r.status_code


def test_traceback_refuses_an_untrusted_issuer(chain):
    untrusted = (DEV_KEYS / "untrusted_authority.sdjwt").read_text().strip()
    r = client.get(f"/list-artifact/{chain.lot_kit}/traceback",
                   headers={"X-Authority-Token": untrusted})
    assert r.status_code in (401, 404), r.status_code


# ==========================================================================
# DEBUG BEHAVIOUR. What the trace does when the data is imperfect, which in a
# real supply chain it always is.
# ==========================================================================

def test_an_unrecorded_hop_location_is_reported_not_hidden():
    """A lot whose creation site was never captured is a compliance gap under
    1.1330(a)(14). The trace must name it, so the investigator knows to ask."""
    bare = Chain(record_locations=False)
    out = traceback(bare.lot_kit)

    assert set(out["geoids"]) == bare.all_fields, "the trace still answers"
    assert out["locations"] == []
    assert set(out["hops_missing_location"]) == {
        bare.lot_kit, bare.lot_shred, bare.lot_pack_1, bare.lot_pack_2
    }
    assert all(h["location_recorded"] is False for h in out["hops"])


def test_a_depth_limit_is_flagged_rather_than_silently_truncating(chain):
    """A partial answer that looks complete is the dangerous failure mode."""
    out = traceback(chain.lot_kit, max_depth=2)
    assert out["truncated"] is True
    assert set(out["geoids"]) != chain.all_fields, (
        "with the walk cut short the field set must be incomplete, which is "
        "exactly why the flag has to be there"
    )

    full = traceback(chain.lot_kit, max_depth=64)
    assert full["truncated"] is False
    assert set(full["geoids"]) == chain.all_fields


def test_a_corrupt_edge_cannot_hang_the_trace(chain):
    """Cycles cannot arise through content-derived ListIDs -- a lot would have to
    contain its own root -- but a corrupt row must still terminate."""
    from app.models.geo_id_model import ListParentEdge
    db = SessionLocal()
    try:
        db.add(ListParentEdge(child_list_id=chain.lot_kit,
                              parent_list_id=chain.lot_pack_1))
        db.commit()
    finally:
        db.close()

    out = traceback(chain.lot_kit)
    seen = [h["list_id"] for h in out["hops"]]
    assert len(seen) == len(set(seen)), "each lot reported once despite the cycle"
    assert chain.field_c in out["geoids"]


def test_a_lot_citing_a_region_admits_its_field_set_is_incomplete(chain):
    """A grower may declare a whole ranch rather than each field. Region
    membership is spatial -- /traceforward tests a field's S2 cells against the
    region cover -- and there is nothing to read back, so trace-back reports the
    region and flags the closure as a lower bound instead of implying it is total.
    Under-scoping a recall is the expensive direction of this error."""
    ranch = client.post("/region-artifact",
                        json={"members": [chain.field_a, chain.field_b]})
    assert ranch.status_code == 200, ranch.text
    region_id = ranch.json()["region_id"]

    lot = _create_lot([f"R:{region_id}", chain.field_c],
                      at=chain.packhouse_1, event="initial_pack")

    out = traceback(lot)
    assert out["region_ids"] == [region_id]
    assert out["regions_unexpanded"] is True, (
        "the caller must be told the field set is a lower bound"
    )
    hop = next(h for h in out["hops"] if h["list_id"] == lot)
    assert hop["input_region_ids"] == [region_id]
    # the directly declared field is still returned
    assert chain.field_c in out["geoids"]
    # and a lot with no region at all does not raise the flag
    assert traceback(chain.lot_kit)["regions_unexpanded"] is False


def test_this_scenario_cannot_leak_into_other_suites():
    """Guard on the fixtures rather than the code.

    Registering a region publishes its cover cells to every later trace-forward
    in the session, because containment is resolved by probing a seed's cells and
    all their ancestors. If this file reused a cell that another suite hardcodes,
    that suite would silently gain a match -- a false positive, which in a recall
    means product pulled for no reason. So: our cells are unique among ourselves,
    and prefix-disjoint from the tokens the other suites use.
    """
    hardcoded_elsewhere = {"111", "111111", "222", "222222", "333333",
                           "3f1a9f0f", "89c259", "89c25b"}
    ours = [_fresh_cell() for _ in range(64)]

    assert len(set(ours)) == 64, "each place must get its own cell"
    for cell in ours:
        for other in hardcoded_elsewhere:
            assert not cell.startswith(other), f"{cell} sits inside {other}"
            assert not other.startswith(cell), f"{other} sits inside {cell}"


def test_an_unknown_lot_is_a_404(chain):
    r = client.get("/list-artifact/does-not-exist/traceback", headers=_authority())
    assert r.status_code == 404


# ==========================================================================
# The investigator's-eye walkthrough. `pytest -s -k walkthrough`
# ==========================================================================

def test_walkthrough_for_ifpa(chain, capsys):
    out = traceback(chain.lot_kit)
    reached = _recall_scope(chain.field_c)

    db = SessionLocal()
    name = {g.geo_id: g.field_name for g in db.query(GeoID).all()}
    db.close()

    with capsys.disabled():
        print("\n" + "=" * 74)
        print("FSMA 204 WALKTHROUGH — romaine lettuce, retail case to fields")
        print("=" * 74)
        print("\nAn FDA investigator holds one case of bagged salad and presents")
        print("its lot code. One request, one authority credential.\n")
        print(f"  GET /list-artifact/{chain.lot_kit[:16]}…/traceback\n")

        for hop in sorted(out["hops"], key=lambda h: (h["depth"], h["list_id"])):
            pad = "  " + "    " * hop["depth"]
            where = name.get(hop["location_geo_id"], "LOCATION NOT RECORDED")
            print(f"{pad}hop {hop['depth']}  {hop['event_type'] or '?':<15} at {where}")
            print(f"{pad}       lot {hop['list_id'][:24]}…")
            for field in hop["geoids"]:
                print(f"{pad}       └─ harvested from {name.get(field, field)}")

        print(f"\n  Fields implicated: {len(out['geoids'])}")
        for f in out["geoids"]:
            print(f"    - {name.get(f, f)}")

        print("\n  Every lot code above is a Merkle root over its own members, so")
        print("  each link was verified arithmetically rather than by trusting a")
        print("  firm's spreadsheet. No glossary was needed (21 CFR 1.1455(c)(1)).")

        print("\n" + "-" * 74)
        print("Now the reverse question: contamination is confirmed in Yuma Field C.")
        print("What must be recalled?\n")
        for hop in sorted(out["hops"], key=lambda h: h["depth"]):
            mark = "RECALL" if hop["list_id"] in reached else "clear "
            where = name.get(hop["location_geo_id"], "?")
            print(f"    [{mark}] {hop['event_type'] or '?':<15} at {where}")
        print("\n  The Salinas packing lot is clear: Field C material never entered")
        print("  it. That distinction is what limits the scope of a recall, and it")
        print("  is only available because the field identity is content-derived")
        print("  rather than a private name needing reconciliation.")
        print("=" * 74 + "\n")

    assert set(out["geoids"]) == chain.all_fields
