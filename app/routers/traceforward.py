import uuid
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from app.auth import require_hub_user
from sqlalchemy.orm import Session
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel

from app.database import get_db
from app.models.geo_id_model import (
    ListArtifact, ListMemberEdge, ListParentEdge,
    RegionArtifact, RegionCoverCell, RegionParentEdge,
    GeoID
)
from app.merkle import merkle_root, canonical_members
import hashlib

router = APIRouter(tags=["Trace Forward"])

class RegisterListRequest(BaseModel):
    members: List[str]

class RegisterListResponse(BaseModel):
    list_id: str
    message: str

class RegisterRegionRequest(BaseModel):
    members: List[str]

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

    new_artifact = ListArtifact(list_id=list_id)
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
    if not payload.members:
        raise HTTPException(status_code=400, detail="Region must have at least one member")

    all_geoids = set()
    for member in payload.members:
        is_list = db.execute(select(ListArtifact).where(ListArtifact.list_id == member)).scalar_one_or_none()
        if is_list:
            list_geoids = db.execute(select(ListMemberEdge.geoid).where(ListMemberEdge.list_id == member)).scalars().all()
            all_geoids.update(list_geoids)
        else:
            all_geoids.add(member)
            
    if not all_geoids:
        raise HTTPException(status_code=400, detail="Could not resolve any GeoIDs from members")

    geoid_records = db.execute(select(GeoID.s2_cells).where(GeoID.geo_id.in_(all_geoids))).scalars().all()
    s2_cells = set()
    for cells in geoid_records:
        if cells:
            s2_cells.update(cells)
            
    if not s2_cells:
        raise HTTPException(status_code=400, detail="No S2 cells found for the provided members")

    sorted_cells = sorted(list(s2_cells))
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
    members: List[str]

@router.get("/list-artifact/{list_id}", response_model=ListMembersResponse)
def get_list_artifact(
    list_id: str,
    user: dict = Depends(require_hub_user),
    db: Session = Depends(get_db)
):
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

class ReverseLookupResponse(BaseModel):
    geoid: str
    list_ids: List[str]

@router.get("/list-artifact/reverse/{geoid}", response_model=ReverseLookupResponse)
def get_lists_for_geoid(
    geoid: str,
    user: dict = Depends(require_hub_user),
    db: Session = Depends(get_db)
):
    """
    Retrieves the ListIDs that contain a specific GeoID (2-hop reverse lookup).
    Hop 1: Lists that directly contain the GeoID + Regions containing the GeoID.
    Hop 2: Parent lists of Hop 1 matches.
    """
    direct_list_ids = set(db.execute(select(ListMemberEdge.list_id).where(ListMemberEdge.geoid == geoid)).scalars().all())

    geoid_record = db.execute(select(GeoID).where(GeoID.geo_id == geoid)).scalar_one_or_none()
    intersecting_regions = set()
    if geoid_record and geoid_record.s2_cells:
        all_covers = db.execute(select(RegionCoverCell)).scalars().all()
        for cell in geoid_record.s2_cells:
            for cover in all_covers:
                if cell.startswith(cover.s2_cell):
                    intersecting_regions.add(cover.region_id)

    region_parent_lists = set()
    if intersecting_regions:
        region_parent_lists = set(db.execute(
            select(RegionParentEdge.parent_list_id).where(RegionParentEdge.child_region_id.in_(intersecting_regions))
        ).scalars().all())

    list_parent_lists = set()
    if direct_list_ids:
        list_parent_lists = set(db.execute(
            select(ListParentEdge.parent_list_id).where(ListParentEdge.child_list_id.in_(direct_list_ids))
        ).scalars().all())

    all_list_ids = direct_list_ids.union(region_parent_lists).union(list_parent_lists)

    return ReverseLookupResponse(geoid=geoid, list_ids=list(all_list_ids))
