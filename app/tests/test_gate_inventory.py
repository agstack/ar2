"""Every door to a gated answer carries the gate.

The recurrence (lesson key `second-door-to-a-gated-answer`): a route is gated
carefully, and then a second route, a flag, or a header answers the same
question with no credential, because it was added for convenience and the
convenience never got the gate. Four instances by 2026-10-08:

  * GET /list-artifact/reverse/{geoid} answered the first hop of trace-forward
    for any login (AG-015; agstack/ar2#19);
  * AR_ACCOUNT_L1=legacy turned any login into an L1 grant on four field
    routes (agstack/ar2#22);
  * the shared secret opened trace-back with no credential and no ledger
    entry (AG-076; agstack/ar2#20);
  * ar2-hub minted the trace-forward capability from a self-typed role
    (agstack/ar2-hub#9; held there, not here).

This test is the mechanization. It walks the app's routes, picks the ones
whose path answers a gated question, and reads each handler's source:

  1. the handler must call one of the gate functions;
  2. the handler must not read the environment, which is the idiom every
     flag-shaped bypass has used.

A route that is gated but not yet merged is listed in OPEN_DOORS with the PR
that closes it. Such a route is required to still fail: when the PR merges,
this test goes red until the waiver is removed, so a waiver cannot outlive
the door it describes.
"""

import inspect
import re

import pytest
from fastapi.routing import APIRoute

from app.main import app

GATE_CALLS = ("authorize_artifact(", "verify_field_grant(")
ENV_READ = re.compile(r"\bos\.(getenv|environ)\b")

# Path prefixes whose answer is gated: a field's L1 data, a lot's members,
# a trace in either direction.
GATED_PREFIXES = (
    "/fetch-field/",
    "/fetch-field-wkt/",
    "/fetch-field-centroid/",
    "/resolve/",
    "/geoid/",
    "/list-artifact/",
    "/traceforward",
)

# Doors that are open on this tree and closed on an unmerged branch.
# Keyed by handler name; the value is the pull request. Remove the entry
# when the PR merges; the test below insists on it.
OPEN_DOORS = {
    "get_list_artifact_reverse": "agstack/ar2#19 (no gate call)",
    "fetch_field": "agstack/ar2#22 (reads AR_ACCOUNT_L1)",
    "fetch_field_wkt": "agstack/ar2#22 (reads AR_ACCOUNT_L1)",
    "fetch_field_centroid": "agstack/ar2#22 (reads AR_ACCOUNT_L1)",
    "eudr_export": "agstack/ar2#22 (reads AR_ACCOUNT_L1)",
}


def _walk(routes):
    """APIRoutes, through FastAPI's included-router wrappers."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _walk(inner.routes)


def _gated_handlers():
    seen = {}
    for route in _walk(app.routes):
        if not route.path.startswith(GATED_PREFIXES):
            continue
        if "GET" not in route.methods and route.path != "/traceforward":
            continue  # registration routes write; the gate is on reads and traces
        seen.setdefault(route.endpoint.__name__, (route.path, route.endpoint))
    return seen


def _problems(fn) -> list[str]:
    src = inspect.getsource(fn)
    out = []
    if not any(call in src for call in GATE_CALLS):
        out.append("no gate call")
    if ENV_READ.search(src):
        out.append("reads the environment inside a gated handler")
    return out


HANDLERS = _gated_handlers()


def test_the_inventory_is_not_empty():
    """If route discovery silently matched nothing, every test below would pass."""
    assert len(HANDLERS) >= 6, sorted(HANDLERS)


@pytest.mark.parametrize("name", sorted(HANDLERS))
def test_every_door_to_a_gated_answer_carries_the_gate(name):
    path, fn = HANDLERS[name]
    problems = _problems(fn)
    if name in OPEN_DOORS:
        assert problems, (
            f"{name} ({path}) is listed as an open door closed by {OPEN_DOORS[name]}, "
            f"but it now passes. The PR has merged: remove it from OPEN_DOORS.")
        pytest.xfail(f"{name}: {', '.join(problems)}; closed by {OPEN_DOORS[name]}")
    assert not problems, f"{name} ({path}): {', '.join(problems)}"


def test_the_check_can_fire():
    """A handler with no gate call and an environment read is flagged on both counts."""
    def leaky(geo_id: str):
        import os
        if os.getenv("ANY_FLAG") == "yes":
            return {"wkt": "..."}
        return {}
    assert _problems(leaky) == ["no gate call", "reads the environment inside a gated handler"]

    def gated(geo_id: str, x_field_grant=None):
        return verify_field_grant(x_field_grant, geo_id)  # noqa: F821 -- source is what is inspected
    assert _problems(gated) == []
