"""Tier 3 trace-forward with nothing patched.

Every other Tier 3 test patches _resolve_holders and _append_to_meal_chain,
so none of them exercises the two HTTP calls that make Tier 3 what it is:
the ledger write to Pancake and the holder resolution from Pancake (review
ledger AG-016, "the identity tier has never been run end to end").

Here a stub Pancake listens on a real socket. It implements the two routes
AR2 calls, records every request it receives, and answers as Pancake would.
AR2 is pointed at it through PANCAKE_URL. No function in app/ is replaced.

What this proves that the patched tests cannot:
  * the ledger packet actually leaves the process, over HTTP, to the route
    Pancake exposes, with the shared secret in the header;
  * the holder request carries the authority credential and the secret,
    names the matched lots, and the answer is wired into the response;
  * the ledger write precedes holder resolution, so a disclosure that fails
    half-way is already on the record;
  * an unreachable ledger refuses the disclosure with 503, and asks Pancake
    for nothing.
"""

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TESTKIT = Path(__file__).parent / "testkit" / "dev_keys"
os.environ["AR_TRUSTED_ISSUER_PUBKEY"] = str(TESTKIT / "dev_issuer_public.pem")
os.environ["AR_TRUSTED_AUTHORITY_PUBKEY"] = str(TESTKIT / "authority_issuer_public.pem")
os.environ.setdefault("AR2_INTERNAL_SHARED_SECRET", "tier3-live-secret")

from app.auth import require_hub_user  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.geo_id_model import GeoID  # noqa: E402

SECRET = os.environ["AR2_INTERNAL_SHARED_SECRET"]
client = TestClient(app)


# --- a Pancake that answers on a socket ----------------------------------------

class StubPancake:
    """The two routes AR2 calls at Tier 3, and a record of every call."""

    def __init__(self, holders_by_list: dict[str, str]):
        self.holders_by_list = holders_by_list
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):  # silence
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                stub.requests.append({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                })
                if self.path == "/audit/events":
                    self._reply(201, {"ok": True})
                elif self.path == "/fieldlists/holders":
                    if self.headers.get("X-Pancake-Internal") != SECRET:
                        self._reply(401, {"detail": "wrong secret"})
                        return
                    if not self.headers.get("X-Authority-Token"):
                        self._reply(401, {"detail": "no authority credential"})
                        return
                    wanted = body.get("list_ids", [])
                    self._reply(200, {"holders": {
                        lid: stub.holders_by_list[lid] for lid in wanted if lid in stub.holders_by_list
                    }})
                else:
                    self._reply(404, {"detail": "no such route on the stub"})

            def _reply(self, code: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()

    def calls(self, path: str) -> list[dict]:
        return [r for r in self.requests if r["path"] == path]


# --- fixtures ---------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_STATUS_LIST_DIR", str(TESTKIT))
    # Other modules install their hub-user override at import time, which is
    # before this fixture runs and after it would have popped. Restore rather
    # than pop, or every later module runs against the real dependency.
    previous = app.dependency_overrides.get(require_hub_user)
    app.dependency_overrides[require_hub_user] = lambda: {
        "sub": "investigator@fda.test", "capabilities": ["trace-forward"]}
    yield
    if previous is None:
        app.dependency_overrides.pop(require_hub_user, None)
    else:
        app.dependency_overrides[require_hub_user] = previous


def _field() -> str:
    db = SessionLocal()
    gid = "urn:agstack:geoid:LIVE_" + uuid.uuid4().hex[:8]
    db.add(GeoID(geo_id=gid, geo_id_short="L_" + uuid.uuid4().hex[:8],
                 content_hash="H_" + uuid.uuid4().hex[:8], s2_cells=["111"]))
    db.commit()
    db.close()
    return gid


def _lot(members: list[str]) -> str:
    r = client.post("/list-artifact", json={"members": members})
    assert r.status_code == 200, r.text
    return r.json()["list_id"]


def _authority() -> dict:
    return {"X-Authority-Token": (TESTKIT / "valid_authority.sdjwt").read_text().strip()}


@pytest.fixture
def chain():
    field = _field()
    harvest = _lot([field])
    shipment = _lot([f"L:{harvest}"])
    return field, harvest, shipment


# --- the tests --------------------------------------------------------------------

def test_tier3_runs_end_to_end_against_a_live_ledger_and_holder_service(chain, monkeypatch):
    field, harvest, shipment = chain
    holders = {harvest: "packer@demo.test", shipment: "distributor@demo.test"}

    with StubPancake(holders) as pancake:
        monkeypatch.setenv("PANCAKE_URL", pancake.url)
        r = client.post("/traceforward",
                        json={"seed_geoid": field, "scope": "demo-recall"},
                        headers=_authority())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tier"] == 3
    assert set(body["list_ids"]) == {harvest, shipment}

    # The identities in the response are the ones the holder service returned.
    by_list = {m["list_id"]: m for m in body["matches"]}
    assert by_list[harvest]["holder_account"] == "packer@demo.test"
    assert by_list[shipment]["holder_account"] == "distributor@demo.test"
    assert all(m["holder_status"] == "resolved" for m in body["matches"])

    # The ledger write really left the process, to Pancake's route, with the secret.
    audits = pancake.calls("/audit/events")
    assert len(audits) == 1
    assert audits[0]["headers"].get("x-pancake-internal") == SECRET
    packet = audits[0]["body"]
    assert packet["event"] == "traceforward.invoked"
    assert packet["who"] == "investigator@fda.test"
    assert packet["seed_geoid"] == field
    assert packet["scope"] == "demo-recall"
    assert set(packet["list_ids"]) == {harvest, shipment}
    assert packet["credential_id"], "the credential's jti must be on the record"

    # The holder request carried the credential and named the lots.
    lookups = pancake.calls("/fieldlists/holders")
    assert len(lookups) == 1
    assert lookups[0]["headers"].get("x-authority-token") == _authority()["X-Authority-Token"]
    assert lookups[0]["headers"].get("x-pancake-internal") == SECRET
    assert lookups[0]["body"] == {"list_ids": sorted({harvest, shipment}),
                                  "scope": "demo-recall", "seed_geoid": field}

    # Record first, then disclose.
    assert [req["path"] for req in pancake.requests] == ["/audit/events", "/fieldlists/holders"]


def test_a_holder_the_service_does_not_know_is_reported_unresolved(chain, monkeypatch):
    field, harvest, shipment = chain
    with StubPancake({harvest: "packer@demo.test"}) as pancake:
        monkeypatch.setenv("PANCAKE_URL", pancake.url)
        r = client.post("/traceforward",
                        json={"seed_geoid": field, "scope": "demo-recall"},
                        headers=_authority())
    assert r.status_code == 200, r.text
    by_list = {m["list_id"]: m for m in r.json()["matches"]}
    assert by_list[harvest]["holder_status"] == "resolved"
    assert by_list[shipment]["holder_status"] == "unresolved"
    assert by_list[shipment].get("holder_account") is None  # omitted, not invented


def test_an_unreachable_ledger_refuses_the_disclosure_before_asking_for_holders(chain, monkeypatch):
    """Fail closed, and fail before the second call: a disclosure that cannot
    be recorded must not even reach the holder service."""
    field, harvest, shipment = chain
    with StubPancake({harvest: "packer@demo.test"}) as pancake:
        # A port nothing listens on; the stub is only here to prove it is not called.
        monkeypatch.setenv("PANCAKE_URL", "http://127.0.0.1:9")
        r = client.post("/traceforward",
                        json={"seed_geoid": field, "scope": "demo-recall"},
                        headers=_authority())
        assert r.status_code == 503, r.text
        assert "holder_account" not in r.text
        assert pancake.requests == []


def test_a_holder_service_that_refuses_is_a_502_after_the_ledger_entry(chain, monkeypatch):
    field, harvest, shipment = chain
    with StubPancake({}) as pancake:
        # Make the holder route refuse by withholding the secret AR2 sends.
        monkeypatch.setenv("PANCAKE_URL", pancake.url)
        monkeypatch.setenv("AR2_INTERNAL_SHARED_SECRET", SECRET + "-rotated-on-one-side")
        r = client.post("/traceforward",
                        json={"seed_geoid": field, "scope": "demo-recall"},
                        headers=_authority())
        assert r.status_code == 502, r.text
        # The ledger entry was written first; the refusal came second.
        assert [req["path"] for req in pancake.requests] == ["/audit/events", "/fieldlists/holders"]


def test_tier1_touches_pancake_for_nothing(chain, monkeypatch):
    """The control for the whole file: without an authority credential the
    stub must see no traffic at all, so the Tier 3 traffic above is Tier 3's."""
    from app.tests.testkit.grants import valid_grant_for
    field, harvest, shipment = chain
    with StubPancake({harvest: "packer@demo.test"}) as pancake:
        monkeypatch.setenv("PANCAKE_URL", pancake.url)
        r = client.post("/traceforward", json={"seed_geoid": field},
                        headers={"X-Grant-Token": valid_grant_for(field)})
    assert r.status_code == 200, r.text
    assert r.json()["tier"] == 1
    assert pancake.requests == []
