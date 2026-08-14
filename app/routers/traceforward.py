import hashlib
import os

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_hub_user
from app.database import get_db
from app.merkle import canonical_members, merkle_root
from app.models.geo_id_model import (
    GeoID,
    GeoIDAlias,
    GeoIDRegimeAlias,
    ListArtifact,
    ListMemberEdge,
    ListParentEdge,
    RegionArtifact,
    RegionCoverCell,
    RegionParentEdge,
)

router = APIRouter(tags=["Trace Forward"])

class RegisterListRequest(BaseModel):
    members: list[str]
    # Where this lot is being created: the FSMA 204 traceability lot code source
    # (21 CFR 1.1330(a)(14), 1.1350(a)(2)(ii)). A GeoID, so a packhouse or plant
    # is identified exactly as a field is. Optional -- omitting it records a gap
    # rather than failing, because a lot with no recorded creation site is a real
    # state and the trace has to be able to say so.
    location_geo_id: str | None = None
    event_type: str | None = None   # harvest | initial_pack | transformation | ...

class RegisterListResponse(BaseModel):
    list_id: str
    message: str

class RegisterRegionRequest(BaseModel):
    members: list[str] | None = None
    wkt: str | None = None

class RegisterRegionResponse(BaseModel):
    region_id: str
    message: str

@router.post("/list-artifact", response_model=RegisterListResponse)
def register_list_artifact(
    payload: RegisterListRequest,
    user: dict = Depends(require_hub_user),
    db: Session = Depends(get_db)
):
    """
    Registers a content-derived list artifact in the AR2 node.
    This creates the ListArtifact and the reverse edges for members.
    """
    if not payload.members:
        raise HTTPException(status_code=400, detail="List must contain at least one member")

    canonical = canonical_members(payload.members)
    list_id = merkle_root(canonical)

    existing = db.execute(select(ListArtifact).where(ListArtifact.list_id == list_id)).scalar_one_or_none()
    if existing:
        return RegisterListResponse(list_id=list_id, message="ListArtifact already exists")

    # The location is metadata, deliberately NOT folded into the Merkle root, so
    # existing ListIDs keep their values and a caller can add a location to a
    # lot code that already exists. The consequence is that two packings of the
    # identical field set at two sites share one ListID; if that needs to be two
    # lots, the location has to enter the root and every existing ListID changes.
    # Recorded as an open decision rather than settled here.
    new_artifact = ListArtifact(
        list_id=list_id,
        location_geo_id=payload.location_geo_id,
        event_type=payload.event_type,
    )
    db.add(new_artifact)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return RegisterListResponse(list_id=list_id, message="ListArtifact already exists")

    # Create edges
    for member in canonical:
        if member.startswith("L:"):
            edge = ListParentEdge(child_list_id=member[2:], parent_list_id=list_id)
            db.add(edge)
        elif member.startswith("R:"):
            edge = RegionParentEdge(child_region_id=member[2:], parent_list_id=list_id)
            db.add(edge)
        else:
            edge = ListMemberEdge(geoid=member, list_id=list_id)
            db.add(edge)

    db.commit()

    return RegisterListResponse(list_id=list_id, message="ListArtifact registered successfully")

@router.post("/region-artifact", response_model=RegisterRegionResponse)
def register_region_artifact(
    payload: RegisterRegionRequest,
    user: dict = Depends(require_hub_user),
    db: Session = Depends(get_db)
):
    """
    Registers a content-derived Region artifact. 
    It accepts a list of ListIDs or GeoIDs, fetches their S2 cells, and computes the RegionID.
    """
    if not payload.members and not payload.wkt:
        raise HTTPException(status_code=400, detail="Region must have at least one member or a wkt boundary")

    all_geoids = set()
    if payload.members:
        frontier = set(payload.members)
        visited = set()
        
        while frontier:
            member = frontier.pop()
            if member in visited:
                continue
            visited.add(member)
            
            actual_list_id = member.removeprefix("L:")
            
            is_list = db.execute(select(ListArtifact).where(ListArtifact.list_id == actual_list_id)).scalar_one_or_none()
            if is_list:
                list_geoids = db.execute(select(ListMemberEdge.geoid).where(ListMemberEdge.list_id == actual_list_id)).scalars().all()
                all_geoids.update(list_geoids)
                
                child_lists = db.execute(select(ListParentEdge.child_list_id).where(ListParentEdge.parent_list_id == actual_list_id)).scalars().all()
                frontier.update([f"L:{child}" for child in child_lists])
            else:
                all_geoids.add(member)

    s2_cells = set()
    if all_geoids:
        geoid_records = db.execute(select(GeoID.s2_cells).where(GeoID.geo_id.in_(all_geoids))).scalars().all()
        for cells in geoid_records:
            if cells:
                s2_cells.update(cells)
                
    if payload.wkt:
        from app.s2_services import S2Service
        wkt_cells = S2Service.wkt_to_cell_tokens(payload.wkt, 13)
        s2_cells.update(wkt_cells)
            
    if not s2_cells:
        raise HTTPException(status_code=400, detail="No S2 cells found for the provided members or wkt")

    import s2sphere as s2
    cell_ids = [s2.CellId.from_token(token) for token in s2_cells]
    cell_union = s2.CellUnion(cell_ids)
    cell_union.normalize()

    sorted_cells = sorted([cid.to_token() for cid in cell_union.cell_ids()])
    m = hashlib.sha256()
    for cell in sorted_cells:
        m.update(b"|")
        m.update(cell.encode("utf-8"))
    region_id = m.hexdigest()

    existing = db.execute(select(RegionArtifact).where(RegionArtifact.region_id == region_id)).scalar_one_or_none()
    if existing:
        return RegisterRegionResponse(region_id=region_id, message="RegionArtifact already exists")

    new_region = RegionArtifact(region_id=region_id)
    db.add(new_region)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return RegisterRegionResponse(region_id=region_id, message="RegionArtifact already exists")

    for cell in sorted_cells:
        db.add(RegionCoverCell(region_id=region_id, s2_cell=cell))
        
    db.commit()
    
    return RegisterRegionResponse(region_id=region_id, message="RegionArtifact registered successfully")

class ListMembersResponse(BaseModel):
    list_id: str
    members: list[str]

import hmac


def _is_trusted_internal(token: str | None) -> bool:
    expected = os.getenv("AR2_INTERNAL_SHARED_SECRET")
    return bool(expected) and bool(token) and hmac.compare_digest(token, expected)

@router.get("/list-artifact/{list_id}", response_model=ListMembersResponse)
def get_list_artifact(
    list_id: str,
    x_grant_token: str | None = Header(None),
    x_authority_token: str | None = Header(None),
    x_pancake_internal: str | None = Header(None),
    user: dict = Depends(require_hub_user),
    db: Session = Depends(get_db)
):
    if not _is_trusted_internal(x_pancake_internal):
        from app.auth import authorize_artifact
        from app.meal_logger import log_traceback
        auth_result = authorize_artifact(x_grant_token, x_authority_token, list_id=list_id, raise_404_on_fail=True)
        if auth_result.get("used_authority"):
            log_traceback(user.get("sub"), auth_result.get("authority_jti"), list_id)

    """
    Retrieves the members of a content-derived list artifact from the AR2 node.
    """
    existing = db.execute(select(ListArtifact).where(ListArtifact.list_id == list_id)).scalar_one_or_none()
    if not existing:
        raise HTTPException(status_code=404, detail="ListArtifact not found")

    members = db.execute(select(ListMemberEdge.geoid).where(ListMemberEdge.list_id == list_id)).scalars().all()
    child_lists = db.execute(select(ListParentEdge.child_list_id).where(ListParentEdge.parent_list_id == list_id)).scalars().all()
    child_regions = db.execute(select(RegionParentEdge.child_region_id).where(RegionParentEdge.parent_list_id == list_id)).scalars().all()
    
    all_members = list(members) + [f"L:{child}" for child in child_lists] + [f"R:{child}" for child in child_regions]
    return ListMembersResponse(list_id=list_id, members=canonical_members(all_members))

class ListReverseResponse(BaseModel):
    list_ids: list[str]

@router.get("/list-artifact/reverse/{geoid}", response_model=ListReverseResponse)
def get_list_artifact_reverse(
    geoid: str,
    user: dict = Depends(require_hub_user),
    db: Session = Depends(get_db)
):
    """
    Retrieves all list IDs that contain the given geoid.

    Expands the seed through its equivalence set first, so a v1 GeoID, a
    superseded content-hash alias and the canonical GeoID all return the same
    lists. Without this the reverse lookup silently disagrees with
    /traceforward about what counts as the same field.
    """
    seeds = _equivalence_set(db, geoid)
    list_ids = db.execute(
        select(ListMemberEdge.list_id).where(ListMemberEdge.geoid.in_(seeds))
    ).scalars().all()
    return ListReverseResponse(list_ids=sorted(set(list_ids)))


# --------------------------------------------------------------------------
# Trace-back: one hop per lot.
#
# 21 CFR 1.1320(a) assigns a new traceability lot code at exactly three events --
# initial packing, first land-based receiving, and transformation. Each is the
# creation of a lot, and a ListArtifact is created at exactly those moments. So
# one list is one lot is one hop, and the hop is the regulation's own unit of
# record rather than an artifact of how we happen to traverse.
#
# Each hop reports where its lot was created (the traceability lot code source,
# 1.1330(a)(14) and 1.1350(a)(2)(ii)) as a GeoID, and which lots fed into it
# (1.1350(a)(1) requires the new lot to be linked to each input lot). Flattening
# to a set of GeoIDs would answer "which fields" while destroying "by what
# route", and the route is what lets an investigator narrow a recall.
# --------------------------------------------------------------------------

MAX_TRACEBACK_DEPTH = 64


class TraceBackEdge(BaseModel):
    parent_list_id: str
    child_id: str
    kind: str            # "list" | "region" | "geoid"


class TraceBackHop(BaseModel):
    """One lot: where it was created, what was in it, what fed it."""
    list_id: str
    depth: int
    event_type: str | None = None
    location_geo_id: str | None = None
    location_recorded: bool = False
    geoids: list[str] = []               # fields directly in this lot
    input_list_ids: list[str] = []       # lots consumed to make this one
    input_region_ids: list[str] = []
    created_at: str | None = None


class TraceBackResponse(BaseModel):
    root_list_id: str
    hops: list[TraceBackHop]
    edges: list[TraceBackEdge]
    geoids: list[str]                    # transitive closure: fields to inspect
    locations: list[str] = []            # the chain of places, in hop order
    hops_missing_location: list[str] = []
    region_ids: list[str] = []
    # True when any hop cited a region. The field set is then a lower bound: the
    # region's own fields are resolved spatially by /traceforward and are not
    # read back here. Callers must expand the regions to complete the trace.
    regions_unexpanded: bool = False
    max_depth: int
    truncated: bool = False


@router.get("/list-artifact/{list_id}/traceback", response_model=TraceBackResponse)
def trace_back(
    list_id: str,
    max_depth: int = MAX_TRACEBACK_DEPTH,
    x_grant_token: str | None = Header(None),
    x_authority_token: str | None = Header(None),
    x_pancake_internal: str | None = Header(None),
    user: dict = Depends(require_hub_user),
    db: Session = Depends(get_db),
):
    """Walk downward from a list artifact to the fields, one hop per step.

    Authorization is identical to GET /list-artifact/{list_id}: a grant scoped to
    the list, or an authority credential. Authority use is written to the MEAL
    ledger, because a regulator reading a whole supply chain is exactly the event
    that must be auditable.
    """
    if not _is_trusted_internal(x_pancake_internal):
        from app.auth import authorize_artifact
        from app.meal_logger import log_traceback
        auth_result = authorize_artifact(
            x_grant_token, x_authority_token, list_id=list_id, raise_404_on_fail=True
        )
        if auth_result.get("used_authority"):
            log_traceback(user.get("sub"), auth_result.get("authority_jti"), list_id)

    root = db.execute(
        select(ListArtifact).where(ListArtifact.list_id == list_id)
    ).scalar_one_or_none()
    if not root:
        raise HTTPException(status_code=404, detail="ListArtifact not found")

    depth_cap = max(1, min(max_depth, MAX_TRACEBACK_DEPTH))

    hops: list[TraceBackHop] = []
    edges: list[TraceBackEdge] = []
    all_geoids: set[str] = set()
    all_regions: set[str] = set()
    truncated = False

    # Breadth-first over lots. A lot reached by two routes is reported once, at
    # the shallowest depth it was reached. Cycles cannot arise through a
    # content-derived ListID -- a lot would have to contain its own root -- but a
    # corrupt edge must not be able to hang the node.
    frontier = [list_id]
    seen: set[str] = {list_id}

    for depth in range(depth_cap):
        if not frontier:
            break

        artifacts = {
            a.list_id: a
            for a in db.execute(
                select(ListArtifact).where(ListArtifact.list_id.in_(frontier))
            ).scalars().all()
        }
        members = db.execute(
            select(ListMemberEdge.list_id, ListMemberEdge.geoid)
            .where(ListMemberEdge.list_id.in_(frontier))
        ).all()
        child_lists = db.execute(
            select(ListParentEdge.parent_list_id, ListParentEdge.child_list_id)
            .where(ListParentEdge.parent_list_id.in_(frontier))
        ).all()
        child_regions = db.execute(
            select(RegionParentEdge.parent_list_id, RegionParentEdge.child_region_id)
            .where(RegionParentEdge.parent_list_id.in_(frontier))
        ).all()

        by_lot_geoids: dict[str, set[str]] = {}
        by_lot_inputs: dict[str, set[str]] = {}
        by_lot_regions: dict[str, set[str]] = {}

        for parent, geoid in members:
            by_lot_geoids.setdefault(parent, set()).add(geoid)
            edges.append(TraceBackEdge(parent_list_id=parent, child_id=geoid, kind="geoid"))
        for parent, child in child_lists:
            by_lot_inputs.setdefault(parent, set()).add(child)
            edges.append(TraceBackEdge(parent_list_id=parent, child_id=child, kind="list"))
        for parent, region in child_regions:
            by_lot_regions.setdefault(parent, set()).add(region)
            edges.append(TraceBackEdge(parent_list_id=parent, child_id=region, kind="region"))

        # One hop per lot, not one per depth level: a depth level can hold several
        # unrelated lots, which is a fact about the traversal and not about the
        # supply chain.
        for lot in sorted(frontier):
            artifact = artifacts.get(lot)
            location = artifact.location_geo_id if artifact else None
            hops.append(TraceBackHop(
                list_id=lot,
                depth=depth,
                event_type=artifact.event_type if artifact else None,
                location_geo_id=location,
                location_recorded=location is not None,
                geoids=sorted(by_lot_geoids.get(lot, set())),
                input_list_ids=sorted(by_lot_inputs.get(lot, set())),
                input_region_ids=sorted(by_lot_regions.get(lot, set())),
                created_at=artifact.created_at.isoformat() if artifact and artifact.created_at else None,
            ))

        all_geoids |= {g for _, g in members}
        all_regions |= {r for _, r in child_regions}

        next_frontier = {c for _, c in child_lists} - seen
        frontier = sorted(next_frontier)
        seen |= next_frontier

        if frontier and depth == depth_cap - 1:
            truncated = True

    # Regions are reported, not expanded into fields. Region membership is
    # spatial: /traceforward matches a field to a region by testing the field's
    # S2 cells and their ancestors against RegionCoverCell, and a region has no
    # membership rows to read back. Inverting that is a prefix query over covers,
    # not a lookup, so it is deliberately not attempted here -- and a lot that
    # cites a region therefore has a field set we cannot claim is complete.
    # Saying so is the point: a partial answer that looks total is the failure
    # mode that makes an investigator under-scope a recall.
    return TraceBackResponse(
        root_list_id=list_id,
        hops=hops,
        edges=edges,
        geoids=sorted(all_geoids),
        locations=[h.location_geo_id for h in hops if h.location_geo_id],
        hops_missing_location=[h.list_id for h in hops if not h.location_recorded],
        region_ids=sorted(all_regions),
        regions_unexpanded=bool(all_regions),
        max_depth=max((h.depth for h in hops), default=0),
        truncated=truncated,
    )


class TraceForwardRequest(BaseModel):
    seed_geoid: str
    scope: str | None = None

from typing import Literal


class TraceMatch(BaseModel):
    list_id: str
    holder_account: str | None = None
    resolution: Literal["match", "contained", "no-match"] = "match"
    holder_status: Literal["resolved", "unresolved"] | None = None

class TraceForwardResponse(BaseModel):
    seed_geoid: str
    tier: int
    match_count: int
    list_ids: list[str]
    matches: list[TraceMatch] | None = None

def _resolve_holders(list_ids: set[str], authority_token: str, scope: str, seed_geoid: str) -> dict[str, str]:
    """Tier-3 only: ask Pancake who holds each matched artifact."""
    pancake_url = os.getenv("PANCAKE_URL", "http://localhost:8100")
    internal_secret = os.getenv("AR2_INTERNAL_SHARED_SECRET", "")
    try:
        resp = httpx.post(f"{pancake_url}/fieldlists/holders",
                          json={"list_ids": sorted(list_ids), "scope": scope, "seed_geoid": seed_geoid},
                          headers={
                              "X-Pancake-Internal": internal_secret,
                              "X-Authority-Token": authority_token
                          }, timeout=10)
        resp.raise_for_status()
        return resp.json().get("holders", {})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"holder resolution failed: {e}")

def _equivalence_set(db: Session, seed: str) -> set[str]:
    """Seed + every GeoID that denotes the same physical field.

    Three sources, and all three are needed:

      * GeoIDAlias, keyed on content hash: two submissions of one field.
      * GeoIDRegimeAlias, keyed on the v1 identifier: the same field before and
        after the v2 regime change. A trace seeded with an identifier AR 1.0
        issued years ago must still reach the field, and those identifiers are
        still in circulation.
      * The reverse of each, so the set is the same whichever member you start
        from. Asymmetry here would mean the answer depended on which identifier
        the caller happened to hold.

    Only same_as is followed. child_of is a containment relation, not an identity
    one, and collapsing it here would silently widen every trace to the parent.
    """
    canonical = db.execute(
        select(GeoIDAlias.canonical_geo_id)
        .where(GeoIDAlias.alias_content_hash == seed, GeoIDAlias.relation == "same_as")
    ).scalars().all()

    # v1 seed -> its v2 identity, and a v2 seed -> every v1 identifier for it.
    v1_to_v2 = db.execute(
        select(GeoIDRegimeAlias.v2_geo_id)
        .where(GeoIDRegimeAlias.v1_geo_id == seed, GeoIDRegimeAlias.relation == "same_as")
    ).scalars().all()

    roots = {seed, *canonical, *v1_to_v2}

    aliases = db.execute(
        select(GeoIDAlias.alias_content_hash)
        .where(GeoIDAlias.canonical_geo_id.in_(roots), GeoIDAlias.relation == "same_as")
    ).scalars().all()
    v2_to_v1 = db.execute(
        select(GeoIDRegimeAlias.v1_geo_id)
        .where(GeoIDRegimeAlias.v2_geo_id.in_(roots), GeoIDRegimeAlias.relation == "same_as")
    ).scalars().all()

    return roots | set(aliases) | set(v2_to_v1)

@router.post("/traceforward", response_model=TraceForwardResponse, response_model_exclude_none=True)
def run_traceforward(
    payload: TraceForwardRequest,
    request: Request,
    x_grant_token: str | None = Header(None),
    x_authority_token: str | None = Header(None),
    user: dict = Depends(require_hub_user),
    db: Session = Depends(get_db)
):
    """
    Tiered trace-forward with Gate A/B checks.
    """
    capabilities = user.get("capabilities", [])
    if "trace-forward" not in capabilities and not x_grant_token:
        raise HTTPException(status_code=403, detail="Hub capabilities missing trace-forward")

    if x_authority_token and not payload.scope:
        raise HTTPException(status_code=400, detail="scope is required with an authority credential")

    from app.auth import authorize_artifact
    from app.meal_logger import log_traceforward
    
    auth_result = authorize_artifact(
        grant_token=x_grant_token,
        authority_token=x_authority_token,
        geoid=payload.seed_geoid,
        scope=payload.scope,
        raise_404_on_fail=False
    )

    seeds = _equivalence_set(db, payload.seed_geoid)

    direct_list_ids = set(db.execute(select(ListMemberEdge.list_id).where(ListMemberEdge.geoid.in_(seeds))).scalars().all())

    intersecting_regions = set()
    geoid_records = db.execute(select(GeoID).where(GeoID.geo_id.in_(seeds))).scalars().all()
    if geoid_records:
        import s2sphere as s2
        probe_tokens = set()
        for geoid_record in geoid_records:
            if geoid_record.s2_cells:
                for token in geoid_record.s2_cells:
                    cid = s2.CellId.from_token(token)
                    for level in range(cid.level() + 1):
                        probe_tokens.add(cid.parent(level).to_token())
                
        if probe_tokens:
            intersecting_regions = set(db.execute(
                select(RegionCoverCell.region_id).where(RegionCoverCell.s2_cell.in_(probe_tokens))
            ).scalars().all())

    region_parent_lists = set()
    if intersecting_regions:
        region_parent_lists = set(db.execute(
            select(RegionParentEdge.parent_list_id).where(RegionParentEdge.child_region_id.in_(intersecting_regions))
        ).scalars().all())

    list_resolutions = {}
    for lid in direct_list_ids:
        list_resolutions[lid] = "match"
    for lid in region_parent_lists:
        if lid not in list_resolutions:
            list_resolutions[lid] = "contained"

    frontier = direct_list_ids.union(region_parent_lists)
    found = set(frontier)
    while frontier:
        parents = set(db.execute(
            select(ListParentEdge.parent_list_id).where(ListParentEdge.child_list_id.in_(frontier))
        ).scalars().all())
        frontier = parents - found
        found.update(parents)
        for lid in parents:
            if lid not in list_resolutions:
                list_resolutions[lid] = "contained"

    tier = 3 if auth_result.get("used_authority") else 1

    if tier == 3:
        log_traceforward(user.get("sub"), auth_result.get("authority_jti"), payload.seed_geoid, payload.scope, len(found), list(found))
        holders = _resolve_holders(found, x_authority_token, payload.scope, payload.seed_geoid)
        matches = [TraceMatch(list_id=lid, holder_account=holders.get(lid), holder_status="resolved" if lid in holders else "unresolved", resolution=list_resolutions[lid]) for lid in sorted(found)]
    else:
        matches = [TraceMatch(list_id=lid, resolution=list_resolutions[lid]) for lid in sorted(found)]

    return TraceForwardResponse(
        seed_geoid=payload.seed_geoid,
        tier=tier,
        match_count=len(found),
        list_ids=sorted(found),
        matches=matches
    )
