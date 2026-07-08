# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from pydantic import BaseModel, Field
from typing import Optional, Any, Dict

class FieldRegistrationRequest(BaseModel):
    wkt: str = Field(..., description="WKT geometry of the field")
    threshold: int = Field(default=95, description="Overlap threshold percentage")
    return_s2_indices: bool = Field(default=False)
    s2_index: Optional[str] = Field(default=None, description="Comma separated S2 levels to fetch")
    field_name: Optional[str] = Field(default=None, description="Optional name for the field")

class PointRegistrationRequest(BaseModel):
    wkt: str = Field(..., description="WKT geometry of the point")
    s2_index: Optional[str] = Field(default=None, description="Comma separated S2 levels to fetch")
    field_name: Optional[str] = Field(default=None, description="Optional name for the point")

class FieldRegistrationResponse(BaseModel):
    message: str
    geo_id: Optional[str] = Field(None, alias="Geo Id")
    geo_id_short: Optional[str] = Field(None, alias="Geo Id Short")
    matched_geo_ids: Optional[list] = Field(None, alias="matched geo ids")
    s2_cell_tokens: Optional[Dict[str, Any]] = Field(None, alias="S2 Cell Tokens")

class FetchFieldResponse(BaseModel):
    message: str
    geo_id: str = Field(..., alias="GEO Id")
    geo_id_short: str = Field(..., alias="GEO Id Short")
    masking_level: Optional[str] = Field(None, alias="MaskingLevel")
    geo_data: Optional[Dict[str, Any]] = Field(None, alias="Geo Data")
    geo_json: Dict[str, Any] = Field(..., alias="Geo JSON")

class OverlapRequest(BaseModel):
    geo_id_field_1: str = Field(..., description="First Geo ID")
    geo_id_field_2: str = Field(..., description="Second Geo ID")

class FetchFieldsForPointRequest(BaseModel):
    latitude: float = Field(..., description="Latitude of the point")
    longitude: float = Field(..., description="Longitude of the point")
    domain: Optional[str] = Field(default=None, description="Optional domain filter")
    boundary_type: Optional[str] = Field(default=None, description="Optional boundary type filter")
    s2_index: Optional[str] = Field(default=None, description="Optional S2 indices to filter by")