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

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

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


class GeoIDRegimeAlias(Base):
    """v1 -> v2 identifier mapping, written by the legacy import.

    Distinct from GeoIDAlias, which keys on a content hash and records that two
    *submissions* describe one field. This keys on the v1 identifier itself,
    because AR 1.0 issued those identifiers to real users over several years and
    they persist in TerraPipe, in exports and in print. They have to keep
    resolving permanently.

    Read by _equivalence_set: without it, a trace-forward seeded with a v1 GeoID
    returns nothing for a field that is present under its v2 identifier. That is
    the worst failure shape available -- a confident, empty, wrong answer.

    String(128) rather than 64 because v1 identifiers include the 36-character
    uuid4() fallbacks the old cascade minted on collision.
    """

    __tablename__ = 'geo_id_regime_alias'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    v1_geo_id = Column(String(128), unique=True, index=True, nullable=False)
    v2_geo_id = Column(String(64), ForeignKey('geo_ids.geo_id', ondelete='CASCADE'),
                       index=True, nullable=False)
    v1_kind = Column(String(16))
    v1_regime = Column(String(8), default='v1')
    v2_regime = Column(String(8), default='v2')
    relation = Column(Enum('same_as', 'child_of', name='regime_relation_enum'),
                      nullable=False, default='same_as')
    created_at = Column(DateTime, default=get_utc_now)


class ListArtifact(Base):
    __tablename__ = 'list_artifact'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    list_id = Column(String(64), unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=get_utc_now)

from sqlalchemy import UniqueConstraint


class ListMemberEdge(Base):
    __tablename__ = 'listmember_edge'
    __table_args__ = (
        UniqueConstraint('geoid', 'list_id', name='uix_listmember_edge_geoid_listid'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    geoid = Column(String, index=True, nullable=False)
    list_id = Column(String(64), ForeignKey('list_artifact.list_id', ondelete='CASCADE'), index=True, nullable=False)

class ListParentEdge(Base):
    __tablename__ = 'list_parent_edge'
    __table_args__ = (
        UniqueConstraint('child_list_id', 'parent_list_id', name='uix_list_parent_edge'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    child_list_id = Column(String(64), index=True, nullable=False)
    parent_list_id = Column(String(64), ForeignKey('list_artifact.list_id', ondelete='CASCADE'), index=True, nullable=False)

class RegionArtifact(Base):
    __tablename__ = 'region_artifact'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    region_id = Column(String(64), unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=get_utc_now)

class RegionCoverCell(Base):
    __tablename__ = 'region_cover_cell'
    __table_args__ = (
        UniqueConstraint('region_id', 's2_cell', name='uix_region_cover_cell'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    region_id = Column(String(64), ForeignKey('region_artifact.region_id', ondelete='CASCADE'), index=True, nullable=False)
    s2_cell = Column(String(32), index=True, nullable=False)

class RegionParentEdge(Base):
    __tablename__ = 'region_parent_edge'
    __table_args__ = (
        UniqueConstraint('child_region_id', 'parent_list_id', name='uix_region_parent_edge'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    child_region_id = Column(String(64), ForeignKey('region_artifact.region_id', ondelete='CASCADE'), index=True, nullable=False)
    parent_list_id = Column(String(64), ForeignKey('list_artifact.list_id', ondelete='CASCADE'), index=True, nullable=False)
