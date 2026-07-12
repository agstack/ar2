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

from sqlalchemy import Column, String, Integer, Float, DateTime, JSON
from sqlalchemy.dialects.postgresql import UUID, JSONB , ARRAY
from app.database import Base

class GeoID(Base):
    __tablename__ = 'geo_ids'

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