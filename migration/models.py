"""
SQLAlchemy models for the import's target tables.

These MIRROR the shipped schemas rather than importing them, so this package
runs standalone (no ar2 app config, no hub settings, no pancake service import)
and can be exercised against SQLite in tests. `tests/test_schema_drift.py`
parses the real model files and fails if a table or column name here diverges.

Mirrored from:
  ar2       app/models/geo_id_model.py          -> geo_ids
  ar2-hub   user_models.py                      -> users
  pancake   grants/models.py                    -> users, fieldlists,
                                                   fieldlist_members

geo_id_regime_alias is NEW. It does not exist in ar2, and the import cannot work
without it: `GeoIDAlias` maps a content hash of resubmitted WKT to a canonical
GeoID, which is a different relation entirely. There is nowhere to record that a
v1 identifier is now a given v2 identifier, and that is the join key between the
two legacy sources, because TerraPipe's profile rows reference v1 GeoIDs.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# ar2 registry
# --------------------------------------------------------------------------

class GeoID(Base):
    __tablename__ = "geo_ids"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    geo_id = Column(String, unique=True, index=True, nullable=False)
    geo_id_short = Column(String(16), unique=True, index=True, nullable=False)
    content_hash = Column(String(64), unique=True, index=True, nullable=False)
    field_name = Column(String)
    country = Column(String, index=True)
    boundary_type = Column(String)
    area_ha_approx = Column(Float)
    s2_level = Column(Integer)
    s2_cells = Column(JSON)
    geo_data = Column(JSON)
    crop = Column(String)
    mask_level = Column(String, default="L0")
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class GeoIDBlockingCell(Base):
    """Normalised blocking index.

    ar2 keeps the cover in `geo_ids.s2_cells` with a GIN index, which is fast on
    Postgres but not expressible on SQLite. This side table carries only the L13
    blocking keys, is portable, and keeps candidate lookup a plain indexed join.
    It is additive: nothing reads it except the import.
    """

    __tablename__ = "geo_id_blocking_cell"
    __table_args__ = (
        UniqueConstraint("geo_id", "cell_token", name="uix_geo_id_blocking_cell"),
        Index("ix_blocking_cell_token", "cell_token"),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    geo_id = Column(String, ForeignKey("geo_ids.geo_id", ondelete="CASCADE"),
                    index=True, nullable=False)
    cell_token = Column(String(32), nullable=False)


class GeoIDRegimeAlias(Base):
    """v1 -> v2 identifier mapping. NEW TABLE; see the module docstring.

    Must resolve permanently: AR 1.0 issued these identifiers to real users over
    several years and they persist in TerraPipe, in exports and in print.
    """

    __tablename__ = "geo_id_regime_alias"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    # String(128), not 64: v1 identifiers include 36-char UUID fallbacks.
    v1_geo_id = Column(String(128), unique=True, index=True, nullable=False)
    v2_geo_id = Column(String(64), ForeignKey("geo_ids.geo_id", ondelete="CASCADE"),
                       index=True, nullable=False)
    v1_kind = Column(String(16))
    v1_regime = Column(String(8), default="v1")
    v2_regime = Column(String(8), default="v2")
    relation = Column(Enum("same_as", "child_of", name="regime_relation_enum"),
                      nullable=False, default="same_as")
    created_at = Column(DateTime, default=utcnow)


class GeoIDParentEdge(Base):
    """Nesting (P6). The child keeps its own identity; this records the parent."""

    __tablename__ = "geo_id_parent_edge"
    __table_args__ = (
        UniqueConstraint("child_geo_id", "parent_geo_id", name="uix_geo_id_parent"),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    child_geo_id = Column(String(64), ForeignKey("geo_ids.geo_id", ondelete="CASCADE"),
                          index=True, nullable=False)
    parent_geo_id = Column(String(64), ForeignKey("geo_ids.geo_id", ondelete="CASCADE"),
                           index=True, nullable=False)
    created_at = Column(DateTime, default=utcnow)


class ListArtifact(Base):
    __tablename__ = 'list_artifact'

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    list_id = Column(String(64), unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=utcnow)


class ListMemberEdge(Base):
    __tablename__ = 'listmember_edge'
    __table_args__ = (
        UniqueConstraint('geoid', 'list_id', name='uix_listmember_edge_geoid_listid'),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    geoid = Column(String, index=True, nullable=False)
    list_id = Column(String(64), ForeignKey('list_artifact.list_id', ondelete='CASCADE'), index=True, nullable=False)


# --------------------------------------------------------------------------
# ar2-hub accounts
# --------------------------------------------------------------------------

class HubUser(Base):
    """Mirrors ar2-hub user_models.User.

    Note the constraints, which reject a real share of legacy profiles: email
    and phone are both NOT NULL *and* UNIQUE, names and password_hash are NOT
    NULL, and is_active defaults False.
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    first_name = Column(String(50), nullable=False)
    last_name = Column(String(50), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    phone = Column(String(20), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    client_id = Column(String(50), unique=True, index=True, nullable=True)
    client_secret_hash = Column(String(255), nullable=True)
    registration_date = Column(DateTime, default=utcnow)
    is_active = Column(Boolean, default=False)
    country = Column(String(2), nullable=True)
    role = Column(String(20), default="user", nullable=False)


# --------------------------------------------------------------------------
# pancake
# --------------------------------------------------------------------------

class PancakeBase(DeclarativeBase):
    """Separate metadata: pancake is a different database from the hub, and both
    define a table called `users`."""


class PancakeUser(PancakeBase):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    hub_account_id = Column(String(128), unique=True, index=True)
    email = Column(String(256), nullable=True)
    created_at = Column(DateTime, default=utcnow)


class FieldList(PancakeBase):
    __tablename__ = "fieldlists"
    __table_args__ = (UniqueConstraint("list_id", "owner_id", name="uq_fieldlist_owner"),)

    id = Column(Integer, primary_key=True)
    list_id = Column(String(64), index=True)
    name = Column(String(256))
    owner_id = Column(Integer, ForeignKey("users.id"), index=True)
    created_at = Column(DateTime, default=utcnow)


class ImportCheckpoint(PancakeBase):
    """Resumability marker, written on the AR2 side in production.

    Kept in its own table rather than inferred so a killed run can be audited
    after the fact: which phase, how far, when.
    """

    __tablename__ = "import_checkpoint"

    id = Column(Integer, primary_key=True)
    phase = Column(String(32), nullable=False)
    processed = Column(Integer, default=0)
    last_source_id = Column(String(128))
    note = Column(Text)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
