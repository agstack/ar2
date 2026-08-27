# SPDX-License-Identifier: EUPL-1.2
# Copyright (c) 2026 AgStack project contributors.
# Licensed under the EUPL, Version 1.2; see the LICENSE file for the full text.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import GeoID
from app.s2_services import S2Service
from app.schemas import FetchFieldsForPointRequest, OverlapRequest
from app.utils import Utils

router = APIRouter(prefix="", tags=["Analytics", "Spatial Analysis", "Maintenance"])


@router.get("/fetch-registered-field-count", tags=["Analytics"])
async def fetch_registered_field_count(db: Session = Depends(get_db)):
    try:
        count = db.query(GeoID).count()
        return {
            "message": "Total count fetched successfully.",
            "count": count,
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Fetch registered field count error! {e!s}")


@router.get("/fetch-field-count-by-month", tags=["Analytics"])
async def fetch_field_count_by_month(db: Session = Depends(get_db)):
    try:
        count = Utils.get_row_count_by_month(db)
        return {
            "message": "Fetched Count By Month successfully.",
            "count": count,
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Fetch field counts by month error! {e!s}")


@router.get("/fetch-field-count-by-country", tags=["Analytics"])
async def fetch_field_count_by_country(db: Session = Depends(get_db)):
    try:
        count = Utils.get_row_count_by_country(db)
        return {
            "message": "Fetched count by country successfully.",
            "count": count,
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Fetch field counts by country error! {e!s}")


@router.post("/get-percentage-overlap-two-fields", tags=["Spatial Analysis"])
async def get_percentage_overlap_two_fields(payload: OverlapRequest, db: Session = Depends(get_db)):
    try:
        percentage_overlap = Utils.get_percentage_overlap_two_fields(
            db, payload.geo_id_field_1, payload.geo_id_field_2
        )
        return {"Percentage Overlap": f"{percentage_overlap} %"}
    except AttributeError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Get Percentage Overlap two Fields Error: {e!s}")


@router.post("/fetch-fields-for-a-point", tags=["Spatial Analysis"])
async def fetch_fields_for_a_point(
    payload: FetchFieldsForPointRequest, 
    user: dict | None = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    try:
        s2_cell_token_13, s2_cell_token_20 = S2Service.get_cell_token_for_lat_long(
            payload.latitude, payload.longitude
        )
        
        fetched_fields = Utils.fetch_fields_for_a_point_two_way(
            db, s2_cell_token_13, s2_cell_token_20, 
            payload.domain, payload.s2_index, payload.boundary_type
        )
        
        if not user:
            for field in fetched_fields:
                record = db.query(GeoID).filter(GeoID.geo_id == field["Geo Id"]).first()
                wkt_str = record.geo_data.get('wkt')
                centroid = Utils.fetch_field_centroid_by_wkt(wkt_str)
                s2_l10_data = S2Service.get_s2_level_10_polygon(lat=centroid[1], long=centroid[0])
                field["Geo JSON"] = s2_l10_data["geojson"]
                field["MaskingLevel"] = record.mask_level or "L0"
        else:
            for field in fetched_fields:
                field["MaskingLevel"] = "L1"

        return {"Fetched fields": fetched_fields}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Fetch Fields for a Point Error: {e!s}")

@router.post("/populate-country-in-geo-ids", tags=["Maintenance"])
async def populate_country_in_geo_ids(db: Session = Depends(get_db)):
    try:
        rows = db.query(GeoID).filter(
            or_(GeoID.country == '', GeoID.country.is_(None))
        ).all()
        
        for row in rows:
            wkt_string = row.geo_data.get('wkt')
            if wkt_string:
                from shapely.geometry import Point
                from shapely.wkt import loads as load_wkt
                
                polygon = load_wkt(wkt_string)
                if polygon.geom_type == 'Point':
                    p = Point(polygon.coords[0])
                else:
                    p = Point(polygon.exterior.coords[0])
                    
                country = Utils.get_country_from_point([p.x, p.y])
                row.country = country
                
        db.commit()
        return {"message": f"Countries updated successfully for {len(rows)} records"}
    except Exception as e:  # noqa: BLE001
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

