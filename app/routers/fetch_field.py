# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from fastapi import APIRouter, HTTPException, Depends, Header, status
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.models import GeoID

from app.utils import Utils
from fastapi import APIRouter, HTTPException, Depends, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import Optional
from app.schemas import FetchFieldResponse , OverlapRequest , FetchFieldsForPointRequest
from app.s2_services import S2Service

router = APIRouter(prefix="", tags=["Fetch Field"])

@router.get("/translate-geoid-to-short/{geo_id}", tags=["GeoID Translation"])
async def translate_to_short(geo_id: str, db: Session = Depends(get_db)):

    record = db.query(GeoID).filter(GeoID.geo_id == geo_id).first()
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Geo ID not found"
        )
    return {"geo_id": geo_id, "geo_id_short": record.geo_id_short}

@router.get("/translate-geoid-to-full/{geo_id_short}", tags=["GeoID Translation"])
async def translate_to_full(geo_id_short: str, db: Session = Depends(get_db)):

    record = db.query(GeoID).filter(GeoID.geo_id_short == geo_id_short).first()
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Short Geo ID not found"
        )
    return {"geo_id_short": geo_id_short, "geo_id": record.geo_id}



@router.get("/fetch-field/{geo_id}", response_model=FetchFieldResponse, tags=["Field Fetch"])
async def fetch_field(
    geo_id: str,
    s2_index: Optional[str] = Query(None, description="Comma-separated S2 levels to fetch (e.g., '13,20')"),
    db: Session = Depends(get_db)
):

    try:
        record = db.query(GeoID).filter(
            or_(
                GeoID.geo_id == geo_id,
                GeoID.geo_id_short == geo_id
            )
        ).first()
        
        if not record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Field not found, invalid Geo Id."
            )

        wkt = record.geo_data.get("wkt")
        if not wkt:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Stored geometry is missing WKT data."
            )
            
        field_boundary_geo_json = Utils.get_geo_json(wkt)

        filtered_geo_data = None
        if s2_index:
            s2_index_to_fetch = [int(i) for i in s2_index.split(',')]
            s2_indexes_to_remove = Utils.get_s2_indexes_to_remove(s2_index_to_fetch)
            
            if s2_indexes_to_remove != -1:
                temp_geo_data = dict(record.geo_data) 
                filtered_geo_data = Utils.get_specific_s2_index_geo_data(
                    temp_geo_data, s2_indexes_to_remove
                )
                
                if "wkt" in filtered_geo_data:
                    filtered_geo_data.pop("wkt")

        return {
            "message": "Field fetched successfully.",
            "GEO Id": record.geo_id,
            "GEO Id Short": record.geo_id_short,
            "Geo Data": filtered_geo_data,
            "Geo JSON": field_boundary_geo_json
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Fetch Field Error: {str(e)}"
        )


@router.get("/fetch-field-wkt/{geo_id}", tags=["Field Fetch"])
async def fetch_field_wkt(geo_id: str, db: Session = Depends(get_db)):
    try:
        record = db.query(GeoID).filter(
            or_(GeoID.geo_id == geo_id, GeoID.geo_id_short == geo_id)
        ).first()
        
        if not record:
            raise HTTPException(status_code=404, detail="Field not found.")
            
        return {
            "message": "WKT fetched successfully.",
            "GEO Id": record.geo_id,
            "WKT": record.geo_data.get('wkt')
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Fetch Field WKT Error: {str(e)}")


@router.get("/fetch-field-centroid/{geo_id}", tags=["Field Fetch"])
async def fetch_field_centroid(geo_id: str, db: Session = Depends(get_db)):
    try:
        record = db.query(GeoID).filter(
            or_(GeoID.geo_id == geo_id, GeoID.geo_id_short == geo_id)
        ).first()
        
        if not record:
            raise HTTPException(status_code=404, detail="Field not found.")
            
        field_wkt = record.geo_data.get('wkt')
        if not field_wkt:
            raise HTTPException(status_code=400, detail="Field WKT not found, fetch Field Centroid Error.")
            
        centroid = Utils.fetch_field_centroid_by_wkt(field_wkt)
        return {
            "message": "Centroid fetched successfully.",
            "Centroid": centroid,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Fetch Field Centroid Error: {str(e)}")
