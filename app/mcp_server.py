# Licensed under the EUPL, Version 1.2 or – as soon they will be approved by
# the European Commission - subsequent versions of the EUPL (the "Licence");
# You may not use this work except in compliance with the Licence.
# You may obtain a copy of the Licence at:
# https://joinup.ec.europa.eu/software/page/eupl

"""The registry's API, for callers that are agents.

AR2 is the open half of the stack, and until now the only part of the DPI an
agent could address was terrapipe-os, which is the half that is not open. An
agent wanting a Geo Id had to be taught this service's REST shape by a human
first, which is the opposite of what a public registry is for.

**This surface is a mirror and nothing else.** One tool per route, the same
arguments, the same handler, the same reply. It adds no capability that HTTP
callers do not have and withholds none that they do.

That rule was learned by breaking it. The first version of this module left
out ``register_point`` on the grounds that AG-034 is open -- a point's Geo Id
is ``digest([leaf.ToToken()])`` at S2 level 30 and inverts to the coordinate.
But the REST route still serves it to any token holder, so withholding it here
protected no farmer; it only built a second door with a different lock on the
same room, which is ``twin-divergence``, already the most frequent lesson in
this repository's ledger. A privacy defect is fixed where it lives, in
``geoid_v2``, and until it is, every surface should describe it honestly.
Which is what the tool descriptions do.

The one thing this layer may add is **human context**. ``elicit_context``
exists because an agent composing a registration often lacks something only a
person can supply -- what to call a field, whether a 96%-overlapping boundary
really is the neighbour's plot, whether an area was measured or estimated. MCP
can ask. That is interaction about a call, not functionality beyond the API,
and it is why the mirror rule is stated as "no more functionality" rather than
"nothing at all".

Handlers are called as plain functions with dependencies passed explicitly.
They are ``def`` or ``async def`` with ``Depends`` defaults; supplying the
session and the principal by hand is the whole adaptation. Nothing here
reimplements a handler, so the disclosure tiering in
``fetch_field.apply_masking_logic`` -- the S2 level-10 cell for an anonymous
caller, the boundary for an authorised one -- is inherited rather than copied.

``test_mcp_surface.py`` compares this tool set against the routers' own route
table in both directions, so a route added without a tool, or a tool added
without a route, fails a test rather than a review.
"""

from __future__ import annotations

import inspect
import logging
import os
from typing import Any

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AnyHttpUrl, BaseModel, Field

from app.auth import verify_token
from app.database import SessionLocal
from app.routers import analytics_and_maintenance_apis as analytics
from app.routers import fetch_field as fetch
from app.routers import field_registration as registration
from app.routers import point_registration as points
from app.routers import traceforward as trace
from app.routers.traceforward import (
    RegisterListRequest,
    RegisterRegionRequest,
    TraceForwardRequest,
)
from app.schemas import (
    FetchFieldsForPointRequest,
    FieldRegistrationRequest,
    OverlapRequest,
    PointRegistrationRequest,
)

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "The AgStack Asset Registry (AR2). A field boundary registered here gets a "
    "Geo Id: a deterministic name derived from the geometry itself, so the same "
    "ground registered twice by two parties resolves to one identifier without "
    "either disclosing anything to the other. "
    "This tool set mirrors AR2's HTTP API exactly -- one tool per endpoint, no "
    "more and no less -- so anything you can do here you can do over REST, and "
    "the reverse. "
    "Disclosure is tiered, and reading precisely is gated on a grant rather "
    "than on being logged in. By default every reader -- authenticated or not "
    "-- sees masking level L0: the ~8 km S2 level-10 cell a field lies in, its "
    "country and its approximate area. The boundary itself (L1) needs "
    "field_grant, a field-access credential issued by that field's owner for "
    "that field. A hub token alone does not lift the mask. "
    "Writing anything does require a hub token. "
    "A Geo Id is not a secret and not a permission: holding one lets you ask, "
    "and what comes back still depends on what you are entitled to. "
    "Before registering on someone's behalf, consider elicit_context to ask them "
    "for a name, an accuracy class, or a decision about an overlap. "
    "This registry stores boundaries and identity only. For what is true about "
    "the ground -- vegetation, deforestation, weather -- ask a data node such as "
    "terrapipe-os, giving it the Geo Ids you obtain here."
)

# The AG-034 sentence, attached to every tool that mints or returns a point
# identifier. One constant, so the warning cannot be fixed in one description
# and left stale in another.
POINT_CAVEAT = (
    "Privacy caveat (AG-034, open): a point's Geo Id is the digest of a single "
    "S2 leaf cell of about 0.78 cm2, so the identifier itself inverts to the "
    "coordinate -- brute force over a one-metre box recovers it in a few "
    "thousand tries. Masking hides the body of the record and cannot hide the "
    "handle, because fetching anything requires quoting it. Treat a point Geo "
    "Id as being as sensitive as the location, and prefer a polygon where the "
    "plot has a boundary."
)


class HubTokenVerifier(TokenVerifier):
    """Accept exactly the tokens the REST routes accept.

    Wraps ``app.auth.verify_token``, the same JWKS check ``get_current_user``
    runs, so the tool surface cannot become an unauthenticated side door to
    operations the HTTP routes guard.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        claims = verify_token(token)
        if not claims:
            return None
        subject = str(claims.get("sub") or claims.get("email") or "unknown")
        return AccessToken(
            token=token,
            client_id=subject,
            subject=subject,
            scopes=list(claims.get("capabilities") or []),
            expires_at=int(claims["exp"]) if "exp" in claims else None,
            claims=dict(claims),
        )


def _principal(token: str | None) -> dict[str, Any] | None:
    """The claims dict the handlers expect as ``user``, or None.

    The handlers treat ``user`` as the whole of the tiering decision: truthy
    means L1. Passing verified claims through unchanged is what keeps the two
    surfaces answering identically.
    """
    return verify_token(token) if token else None


def _required(token: str | None, what: str) -> dict[str, Any]:
    """A principal for a route behind ``require_hub_user``, or a refusal."""
    user = _principal(token)
    if user is None:
        raise ToolError(
            f"401: {what} requires a hub token. Pass the bearer you "
            f"authenticated this session with as the token argument."
        )
    return user


async def _handle(fn, /, **kwargs):
    """Call a router handler with an explicit session, and translate failures.

    Handles both ``def`` and ``async def`` handlers: the traceforward router
    is synchronous and the rest are not. FastAPI's ``HTTPException`` means
    nothing to an MCP client, so it becomes a ToolError carrying the status
    and detail unchanged -- an agent that gets "403: Hub capabilities missing
    trace-forward" can act on it, where a bare failure teaches it nothing.
    """
    from fastapi import HTTPException  # local: keeps import cost off startup

    db = SessionLocal()
    try:
        result = fn(db=db, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result
    except HTTPException as exc:
        raise ToolError(f"{exc.status_code}: {exc.detail}") from exc
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced to the agent, not hidden
        raise ToolError(f"error: {exc}") from exc
    finally:
        db.close()


class FieldContext(BaseModel):
    """What a person can tell us that an agent cannot infer."""

    field_name: str | None = Field(
        default=None, description="What the holder calls this field")
    accuracy_class: str | None = Field(
        default=None,
        description="How the boundary was obtained: walked, traced from "
                    "imagery, surveyed, or estimated")
    declared_area_ha: float | None = Field(
        default=None, description="Area in hectares, if the holder knows it")
    proceed: bool = Field(
        default=True,
        description="False to abandon the registration this was asked about")
    note: str | None = Field(
        default=None, description="Anything else worth recording")


def build_mcp(*, hub_url: str | None = None,
              resource_url: str | None = None) -> MCPServer:
    """Build the server.

    Both URLs are required and there is no unauthenticated mode. terrapipe-os
    permits one for stdio, where the operator who launched the process is the
    principal; this server is only ever mounted into the HTTP service, so the
    same allowance here would serve registration to the internet.
    """
    hub_url = hub_url or os.getenv("HUB_URL")
    resource_url = resource_url or os.getenv("AR2_MCP_URL")
    if not hub_url or not resource_url:
        raise ValueError(
            "HUB_URL and AR2_MCP_URL are both required: the first is the token "
            "issuer, the second is the address clients reach this server at, "
            "and the SDK advertises both so an agent can discover where to get "
            "a token and for which resource. Refusing to serve unauthenticated."
        )

    server = MCPServer(
        name="agstack-ar2",
        version="2.0.0",
        instructions=INSTRUCTIONS,
        token_verifier=HubTokenVerifier(),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(hub_url),
            resource_server_url=AnyHttpUrl(resource_url),
            # Off, and not by oversight. Turning it on would be correct in
            # isolation and would make this node unusable: the hub issues one
            # general-purpose bearer with no `aud` claim -- /users/login takes
            # a username and a password and nothing else -- so there is no
            # audience to match and every real token is refused. Verified
            # against a live token on 2026-09-20, and terrapipe-os leaves it
            # off for the same reason.
            #
            # The consequence is worth stating plainly rather than leaving
            # for someone to infer: a token obtained to read layers from
            # terrapipe-os also authorises registration here. Narrowing that
            # is a hub change -- audience-scoped tokens -- and until the hub
            # issues them, setting this True only moves the breakage.
            validate_token_resource=False,
        ),
    )

    # -- POST /register-field-boundary -----------------------------------

    @server.tool(
        description=(
            "POST /register-field-boundary. Register a field boundary and get "
            "its Geo Id. wkt is normally POLYGON((lon lat, ...)) with the ring "
            "closed, WGS 84, longitude first. Requires a hub token. "
            "Registering the same ground twice is safe and is the point: an "
            "exact repeat returns the existing Geo Id, and a boundary "
            "overlapping an existing one by at least threshold percent (IoU, "
            "default 95) resolves to it rather than creating a second field. "
            "The message in the reply says which happened, so a caller can tell "
            "a new field from a re-registration. Set automated_field=1 to "
            "record the boundary as machine-derived rather than drawn."
        )
    )
    async def register_field_boundary(
        wkt: str, threshold: int = 95, field_name: str | None = None,
        accuracy_class: str | None = None, submitter: str | None = None,
        s2_index: str | None = None, return_s2_indices: bool = False,
        automated_field: int | None = None, token: str | None = None,
    ) -> dict[str, Any]:
        payload = FieldRegistrationRequest(
            wkt=wkt, threshold=threshold, field_name=field_name,
            accuracy_class=accuracy_class, submitter=submitter,
            s2_index=s2_index, return_s2_indices=return_s2_indices)
        return await _handle(
            registration.register_field_boundary, payload=payload,
            automated_field=automated_field,
            user=_required(token, "registering a boundary"))

    # -- POST /register-field-boundaries-geojson -------------------------

    @server.tool(
        description=(
            "POST /register-field-boundaries-geojson. Register every polygon in "
            "a GeoJSON FeatureCollection in one call, with the same identity "
            "resolution applied to each -- a collection containing the same "
            "ground twice yields one field and two references to it. Requires a "
            "hub token. Pass the collection as JSON in collection. Feature "
            "properties are stored as given: do not put personal names or "
            "national identity numbers in them."
        )
    )
    async def register_field_boundaries_geojson(
        collection: dict[str, Any], automated_field: int | None = None,
        token: str | None = None,
    ) -> Any:
        return await _handle(
            registration.register_field_boundaries_geojson, file=None,
            payload=collection, automated_field=automated_field,
            user=_required(token, "registering boundaries"))

    # -- POST /register-point --------------------------------------------

    @server.tool(
        description=(
            "POST /register-point. Register a plot described by a single "
            "coordinate rather than a boundary, for smallholdings of at most "
            "4 ha (Regulation (EU) 2023/1115 Art. 2(28)); larger plots need a "
            "polygon. wkt is POINT(lon lat). declared_area_ha is the area the "
            "point stands for, which is the only area a point has. Requires a "
            "hub token. " + POINT_CAVEAT
        )
    )
    async def register_point(
        wkt: str, declared_area_ha: float | None = None,
        field_name: str | None = None, s2_index: str | None = None,
        automated_field: int | None = None, token: str | None = None,
    ) -> dict[str, Any]:
        payload = PointRegistrationRequest(
            wkt=wkt, declared_area_ha=declared_area_ha,
            field_name=field_name, s2_index=s2_index)
        return await _handle(
            points.register_point, payload=payload,
            automated_field=automated_field,
            user=_required(token, "registering a point"))

    # -- POST /register-points-geojson -----------------------------------

    @server.tool(
        description=(
            "POST /register-points-geojson. Register every point in a GeoJSON "
            "FeatureCollection in one call. Requires a hub token. " + POINT_CAVEAT
        )
    )
    async def register_points_geojson(
        collection: dict[str, Any], automated_field: int | None = None,
        token: str | None = None,
    ) -> Any:
        return await _handle(
            points.register_points_geojson, file=None, payload=collection,
            automated_field=automated_field,
            user=_required(token, "registering points"))

    # -- GET /fetch-field/{geo_id} and /resolve/{geo_id} ------------------

    @server.tool(
        description=(
            "GET /fetch-field/{geo_id}, also served as /resolve/{geo_id}. What "
            "the registry holds for a Geo Id: its short form, the kind of plot, "
            "its approximate area, and its geometry at whatever tier you are "
            "entitled to. By default you get MaskingLevel L0 and the ~8 km S2 "
            "cell the field lies in, which is a real answer and not an error. "
            "The boundary (L1) requires field_grant, a field-access credential "
            "issued by this field's owner for this field; a hub token on its "
            "own does not lift the mask. Accepts the full Geo Id or the short "
            "form. s2_index takes comma-separated S2 levels, e.g. '13,20'."
        )
    )
    async def fetch_field(
        geo_id: str, s2_index: str | None = None, token: str | None = None,
        field_grant: str | None = None,
    ) -> dict[str, Any]:
        return await _handle(fetch.fetch_field, geo_id=geo_id,
                             s2_index=s2_index, user=_principal(token),
                             x_field_grant=field_grant)

    # -- GET /fetch-field-wkt/{geo_id} ------------------------------------

    @server.tool(
        description=(
            "GET /fetch-field-wkt/{geo_id}. The stored boundary as WKT. This "
            "is the precise location, so it requires field_grant, a "
            "field-access credential issued by this field's owner for this "
            "field. Without one the call succeeds but WKT comes back null at "
            "MaskingLevel L0 -- read the MaskingLevel, do not assume a reply "
            "means you were given the boundary. A hub token does not lift the "
            "mask."
        )
    )
    async def fetch_field_wkt(
        geo_id: str, token: str | None = None, field_grant: str | None = None,
    ) -> dict[str, Any]:
        return await _handle(fetch.fetch_field_wkt, geo_id=geo_id,
                             user=_principal(token), x_field_grant=field_grant)

    # -- GET /fetch-field-centroid/{geo_id} -------------------------------

    @server.tool(
        description=(
            "GET /fetch-field-centroid/{geo_id}. The field's centroid. For an "
            "granted caller this is the true centroid, which is a precise "
            "location: it identifies the farm as surely as the boundary does, "
            "and more compactly. Everyone else gets the masked view. The "
            "precise answer requires field_grant, a field-access credential "
            "for this field; a hub token does not lift the mask."
        )
    )
    async def fetch_field_centroid(
        geo_id: str, token: str | None = None, field_grant: str | None = None,
    ) -> dict[str, Any]:
        return await _handle(fetch.fetch_field_centroid, geo_id=geo_id,
                             user=_principal(token), x_field_grant=field_grant)

    # -- GET /geoid/{geo_id}/eudr-export ----------------------------------

    @server.tool(
        description=(
            "GET /geoid/{geo_id}/eudr-export. The field in the geometry form an "
            "EU Deforestation Regulation Due Diligence Statement expects. "
            "Requires field_grant, a field-access credential for this field, "
            "because a DDS carries the precise plot; unlike the other read "
            "routes this one refuses rather than answering coarsely, since a "
            "masked cell is not a filing. A hub token does not lift the mask. "
            "Returns geometry and identity only; the deforestation finding "
            "that must accompany it comes from a data node."
        )
    )
    async def eudr_export(
        geo_id: str, token: str | None = None, field_grant: str | None = None,
    ) -> dict[str, Any]:
        return await _handle(fetch.eudr_export, geo_id=geo_id,
                             user=_principal(token), x_field_grant=field_grant)

    # -- GET /translate-geoid-to-short/{geo_id} ---------------------------

    @server.tool(
        description=(
            "GET /translate-geoid-to-short/{geo_id}. The short form of a full "
            "64-character Geo Id. Both name the same field; the short form "
            "exists for places the full one will not fit. Discloses nothing "
            "beyond the fact that the field is registered."
        )
    )
    async def translate_geoid_to_short(geo_id: str) -> dict[str, Any]:
        return await _handle(fetch.translate_to_short, geo_id=geo_id)

    # -- GET /translate-geoid-to-full/{geo_id_short} ----------------------

    @server.tool(
        description=(
            "GET /translate-geoid-to-full/{geo_id_short}. The full "
            "64-character Geo Id for a short form. Discloses nothing beyond the "
            "fact that the field is registered."
        )
    )
    async def translate_geoid_to_full(geo_id_short: str) -> dict[str, Any]:
        return await _handle(fetch.translate_to_full, geo_id_short=geo_id_short)

    # -- POST /fetch-fields-for-a-point -----------------------------------

    @server.tool(
        description=(
            "POST /fetch-fields-for-a-point. Which registered fields contain or "
            "lie near a coordinate, in WGS 84. A caller without a hub token is "
            "told a field is there and given its ~8 km cell rather than its "
            "boundary. Use this to find out whether ground is already "
            "registered before registering it. domain, boundary_type and "
            "s2_index narrow the search."
        )
    )
    async def fetch_fields_for_a_point(
        latitude: float, longitude: float, domain: str | None = None,
        boundary_type: str | None = None, s2_index: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        payload = FetchFieldsForPointRequest(
            latitude=latitude, longitude=longitude, domain=domain,
            boundary_type=boundary_type, s2_index=s2_index)
        return await _handle(analytics.fetch_fields_for_a_point,
                             payload=payload, user=_principal(token))

    # -- POST /get-percentage-overlap-two-fields --------------------------

    @server.tool(
        description=(
            "POST /get-percentage-overlap-two-fields. How much two registered "
            "fields overlap, as a percentage by intersection over union. "
            "Answers whether two Geo Ids name the same ground without either "
            "caller holding a boundary. Discloses only the one number."
        )
    )
    async def get_percentage_overlap_two_fields(
        geo_id_field_1: str, geo_id_field_2: str,
    ) -> dict[str, Any]:
        payload = OverlapRequest(geo_id_field_1=geo_id_field_1,
                                 geo_id_field_2=geo_id_field_2)
        return await _handle(analytics.get_percentage_overlap_two_fields,
                             payload=payload)

    # -- GET /fetch-registered-field-count --------------------------------

    @server.tool(
        description=(
            "GET /fetch-registered-field-count. How many fields the registry "
            "holds in total. An aggregate; no individual field is identifiable "
            "from it."
        )
    )
    async def fetch_registered_field_count() -> dict[str, Any]:
        return await _handle(analytics.fetch_registered_field_count)

    # -- GET /fetch-field-count-by-country --------------------------------

    @server.tool(
        description=(
            "GET /fetch-field-count-by-country. Registered field counts broken "
            "down by country. An aggregate; no individual field is identifiable "
            "from it."
        )
    )
    async def fetch_field_count_by_country() -> dict[str, Any]:
        return await _handle(analytics.fetch_field_count_by_country)

    # -- GET /fetch-field-count-by-month ----------------------------------

    @server.tool(
        description=(
            "GET /fetch-field-count-by-month. Registered field counts by month "
            "of registration. An aggregate; no individual field is identifiable "
            "from it."
        )
    )
    async def fetch_field_count_by_month() -> dict[str, Any]:
        return await _handle(analytics.fetch_field_count_by_month)

    # -- POST /populate-country-in-geo-ids --------------------------------

    @server.tool(
        description=(
            "POST /populate-country-in-geo-ids. Maintenance: fill in the "
            "country column for records registered before it was derived. "
            "Operator-facing, idempotent, and it walks the whole table -- do "
            "not call it as part of answering a question."
        )
    )
    async def populate_country_in_geo_ids() -> dict[str, Any]:
        return await _handle(analytics.populate_country_in_geo_ids)

    # -- POST /list-artifact ----------------------------------------------

    @server.tool(
        description=(
            "POST /list-artifact. Record a lot: a set of Geo Ids handled "
            "together at one step of a supply chain, returned as a list_id. "
            "location_geo_id is where the lot was created -- the FSMA 204 "
            "traceability lot code source (21 CFR 1.1330(a)(14)) -- given as a "
            "Geo Id, so a packhouse is identified exactly as a field is. "
            "Omitting it records a gap rather than failing, because a lot with "
            "no recorded creation site is a real state a trace must express. "
            "event_type is harvest, initial_pack, transformation and so on. "
            "Requires a hub token."
        )
    )
    async def register_list_artifact(
        members: list[str], location_geo_id: str | None = None,
        event_type: str | None = None, token: str | None = None,
    ) -> dict[str, Any]:
        payload = RegisterListRequest(
            members=members, location_geo_id=location_geo_id,
            event_type=event_type)
        return await _handle(
            trace.register_list_artifact, payload=payload,
            user=_required(token, "recording a lot"))

    # -- POST /region-artifact --------------------------------------------

    @server.tool(
        description=(
            "POST /region-artifact. Record a region, either as an explicit set "
            "of member Geo Ids or as a WKT boundary enclosing them, returned as "
            "a region_id. Give members or wkt. Requires a hub token."
        )
    )
    async def register_region_artifact(
        members: list[str] | None = None, wkt: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        payload = RegisterRegionRequest(members=members, wkt=wkt)
        return await _handle(
            trace.register_region_artifact, payload=payload,
            user=_required(token, "recording a region"))

    # -- GET /list-artifact/{list_id} -------------------------------------

    @server.tool(
        description=(
            "GET /list-artifact/{list_id}. The members of a lot. Requires a "
            "hub token, and gated beyond that: supply grant_token, a "
            "field-access credential, or authority_token, a credential from a "
            "competent authority. Without one the members are withheld, "
            "because a lot's membership is a statement about whose fields fed "
            "whose shipment."
        )
    )
    async def get_list_artifact(
        list_id: str, grant_token: str | None = None,
        authority_token: str | None = None, token: str | None = None,
    ) -> dict[str, Any]:
        return await _handle(
            trace.get_list_artifact, list_id=list_id,
            x_grant_token=grant_token, x_authority_token=authority_token,
            x_pancake_internal=None,
            user=_required(token, "reading a lot"))

    # -- GET /list-artifact/reverse/{geoid} -------------------------------

    @server.tool(
        description=(
            "GET /list-artifact/reverse/{geoid}. Which lots a given field "
            "appears in. The reverse index behind a recall: given a "
            "contaminated field, the lots that carried its produce. Requires a "
            "hub token. The seed is expanded through its equivalence set "
            "first, so a v1 Geo Id, a superseded alias and the canonical Geo Id "
            "all return the same lots."
        )
    )
    async def get_list_artifact_reverse(
        geoid: str, token: str | None = None,
    ) -> dict[str, Any]:
        return await _handle(
            trace.get_list_artifact_reverse, geoid=geoid,
            user=_required(token, "the reverse lot index"))

    # -- GET /list-artifact/{list_id}/traceback ---------------------------

    @server.tool(
        description=(
            "GET /list-artifact/{list_id}/traceback. Walk a lot backwards to "
            "the fields it came from, following transformations up to "
            "max_depth. Requires a hub token, and gated beyond that like "
            "get_list_artifact: grant_token or authority_token. One hop is one "
            "lot, which is the unit 21 CFR 1.1320(a) records. Answers 'what is "
            "in this shipment'."
        )
    )
    async def trace_back(
        list_id: str, max_depth: int = 10, grant_token: str | None = None,
        authority_token: str | None = None, token: str | None = None,
    ) -> dict[str, Any]:
        return await _handle(
            trace.trace_back, list_id=list_id, max_depth=max_depth,
            x_grant_token=grant_token, x_authority_token=authority_token,
            x_pancake_internal=None,
            user=_required(token, "tracing a lot back"))

    # -- POST /traceforward ------------------------------------------------

    @server.tool(
        description=(
            "POST /traceforward. From a field, the lots its produce entered -- "
            "the direction a recall travels. seed_geoid is the field. Requires "
            "a hub token carrying the trace-forward capability, or a "
            "grant_token. With authority_token, scope is required. The reply is "
            "tiered: without the standing to see holders you get the count and "
            "the list ids, not who holds them, which is enough to know a recall "
            "is warranted without learning a competitor's customers."
        )
    )
    async def run_traceforward(
        seed_geoid: str, scope: str | None = None,
        grant_token: str | None = None, authority_token: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        payload = TraceForwardRequest(seed_geoid=seed_geoid, scope=scope)
        return await _handle(
            trace.run_traceforward, payload=payload, request=None,
            x_grant_token=grant_token, x_authority_token=authority_token,
            user=_required(token, "trace-forward"))

    # -- the one addition: asking a person ---------------------------------

    @server.tool(
        description=(
            "Ask the human operating this session for context about a "
            "registration. Not an AR2 endpoint: the registry stores what it is "
            "given and has no way to ask where it came from. Use before "
            "registering on someone's behalf, when you need a field name, how "
            "the boundary was obtained, a declared area, or a decision about an "
            "overlap the registry is about to resolve -- 'this boundary "
            "overlaps an existing field by 96%, register as the same field?'. "
            "Returns what the person supplied, with proceed=false if they "
            "declined. If the client does not support elicitation this returns "
            "supported=false and you should ask in conversation instead."
        )
    )
    async def elicit_context(question: str, ctx: Context) -> dict[str, Any]:
        return await ask_human(question, ctx)

    return server


async def ask_human(question: str, ctx: Any) -> dict[str, Any]:
    """Put a question to the person running the session.

    Separate from the tool so it can be tested without an MCP client: the
    tool body is a one-line delegation and this is where the behaviour is.

    Three outcomes, and they are deliberately distinct. A client that cannot
    elicit is not an error -- an agent told "error" retries, an agent told
    supported=false asks in conversation instead. A person declining is a
    real answer and not a failure to get one.
    """
    if ctx is None:
        return {"supported": False,
                "why": "no request context available on this transport"}
    try:
        result = await ctx.elicit(message=question, schema=FieldContext)
    except Exception as exc:  # noqa: BLE001
        log.info("elicitation unavailable: %s", exc)
        return {"supported": False, "why": str(exc)}
    action = getattr(result, "action", None)
    if action != "accept":
        return {"supported": True, "proceed": False, "action": action}
    data = getattr(result, "data", None)
    answered = data.model_dump(exclude_none=True) if data else {}
    return {"supported": True, "answered": answered,
            "proceed": answered.get("proceed", True)}


#: Routers whose every route must be mirrored by exactly one tool.
MIRRORED_ROUTERS = (analytics, fetch, registration, points, trace)

#: Which tool serves which route. Keyed on the route, because the route is
#: the API: two of AR2's handler functions are named differently from the
#: paths they serve (``translate_to_short`` serves
#: ``/translate-geoid-to-short``), and a mirror anchored on function names
#: would have quietly demanded the wrong tool names.
#:
#: Two paths may share a value where AR2 serves one operation at two URLs.
#: That is an alias, and writing it out is how it stays visible.
ROUTE_TOOLS = {
    "POST /register-field-boundary": "register_field_boundary",
    "POST /register-field-boundaries-geojson": "register_field_boundaries_geojson",
    "POST /register-point": "register_point",
    "POST /register-points-geojson": "register_points_geojson",
    "GET /fetch-field/{geo_id}": "fetch_field",
    "GET /resolve/{geo_id}": "fetch_field",  # alias for the same operation
    "GET /fetch-field-wkt/{geo_id}": "fetch_field_wkt",
    "GET /fetch-field-centroid/{geo_id}": "fetch_field_centroid",
    "GET /geoid/{geo_id}/eudr-export": "eudr_export",
    "GET /translate-geoid-to-short/{geo_id}": "translate_geoid_to_short",
    "GET /translate-geoid-to-full/{geo_id_short}": "translate_geoid_to_full",
    "GET /fetch-registered-field-count": "fetch_registered_field_count",
    "GET /fetch-field-count-by-country": "fetch_field_count_by_country",
    "GET /fetch-field-count-by-month": "fetch_field_count_by_month",
    "POST /get-percentage-overlap-two-fields": "get_percentage_overlap_two_fields",
    "POST /fetch-fields-for-a-point": "fetch_fields_for_a_point",
    "POST /populate-country-in-geo-ids": "populate_country_in_geo_ids",
    "POST /list-artifact": "register_list_artifact",
    "POST /region-artifact": "register_region_artifact",
    "GET /list-artifact/{list_id}": "get_list_artifact",
    "GET /list-artifact/reverse/{geoid}": "get_list_artifact_reverse",
    "GET /list-artifact/{list_id}/traceback": "trace_back",
    "POST /traceforward": "run_traceforward",
}

#: The single tool that mirrors no route, and why it is allowed to exist.
#: Anything else on the surface without a route fails the mirror test.
NON_API_TOOLS = {
    "elicit_context": "asks the human for context the registry cannot infer",
}
