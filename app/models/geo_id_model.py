# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import uuid
from datetime import datetime, timezone

def get_utc_now():
    return datetime.now(timezone.utc)

from sqlalchemy import Column, String, Integer, Float, DateTime, JSON, ForeignKey, Enum, Index
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB , ARRAY
from app.database import Base

class GeoID(Base):
    __tablename__ = 'geo_ids'
    __table_args__ = (
        Index('ix_geo_ids_s2_cells_gin', 's2_cells', postgresql_using='gin'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    geo_id = Column(String, unique=True, index=True, nullable=False)
    geo_id_short = Column(String(16), unique=True, index=True, nullable=False)
    content_hash = Column(String(64), unique=True, index=True, nullable=False)
    field_name = Column(String)
    country = Column(String, index=True)
    boundary_type = Column(String)
    area_ha_approx = Column(Float)
    s2_level = Column(Integer)
    s2_cells = Column(ARRAY(String).with_variant(JSON(), 'sqlite'))
    geo_data = Column(JSONB().with_variant(JSON(), 'sqlite')) 
    crop = Column(String)
    mask_level = Column(String, default="L0")
    created_at = Column(DateTime, default=get_utc_now)
    updated_at = Column(DateTime, default=get_utc_now, onupdate=get_utc_now)

class GeoIDAlias(Base):
    __tablename__ = 'geo_id_alias'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_geo_id = Column(String, ForeignKey('geo_ids.geo_id', ondelete='CASCADE'), index=True, nullable=False)
    alias_content_hash = Column(String(64), index=True, nullable=False)
    submitter = Column(String, nullable=True)
    accuracy_class = Column(String, nullable=True)
    created_at = Column(DateTime, default=get_utc_now)
    relation = Column(Enum('same_as', 'child_of', name='relation_enum'), nullable=False)
class ListArtifact(Base):
    """Content-derived list artifact. list_id is the Merkle root over its members."""
    __tablename__ = 'list_artifact'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    list_id = Column(String(64), unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=get_utc_now)

class ListMemberEdge(Base):
    """Reverse edge: geoid -> list_id"""
    __tablename__ = 'listmember_edge'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    geoid = Column(String, index=True, nullable=False)
    list_id = Column(String(64), ForeignKey('list_artifact.list_id', ondelete='CASCADE'), index=True, nullable=False)

class ListParentEdge(Base):
    """Recursive composition: child_list_id -> parent_list_id"""
    __tablename__ = 'list_parent_edge'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    child_list_id = Column(String(64), index=True, nullable=False)
    parent_list_id = Column(String(64), ForeignKey('list_artifact.list_id', ondelete='CASCADE'), index=True, nullable=False)
