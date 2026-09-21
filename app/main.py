# Licensed under the EUPL, Version 1.2 or – as soon they will be approved by
# the European Commission - subsequent versions of the EUPL (the "Licence");
# You may not use this work except in compliance with the Licence.
# You may obtain a copy of the Licence at:
# https://joinup.ec.europa.eu/software/page/eupl

import contextlib
import logging
import os

from dotenv import load_dotenv

load_dotenv()
load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"))

from fastapi import FastAPI

from app.database import Base, engine
from app.routers import (
    analytics_and_maintenance_apis,
    fetch_field,
    field_registration,
    point_registration,
    traceforward,
)

Base.metadata.create_all(bind=engine)

log = logging.getLogger(__name__)


def _build_mcp_app():
    """The MCP surface, or None if this deployment has not configured it.

    Returned rather than mounted here so the failure is visible as a value.
    The REST service predates the agent surface and must keep starting
    without it: a node upgraded before HUB_URL and AR2_MCP_URL are set should
    lose the tools, not the registry. Any failure to build is logged and
    swallowed for that reason, and for that reason only -- once mounted, the
    surface authenticates or refuses, it never degrades.
    """
    try:
        from app.mcp_server import build_mcp

        # The transport path is set inside the sub-app rather than by where
        # it is mounted, so the two routes it publishes land on their real
        # URLs: /mcp, and the RFC 9728 protected-resource document at
        # /.well-known/oauth-protected-resource/mcp. That document has to be
        # at the site root -- an agent reads it to discover where to get a
        # token -- and mounting the sub-app under a prefix would bury it at
        # /mcp/.well-known/..., where no client looks.
        return build_mcp().streamable_http_app(streamable_http_path="/mcp")
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "MCP surface not mounted (%s). The REST API is unaffected. "
            "Set HUB_URL and AR2_MCP_URL to enable it.", exc)
        return None


_mcp_app = _build_mcp_app()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run the MCP session manager alongside the service.

    A Starlette app mounted into FastAPI does not get its lifespan run by the
    parent, and the streamable-HTTP transport keeps its session state there.
    Without this the mount answers the first request and then fails on the
    session lookup, which looks like an intermittent transport bug rather
    than a missing startup hook.
    """
    if _mcp_app is None:
        yield
        return
    async with _mcp_app.router.lifespan_context(_mcp_app):
        yield


app = FastAPI(
    title="Asset Registry 2.0",
    description="FastAPI rewrite of the Terrapipe Asset Registry",
    version="2.0.0",
    lifespan=lifespan,
)

# router
app.include_router(field_registration.router)
app.include_router(fetch_field.router)
app.include_router(point_registration.router)
app.include_router(analytics_and_maintenance_apis.router)
app.include_router(traceforward.router)

@app.get("/")
async def root():
    return {"message": "Welcome to Asset Registry 2.0 APIs"}


if _mcp_app is not None:
    # Mounted at the root and appended last, so every AR2 route above is
    # matched first and only unclaimed paths reach the MCP app. Mounting
    # rather than copying its two routes across is deliberate: the bearer
    # authentication is Starlette middleware on the sub-app, not on the
    # routes, so lifting the routes out would leave the transport reachable
    # with no token at all.
    from starlette.routing import Mount

    app.router.routes.append(Mount("/", app=_mcp_app))