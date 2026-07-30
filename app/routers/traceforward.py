import uuid
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from app.auth import require_hub_user
from sqlalchemy.orm import Session
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.models.geo_id_model import ListArtifact, ListMemberEdge, ListParentEdge
from app.merkle import merkle_root, canonical_members

router = APIRouter(tags=["Trace Forward"])

class RegisterListRequest(BaseModel):
    members: List[str]

class RegisterListResponse(BaseModel):
    list_id: str
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

    # Compute ListID (Merkle root)
    canonical = canonical_members(payload.members)
    list_id = merkle_root(canonical)

    # Check if exists
    existing = db.execute(select(ListArtifact).where(ListArtifact.list_id == list_id)).scalar_one_or_none()
    if existing:
        return RegisterListResponse(list_id=list_id, message="ListArtifact already exists")

    # Create new artifact
    new_artifact = ListArtifact(list_id=list_id)
    db.add(new_artifact)
    db.flush()

    # Create edges
    for member in canonical:
        # Day 1: we assume members are all GeoIDs. Day 2 will handle nesting.
        edge = ListMemberEdge(geoid=member, list_id=list_id)
        db.add(edge)

    db.commit()

    return RegisterListResponse(list_id=list_id, message="ListArtifact registered successfully")

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
    
    return ListMembersResponse(list_id=list_id, members=list(members))

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
    Retrieves the ListIDs that contain a specific GeoID (node-local reverse edge lookup).
    """
    list_ids = db.execute(select(ListMemberEdge.list_id).where(ListMemberEdge.geoid == geoid)).scalars().all()
    return ReverseLookupResponse(geoid=geoid, list_ids=list(list_ids))
