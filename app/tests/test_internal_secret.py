"""What the shared secret opens, and what it leaves in the ledger.

AR2_INTERNAL_SHARED_SECRET lets Pancake read a lot's members in order to mint
a grant for the lot's owner, before any grant exists. Until 2026-10 the same
secret also opened the whole trace-back walk, and neither read left a MEAL
entry (review ledger AG-076). Pancake, holding the same secret, treats the
header as transport only and still demands a credential and still logs.

This file holds AR2 to the same shape:

  * the secret opens one route, GET /list-artifact/{list_id}, and that read is
    written to the ledger before the members are returned;
  * the secret does not open GET /list-artifact/{list_id}/traceback;
  * a wrong secret opens nothing.
"""

import os
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

TESTKIT = os.path.join(os.path.dirname(__file__), "testkit", "dev_keys")
os.environ["AR_TRUSTED_ISSUER_PUBKEY"] = os.path.join(TESTKIT, "dev_issuer_public.pem")
os.environ["AR_TRUSTED_AUTHORITY_PUBKEY"] = os.path.join(TESTKIT, "authority_issuer_public.pem")
os.environ.setdefault("AR2_INTERNAL_SHARED_SECRET", "internal-test-secret")

from app.auth import require_hub_user  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.geo_id_model import GeoID  # noqa: E402

SECRET = os.environ["AR2_INTERNAL_SHARED_SECRET"]
RIGHT = {"x-pancake-internal": SECRET}
WRONG = {"x-pancake-internal": SECRET + "x"}

client = TestClient(app)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_STATUS_LIST_DIR", TESTKIT)
    # Other modules install their hub-user override at import time, which is
    # before this fixture runs and after it would have popped. Restore rather
    # than pop, or every later module runs against the real dependency.
    previous = app.dependency_overrides.get(require_hub_user)
    app.dependency_overrides[require_hub_user] = lambda: {"sub": "pancake-forwarded@demo.com"}
    yield
    if previous is None:
        app.dependency_overrides.pop(require_hub_user, None)
    else:
        app.dependency_overrides[require_hub_user] = previous


@pytest.fixture
def meal():
    packets = []
    with patch("app.meal_logger._append_to_meal_chain", side_effect=packets.append):
        yield packets


def _geoid() -> str:
    db = SessionLocal()
    gid = "urn:agstack:geoid:SECRET_" + uuid.uuid4().hex[:8]
    db.add(GeoID(geo_id=gid, geo_id_short="S_" + uuid.uuid4().hex[:8],
                 content_hash="H_" + uuid.uuid4().hex[:8], s2_cells=["111"]))
    db.commit()
    db.close()
    return gid


def _lot() -> tuple[str, str]:
    field = _geoid()
    r = client.post("/list-artifact", json={"members": [field]})
    assert r.status_code == 200, r.text
    return r.json()["list_id"], field


def _authority() -> dict:
    return {"X-Authority-Token": open(os.path.join(TESTKIT, "valid_authority.sdjwt")).read().strip()}


# --- GET /list-artifact/{list_id} ---------------------------------------------

def test_the_secret_reads_members_and_the_read_is_in_the_ledger(meal):
    lot, field = _lot()
    r = client.get(f"/list-artifact/{lot}", headers=RIGHT)
    assert r.status_code == 200, r.text
    assert r.json()["members"] == [field]
    assert len(meal) == 1, "an internal read must leave exactly one ledger entry"
    entry = meal[0]
    assert entry["event"] == "list.read.internal"
    assert entry["artifact"] == lot
    assert entry["who"] == "pancake-forwarded@demo.com"
    assert entry["credential_id"] == "none:internal-shared-secret"
    # Pancake's AuditEventRequest (pancake_services/grants/routers/audit.py)
    # requires these four as strings; the e2e workflow found a None here.
    for key in ("event", "who", "credential_id", "seed_geoid"):
        assert isinstance(entry[key], str), key


def test_the_ledger_write_happens_before_the_members_are_returned():
    """Fail closed: if the ledger cannot be written, the members are not disclosed."""
    lot, _ = _lot()
    from fastapi import HTTPException
    with patch("app.meal_logger._append_to_meal_chain",
               side_effect=HTTPException(status_code=503, detail="ledger down")):
        r = client.get(f"/list-artifact/{lot}", headers=RIGHT)
    assert r.status_code == 503
    assert "members" not in r.text


def test_a_wrong_secret_does_not_read_members(meal):
    lot, _ = _lot()
    r = client.get(f"/list-artifact/{lot}", headers=WRONG)
    assert r.status_code == 404  # raise_404_on_fail: the lot's existence is not confirmed
    assert meal == []


def test_no_secret_and_no_credential_does_not_read_members(meal):
    lot, _ = _lot()
    r = client.get(f"/list-artifact/{lot}")
    assert r.status_code == 404
    assert meal == []


def test_the_credential_path_is_unchanged_by_the_secret_path(meal):
    """A credential still reads and still logs as traceback.invoked, not as internal."""
    lot, field = _lot()
    r = client.get(f"/list-artifact/{lot}", headers=_authority())
    assert r.status_code == 200, r.text
    assert r.json()["members"] == [field]
    assert [p["event"] for p in meal] == ["traceback.invoked"]


# --- GET /list-artifact/{list_id}/traceback -------------------------------------

def test_the_secret_does_not_open_trace_back(meal):
    """The control for the route the secret used to open. Fails against the
    previous code, which returned the walk with no ledger entry."""
    lot, _ = _lot()
    r = client.get(f"/list-artifact/{lot}/traceback", headers=RIGHT)
    assert r.status_code == 404, r.text
    assert "hops" not in r.text
    assert meal == []


def test_trace_back_still_answers_a_credential_and_the_check_can_fire(meal):
    """Same lot, with a credential: the walk is returned and logged. This is
    what shows the control above is refusing a real answer, not an empty one."""
    lot, field = _lot()
    r = client.get(f"/list-artifact/{lot}/traceback", headers=_authority())
    assert r.status_code == 200, r.text
    assert field in r.json()["geoids"]
    assert [p["event"] for p in meal] == ["traceback.invoked"]


def test_the_secret_plus_a_credential_on_trace_back_is_just_the_credential(meal):
    lot, field = _lot()
    r = client.get(f"/list-artifact/{lot}/traceback", headers={**RIGHT, **_authority()})
    assert r.status_code == 200
    assert field in r.json()["geoids"]
    assert [p["event"] for p in meal] == ["traceback.invoked"]
