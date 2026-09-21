# Licensed under the EUPL, Version 1.2 or – as soon they will be approved by
# the European Commission - subsequent versions of the EUPL (the "Licence");
# You may not use this work except in compliance with the Licence.
# You may obtain a copy of the Licence at:
# https://joinup.ec.europa.eu/software/page/eupl

"""Emit the route-to-tool map as markdown.

Generated rather than written, because a hand-kept table of which tool serves
which endpoint is a second copy of ROUTE_TOOLS, and the two would agree for
about a week. ``twin-divergence`` is six occurrences in this repository's
ledger; a documentation table is the cheapest possible way to earn a seventh.

Everything in the output is read from the code: the routes from the routers,
the tool names from ``ROUTE_TOOLS``, whether a token is required from the
handler's ``Depends``, and whether a grant is honoured from the handler's
signature. The one curated column is "notes", and it is kept to things the
code cannot say about itself.

    python3 scripts/mcp_surface.py            # print
    python3 scripts/mcp_surface.py --write    # update docs/MCP_SURFACE.md

``test_mcp_surface.py`` regenerates and compares, so a stale file is a test
failure rather than something a reader has to notice.
"""

from __future__ import annotations

import argparse
import inspect
import re
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# build_mcp refuses to construct without these; the values are irrelevant to
# the shape of the surface and never leave this process.
os.environ.setdefault("HUB_URL", "http://hub.invalid")
os.environ.setdefault("AR2_MCP_URL", "http://ar2.invalid/mcp")

DOC = ROOT / "docs" / "MCP_SURFACE.md"

# What the code cannot say about itself. Keyed by tool so that renaming a
# route does not silently drop the note.
NOTES = {
    "register_field_boundary":
        "Identity resolution: an exact repeat returns the existing Geo Id; an "
        "overlap at or above `threshold` percent IoU (default 95) resolves to "
        "the existing field instead of making a second one.",
    "register_field_boundaries_geojson":
        "One call for a whole FeatureCollection. Feature properties are stored "
        "as supplied, so they must not carry names or national ID numbers.",
    "register_point":
        "AG-034 open: the returned Geo Id inverts to the coordinate. Caveat is "
        "in the tool description and tested for.",
    "register_points_geojson":
        "AG-034 open, as above.",
    "fetch_field":
        "Also served at `/resolve/{geo_id}`; one operation, one tool. Accepts "
        "the full or short Geo Id.",
    "fetch_field_wkt":
        "Without a grant this returns HTTP 200 with `WKT: null` at L0 rather "
        "than refusing. Read `MaskingLevel`, not the status code.",
    "fetch_field_centroid":
        "A centroid is a precise location: it identifies the farm as surely as "
        "the boundary, and more compactly.",
    "eudr_export":
        "The only read route that refuses without a grant rather than "
        "answering coarsely, because a masked cell is not a filing.",
    "get_percentage_overlap_two_fields":
        "Answers whether two Geo Ids name the same ground while disclosing "
        "only the one number.",
    "fetch_fields_for_a_point":
        "Reverse lookup. Use before registering, to find out whether the "
        "ground already has an identifier.",
    "populate_country_in_geo_ids":
        "Maintenance. Walks the whole table; not for answering a question.",
    "register_list_artifact":
        "FSMA 204 lot. `location_geo_id` is the traceability lot code source "
        "(21 CFR 1.1330(a)(14)); omitting it records a gap rather than failing.",
    "get_list_artifact":
        "Gated twice: a hub token to call at all, then a grant or authority "
        "credential to see the members.",
    "trace_back":
        "One hop is one lot, which is the unit 21 CFR 1.1320(a) records.",
    "run_traceforward":
        "Needs the `trace-forward` capability in the hub token, or a grant. "
        "Tiered: without standing to see holders you get counts and list ids.",
    "elicit_context":
        "Not an AR2 route. The one permitted addition: asks the operator for "
        "context the registry has no way to infer.",
}


def _handlers() -> dict[str, object]:
    from app import mcp_server

    found = {}
    for module in mcp_server.MIRRORED_ROUTERS:
        for route in module.router.routes:
            for method in route.methods - {"HEAD", "OPTIONS"}:
                found[f"{method} {route.path}"] = route.endpoint
    return found


def _auth_of(handler) -> str:
    """What the HTTP route demands before it will answer at all."""
    from app.auth import get_current_user, require_hub_user

    level = "none"
    for param in inspect.signature(handler).parameters.values():
        dependency = getattr(param.default, "dependency", None)
        if dependency is require_hub_user:
            return "hub token"
        if dependency is get_current_user:
            level = "optional"
    return level


def _grant_of(handler) -> str:
    """Whether the handler reads a field-access grant, and what it does.

    The distinction between "degrades to L0" and "refuses" is the one a
    caller most needs and the one easiest to get wrong: the first draft of
    this function looked for `raise HTTPException` anywhere after `has_l1`
    and matched the generic 400 handler at the bottom of every route, so it
    printed "refuses without" for `fetch_field`, which does no such thing.
    A wrong fact in a generated table is worse than a missing column.

    So: match the refusal exactly -- `if not has_l1:` immediately followed by
    a raise. Asserted against all four gated routes in the test.
    """
    if "x_field_grant" not in inspect.signature(handler).parameters:
        return "--"
    source = inspect.getsource(handler)
    if "verify_field_grant" not in source:
        return "accepted, not checked"
    refuses = re.search(r"if not has_l1:\s*\n\s*raise\b", source)
    return "refuses without" if refuses else "lifts mask to L1"


def rows() -> list[dict]:
    from app import mcp_server

    handlers = _handlers()
    out = []
    for route in sorted(handlers, key=lambda r: (r.split()[1], r)):
        handler = handlers[route]
        tool = mcp_server.ROUTE_TOOLS.get(route, "MISSING")
        out.append({
            "route": route,
            "tool": tool,
            "auth": _auth_of(handler),
            "grant": _grant_of(handler),
            "notes": NOTES.get(tool, ""),
        })
    return out


def render() -> str:
    from app import mcp_server

    served = rows()
    tooled = {r["tool"] for r in served}
    extra = sorted(set(mcp_server.NON_API_TOOLS) - tooled)

    lines = [
        "# The AR2 MCP surface",
        "",
        "Generated by `scripts/mcp_surface.py`. Do not edit: "
        "`test_mcp_surface.py` regenerates this file and fails if it differs, "
        "so an edit here is reverted by the next run rather than believed.",
        "",
        "The agent surface mirrors the HTTP API exactly -- one tool per route, "
        "nothing composed, nothing withheld. A tool that has no route, or a "
        "route that has no tool, fails a test. The single exception is "
        "declared in `NON_API_TOOLS` and listed at the bottom.",
        "",
        "Endpoint: `POST /mcp`, streamable HTTP, bearer token from the hub. "
        "Discovery at `/.well-known/oauth-protected-resource/mcp`.",
        "",
        "## Columns",
        "",
        "- **auth** -- what the route demands before answering at all. "
        "`hub token` is `require_hub_user`; `optional` means the route answers "
        "either way and the answer differs; `none` means no principal is read.",
        "- **grant** -- what a field-access credential (`field_grant`, the "
        "`X-Field-Grant` header over HTTP) does on this route. This is the "
        "column that matters: a hub token does **not** lift the disclosure "
        "mask on any read route.",
        "",
        "## Routes and tools",
        "",
        "| HTTP route | MCP tool | auth | grant | notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in served:
        lines.append(
            f"| `{row['route']}` | `{row['tool']}` | {row['auth']} "
            f"| {row['grant']} | {row['notes']} |")

    lines += ["", f"{len(served)} routes, "
                  f"{len({r['tool'] for r in served})} tools.", ""]

    if extra:
        lines += ["## Not a route", "",
                  "| MCP tool | why it is allowed |", "| --- | --- |"]
        for name in extra:
            lines.append(f"| `{name}` | {mcp_server.NON_API_TOOLS[name]} |")
        lines.append("")

    lines += [
        "## Known limitation",
        "",
        "Audience validation is off. The hub issues one general-purpose bearer "
        "with no `aud` claim, so a token obtained to read layers from "
        "terrapipe-os also authorises registration here. Turning validation on "
        "without a hub change refuses every existing token. The fix is "
        "audience-scoped hub tokens.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help=f"update {DOC.relative_to(ROOT)}")
    args = parser.parse_args()
    text = render()
    if args.write:
        DOC.parent.mkdir(parents=True, exist_ok=True)
        DOC.write_text(text)
        print(f"wrote {DOC.relative_to(ROOT)}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
