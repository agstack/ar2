# Licensed under the EUPL, Version 1.2 or – as soon they will be approved by
# the European Commission - subsequent versions of the EUPL (the "Licence");
# You may not use this work except in compliance with the Licence.
# You may obtain a copy of the Licence at:
# https://joinup.ec.europa.eu/software/page/eupl

"""The agent surface mirrors the HTTP API: no more, no less.

That is the whole contract of `app/mcp_server.py`, and this file is the part
of it that holds. A paragraph saying "keep these in step" is
`prose-warning-instead-of-a-check`, three occurrences in this repository's
ledger, and two surfaces over one database is `twin-divergence`, six.

The comparison is against the routers' own route table, computed at test
time. A hand-maintained list of expected tool names would agree with itself
forever while the API grew underneath it -- which is the same defect in a
different costume.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

from app import mcp_server

HUB = "http://hub.example"
RESOURCE = "http://ar2.example/mcp"


@pytest.fixture
def server():
    return mcp_server.build_mcp(hub_url=HUB, resource_url=RESOURCE)


def _tools(server) -> dict:
    return {t.name: t for t in asyncio.run(server.list_tools())}


def _routes() -> set[str]:
    """Every route the mirrored routers serve, as "METHOD /path".

    Computed from the routers themselves. A hand-written list of expected
    routes would agree with itself forever while the API grew underneath it,
    which is the same defect this file exists to prevent, in a costume.
    """
    found = set()
    for module in mcp_server.MIRRORED_ROUTERS:
        for route in module.router.routes:
            for method in route.methods - {"HEAD", "OPTIONS"}:
                found.add(f"{method} {route.path}")
    return found


def test_the_route_table_is_not_empty():
    """The comparisons below are vacuous if this is empty.

    `gate-reports-green-while-blind`, seven occurrences. Two empty sets are
    equal, so without this the mirror tests would pass on a broken import.
    """
    routes = _routes()
    assert len(routes) >= 20, f"only found {len(routes)} routes"
    assert "POST /register-field-boundary" in routes
    assert "POST /traceforward" in routes


def test_every_route_has_a_tool(server):
    """No less. A route an agent cannot reach is a door with one lock missing.

    When this fails you have added an endpoint. Add the tool in the same
    commit; that is the entire point of the check.
    """
    undeclared = sorted(_routes() - set(mcp_server.ROUTE_TOOLS))
    assert not undeclared, (
        f"these AR2 routes are not in ROUTE_TOOLS: {undeclared}. The surface "
        f"is a mirror; add a tool per route in the same commit as the route.")

    tools = _tools(server)
    unbuilt = sorted({name for route, name in mcp_server.ROUTE_TOOLS.items()
                      if name not in tools})
    assert not unbuilt, (
        f"ROUTE_TOOLS names these tools but the server does not publish "
        f"them: {unbuilt}")


def test_every_tool_has_a_route(server):
    """No more. A tool without a route is functionality invented here.

    Composing two endpoints into one convenient tool is the tempting version
    and it is still a second implementation: it decides for the caller what
    the API left open, and it drifts the moment either endpoint changes.
    """
    mirrored = set(mcp_server.ROUTE_TOOLS.values())
    invented = sorted(
        name for name in _tools(server)
        if name not in mirrored and name not in mcp_server.NON_API_TOOLS)
    assert not invented, (
        f"these MCP tools mirror no AR2 route: {invented}. Either add the "
        f"route, or if the tool is genuinely not API functionality, declare "
        f"it in NON_API_TOOLS with a reason.")


def test_the_only_non_api_tool_is_asking_a_person(server):
    """The exemption list exists; keep it from becoming a loophole.

    NON_API_TOOLS is how a tool escapes the mirror rule, so it is exactly
    where the rule will be eroded. Asking a human for context is interaction
    about a call. Anything that computes an answer is not, whatever it is
    called, and should fail here until someone argues it in review.
    """
    assert set(mcp_server.NON_API_TOOLS) == {"elicit_context"}
    assert all(mcp_server.NON_API_TOOLS.values()), "each needs a stated reason"
    assert "elicit_context" in _tools(server)


def test_point_registration_is_present_and_says_that_the_geoid_inverts(server):
    """AG-034 is disclosed, not hidden by withholding the tool.

    An earlier version of the surface left these tools out. That protected
    nobody -- the REST routes still serve point registration to any token
    holder -- and created two surfaces with different rules over one
    database. The defect belongs in geoid_v2; until it is fixed, every
    surface should say so, and the caveat is one constant so it cannot be
    updated in one description and left stale in another.
    """
    tools = _tools(server)
    for name in ("register_point", "register_points_geojson"):
        assert name in tools, f"{name} is a real AR2 route and must be mirrored"
        assert "inverts" in (tools[name].description or ""), (
            f"{name} does not warn that a point's Geo Id inverts to its "
            f"coordinate. If AG-034 has been fixed, remove POINT_CAVEAT and "
            f"this assertion in the same commit as the fix.")


def test_the_point_caveat_matches_the_open_defect():
    """The warning text must still describe a defect that is actually open.

    If `test_point_polygon_peers.py` stops xfailing, AG-034 is fixed and this
    caveat has become a false statement on a public tool surface. A stale
    warning is worse than none: it spends the reader's trust on nothing.
    """
    assert "AG-034" in mcp_server.POINT_CAVEAT

    # Bound to the marker on the inversion test itself, not to the string
    # "AG-034" appearing somewhere in that file -- it appears four times, so
    # a grep stays green after the marker is gone. Fixing the defect means
    # removing this marker, and that is what should fire here.
    from app.tests import test_point_polygon_peers as peers

    subject = peers.test_a_points_geo_id_cannot_be_turned_back_into_its_coordinate
    xfails = [m for m in getattr(subject, "pytestmark", []) if m.name == "xfail"]
    assert xfails, (
        "the point-inversion test no longer expects to fail, so AG-034 is "
        "fixed. Remove POINT_CAVEAT and this test in the same commit: a "
        "warning that has stopped being true spends the reader's trust on "
        "nothing.")
    assert "AG-034" in xfails[0].kwargs.get("reason", "")


def test_every_tool_says_what_it_requires_or_discloses(server):
    """A description is all an agent reads before choosing a tool.

    terrapipe-os puts a disclosure tier on every layer. The equivalent here
    is that each tool states what it needs and what a caller without it gets
    instead, so the privacy model does not have to be inferred from a name.
    """
    speaks_to_access = (
        "token", "grant", "anonymous", "tier", "coarse", "aggregate",
        "masking", "l0", "discloses", "gated", "withheld", "requires",
        "registered", "human",
    )
    for name, tool in _tools(server).items():
        text = (tool.description or "").lower()
        assert len(text) > 120, f"{name} is barely described"
        assert any(word in text for word in speaks_to_access), (
            f"{name} does not say what it requires or what it discloses")


def test_read_tools_name_the_route_they_mirror(server):
    """The description carries the HTTP route, so the two are traceable.

    Someone reading the tool list should be able to find the endpoint, and
    someone changing an endpoint should be able to find the tool.
    """
    for name, tool in _tools(server).items():
        if name in mcp_server.NON_API_TOOLS:
            continue
        text = tool.description or ""
        assert ("GET /" in text or "POST /" in text), (
            f"{name} does not name the HTTP route it mirrors")


def _routes_requiring_a_hub_token() -> set[str]:
    """Routes whose handler depends on require_hub_user.

    Read off the dependency itself rather than listed by hand, so the answer
    comes from the code that enforces it.
    """
    from app.auth import require_hub_user

    gated = set()
    for module in mcp_server.MIRRORED_ROUTERS:
        for route in module.router.routes:
            for param in inspect.signature(route.endpoint).parameters.values():
                dep = getattr(param.default, "dependency", None)
                if dep is require_hub_user:
                    for method in route.methods - {"HEAD", "OPTIONS"}:
                        gated.add(f"{method} {route.path}")
    return gated


def test_tools_refuse_anonymously_wherever_the_route_does(server, monkeypatch):
    """The gate on a tool must match the gate on its route.

    Calling a handler directly bypasses its Depends defaults, so a tool that
    passes an optional principal to a require_hub_user route serves anonymous
    callers that HTTP refuses. That is not a theoretical hazard: three tools
    shipped that way in this file's first draft -- get_list_artifact,
    get_list_artifact_reverse and trace_back -- and were caught here rather
    than in review.

    The expected set is derived from the dependency, so a route that becomes
    gated later fails this until its tool is gated too.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.setattr(mcp_server, "verify_token", lambda _t: None)
    tools = _tools(server)
    gated_routes = _routes_requiring_a_hub_token()
    assert len(gated_routes) >= 8, "sanity: the gated set looks too small"

    checked = 0
    for route in sorted(gated_routes):
        name = mcp_server.ROUTE_TOOLS[route]
        arguments = _minimal_arguments(tools[name])
        with pytest.raises(ToolError, match="401") as raised:
            asyncio.run(server.call_tool(name, arguments))
        assert "hub token" in str(raised.value)
        checked += 1
    assert checked == len(gated_routes)


def _minimal_arguments(tool) -> dict:
    """Just enough to reach the auth check, of the right JSON types.

    The call must fail on the token and not on a missing argument, or the
    test would pass without ever reaching the gate.
    """
    schema = tool.input_schema or {}
    required = schema.get("required", [])
    stand_ins = {
        "string": "x", "integer": 1, "number": 1.0,
        "boolean": False, "array": [], "object": {},
    }
    arguments = {}
    for name in required:
        spec = schema.get("properties", {}).get(name, {})
        kind = spec.get("type")
        if kind is None:  # a union, e.g. "string | null"
            kind = next((o.get("type") for o in spec.get("anyOf", [])
                         if o.get("type") != "null"), "string")
        arguments[name] = stand_ins.get(kind, "x")
    return arguments


#: Read routes whose L1 answer is gated on a field grant, not on a token.
#: Named here because the description is the contract an agent acts on, and
#: getting it wrong is not a documentation slip -- an agent told a token
#: suffices will report a masked cell as a boundary.
GRANT_GATED_TOOLS = (
    "fetch_field", "fetch_field_wkt", "fetch_field_centroid", "eudr_export")


def test_grant_gated_tools_do_not_claim_a_token_is_enough(server):
    """The descriptions must match what the handlers actually do.

    Every one of these routes computes

        has_l1 = bool(x_field_grant and verify_field_grant(...))

    so a hub token alone leaves the mask on. The first draft of this file
    described them as "without a token L0, with one L1", which was checked
    against apply_masking_logic -- a helper the routes wrap rather than the
    rule they apply. A live call returned L0 for a valid bearer and caught
    it. `claim-not-in-the-evidence`, four occurrences.
    """
    tools = _tools(server)
    for name in GRANT_GATED_TOOLS:
        text = (tools[name].description or "").lower()
        assert "field_grant" in text, (
            f"{name} does not tell the agent a grant is what lifts the mask")
        assert "does not lift the mask" in text, (
            f"{name} must say explicitly that a hub token is not sufficient, "
            f"because the obvious assumption is that it is")


def test_the_grant_gate_is_still_what_the_handlers_do(server):
    """Read the rule off the handlers, so the test above cannot go stale.

    If the routes are ever changed to honour a bare hub token, these
    descriptions become wrong in the other direction and should be revised.
    Asserting the source keeps the two bound without a live database.
    """
    from app.routers import fetch_field as fetch

    source = inspect.getsource(fetch)
    assert source.count(
        "has_l1 = bool(x_field_grant and verify_field_grant(") >= 3, (
        "the grant-gating rule in fetch_field.py has changed shape. Re-read "
        "the routes and update the tool descriptions to match before "
        "editing this assertion.")


def test_the_published_map_is_current():
    """docs/MCP_SURFACE.md is regenerated and compared, not trusted.

    The table exists so a reader can see which tool serves which endpoint
    without reading the module. That makes it a second copy of ROUTE_TOOLS,
    and a second copy nobody checks is `twin-divergence` waiting -- six
    occurrences already. Regenerating here costs a second and means the file
    is either right or red.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import mcp_surface

    expected = mcp_surface.render()
    actual = mcp_surface.DOC.read_text() if mcp_surface.DOC.exists() else ""
    assert actual == expected, (
        "docs/MCP_SURFACE.md is out of date. Regenerate it with "
        "`python3 scripts/mcp_surface.py --write` and commit the result; do "
        "not edit the file by hand.")


def test_the_map_reports_the_grant_gate_correctly():
    """The column that is easiest to get wrong, checked against the routes.

    The first version of this column matched `raise HTTPException` anywhere
    after `has_l1`, caught the generic 400 handler every route ends with,
    and published "refuses without" for fetch_field -- which degrades to L0
    and refuses nothing. That is a wrong fact in a generated document, which
    is worse than an absent one because it looks authoritative.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import mcp_surface

    by_tool = {row["tool"]: row["grant"] for row in mcp_surface.rows()}
    assert by_tool["fetch_field"] == "lifts mask to L1"
    assert by_tool["fetch_field_wkt"] == "lifts mask to L1"
    assert by_tool["fetch_field_centroid"] == "lifts mask to L1"
    assert by_tool["eudr_export"] == "refuses without", (
        "eudr_export is the one read route that refuses rather than "
        "answering coarsely; if that changed, the description must change too")
    assert by_tool["register_field_boundary"] == "--"


def test_the_server_refuses_to_build_unauthenticated(monkeypatch):
    """There is no anonymous mode, and refusing must be the default.

    terrapipe-os allows one for stdio, where the operator who launched the
    process is the principal. This server is only mounted into the HTTP
    service, so the same allowance would serve registration to the internet.
    """
    monkeypatch.delenv("HUB_URL", raising=False)
    monkeypatch.delenv("AR2_MCP_URL", raising=False)
    with pytest.raises(ValueError, match="unauthenticated"):
        mcp_server.build_mcp()

    monkeypatch.setenv("HUB_URL", HUB)
    with pytest.raises(ValueError, match="unauthenticated"):
        mcp_server.build_mcp()


def test_an_invalid_token_yields_no_principal(monkeypatch):
    """Tiering turns on this returning None, so prove that it can.

    If _principal were truthy for a bad token, every masked view would
    silently become an L1 one and nothing would look wrong.
    """
    monkeypatch.setattr(mcp_server, "verify_token", lambda _t: None)
    assert mcp_server._principal("rubbish") is None
    assert mcp_server._principal(None) is None

    monkeypatch.setattr(mcp_server, "verify_token", lambda _t: {"sub": "a@b.c"})
    assert mcp_server._principal("good") == {"sub": "a@b.c"}
    assert mcp_server._principal(None) is None, "no token is never a principal"


def test_write_routes_refuse_without_a_token(monkeypatch):
    """_required is the gate in front of every require_hub_user route."""
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.setattr(mcp_server, "verify_token", lambda _t: None)
    with pytest.raises(ToolError, match="401"):
        mcp_server._required(None, "registering")
    with pytest.raises(ToolError, match="401"):
        mcp_server._required("bad-token", "registering")

    monkeypatch.setattr(mcp_server, "verify_token", lambda _t: {"sub": "a@b.c"})
    assert mcp_server._required("good", "registering") == {"sub": "a@b.c"}


def test_the_token_verifier_rejects_what_the_rest_routes_reject(monkeypatch):
    """One verification path, not two.

    Drifting from app.auth.verify_token would make the tool surface a door
    with a different lock on the same room.
    """
    verifier = mcp_server.HubTokenVerifier()
    monkeypatch.setattr(mcp_server, "verify_token", lambda _t: None)
    assert asyncio.run(verifier.verify_token("bad")) is None

    monkeypatch.setattr(
        mcp_server, "verify_token",
        lambda _t: {"sub": "a@b.c", "exp": 99, "capabilities": ["trace-forward"]})
    granted = asyncio.run(verifier.verify_token("good"))
    assert granted is not None
    assert granted.subject == "a@b.c"
    assert granted.expires_at == 99
    assert "trace-forward" in granted.scopes, (
        "capabilities must reach the transport: traceforward gates on them")


def test_any_valid_hub_token_is_accepted_and_that_is_a_known_limitation(server):
    """Audience validation is off, deliberately, and this records why.

    The first version of this file asserted the opposite -- that a token
    minted for another resource is refused -- which is what a reviewer would
    want to be true. It is not true and cannot be yet: the hub issues one
    general-purpose bearer with no `aud` claim, so switching validation on
    refuses every real token, including the ones the demo runs on. Checked
    against a live token on 2026-09-20.

    So the claim is changed rather than the guard narrowed, per AGENTS.md,
    and the consequence is written down where it is enforced: a token
    obtained to read layers from terrapipe-os also authorises registration
    here. Recorded as a finding; the fix is audience-scoped hub tokens.

    This test fails if someone turns validation on without the hub change,
    which would take AR2's agent surface down for every existing caller.
    """
    assert server.settings.auth is not None
    assert server.settings.auth.validate_token_resource is False, (
        "audience validation was turned on. Confirm the hub now issues "
        "tokens carrying an `aud` claim for this resource -- if it does not, "
        "every existing token is now refused.")
    assert "no `aud` claim" in inspect.getsource(mcp_server), (
        "the reason audience validation is off must stay next to the switch")


def test_elicitation_degrades_when_the_client_cannot_ask():
    """Not every MCP client supports elicitation; that is not an error.

    The tool has to say so rather than fail, because an agent told "error"
    will retry, and an agent told supported=false will ask in conversation.
    """
    result = asyncio.run(
        mcp_server.ask_human("what is this field called?", None))
    assert result["supported"] is False
    assert result["why"]


def test_elicitation_reports_a_refusal_as_a_refusal():
    """A person declining is a real answer, not a failure to get one."""

    class Declined:
        action = "decline"
        data = None

    class Ctx:
        async def elicit(self, message, schema):
            return Declined()

    result = asyncio.run(mcp_server.ask_human("register this?", Ctx()))
    assert result["supported"] is True
    assert result["proceed"] is False


def test_elicitation_returns_what_the_person_supplied():
    """The accepted path, so the refusal test above is not the only one."""

    class Accepted:
        action = "accept"
        data = mcp_server.FieldContext(field_name="Lote 3",
                                       accuracy_class="walked")

    class Ctx:
        async def elicit(self, message, schema):
            return Accepted()

    result = asyncio.run(mcp_server.ask_human("what is this called?", Ctx()))
    assert result["answered"]["field_name"] == "Lote 3"
    assert result["answered"]["accuracy_class"] == "walked"
    assert result["proceed"] is True


def test_mounting_is_optional_and_its_absence_does_not_break_the_service(monkeypatch):
    """A node upgraded before the env vars are set must still serve REST.

    This is the disruption guard. The mount is built at import time, and an
    exception there would take down a registry that has been running since
    long before there was an agent surface.
    """
    monkeypatch.delenv("HUB_URL", raising=False)
    monkeypatch.delenv("AR2_MCP_URL", raising=False)

    from app import main

    assert main._build_mcp_app() is None

    monkeypatch.setenv("HUB_URL", HUB)
    monkeypatch.setenv("AR2_MCP_URL", RESOURCE)
    assert main._build_mcp_app() is not None
