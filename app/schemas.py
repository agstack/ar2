# SPDX-License-Identifier: EUPL-1.2
# Copyright (c) 2026 AgStack project contributors.
# Licensed under the EUPL, Version 1.2; see the LICENSE file for the full text.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from typing import Any

from pydantic import BaseModel, Field


class FieldRegistrationRequest(BaseModel):
    wkt: str = Field(..., description="WKT geometry of the field")
    threshold: int = Field(default=95, description="Overlap IoU threshold percentage for identity resolution")
    return_s2_indices: bool = Field(default=False)
    s2_index: str | None = Field(default=None, description="Comma separated S2 levels to fetch")
    field_name: str | None = Field(default=None, description="Optional name for the field")
    accuracy_class: str | None = Field(default=None, description="Optional accuracy class metadata")
    submitter: str | None = Field(default=None, description="Optional submitter info")

class PointRegistrationRequest(BaseModel):
    wkt: str = Field(..., description="WKT geometry of the point")
    s2_index: str | None = Field(default=None, description="Comma separated S2 levels to fetch")
    field_name: str | None = Field(default=None, description="Optional name for the point")

class FieldRegistrationResponse(BaseModel):
    message: str
    geo_id: str | None = Field(None, alias="Geo Id")
    geo_id_short: str | None = Field(None, alias="Geo Id Short")
    matched_geo_ids: list | None = Field(None, alias="matched geo ids")
    s2_cell_tokens: dict[str, Any] | None = Field(None, alias="S2 Cell Tokens")

class FetchFieldResponse(BaseModel):
    message: str
    geo_id: str = Field(..., alias="GEO Id")
    geo_id_short: str = Field(..., alias="GEO Id Short")
    masking_level: str | None = Field(None, alias="MaskingLevel")
    geo_data: dict[str, Any] | None = Field(None, alias="Geo Data")
    geo_json: dict[str, Any] = Field(..., alias="Geo JSON")

class OverlapRequest(BaseModel):
    geo_id_field_1: str = Field(..., description="First Geo ID")
    geo_id_field_2: str = Field(..., description="Second Geo ID")

class FetchFieldsForPointRequest(BaseModel):
    latitude: float = Field(..., description="Latitude of the point")
    longitude: float = Field(..., description="Longitude of the point")
    domain: str | None = Field(default=None, description="Optional domain filter")
    boundary_type: str | None = Field(default=None, description="Optional boundary type filter")
    s2_index: str | None = Field(default=None, description="Optional S2 indices to filter by")