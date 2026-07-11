# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from fastapi import APIRouter, HTTPException, Depends, Header, status, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import Optional

from app.database import get_db
from app.models import GeoID
from app.utils import Utils
from app.schemas import FetchFieldResponse, OverlapRequest, FetchFieldsForPointRequest
from app.s2_services import S2Service
from app.auth import get_current_user, require_l1, verify_field_grant

router = APIRouter(prefix="", tags=["Fetch Field"])

def apply_masking_logic(record, user, s2_index=None):
    wkt = record.geo_data.get("wkt")
    if not wkt:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored geometry is missing WKT data."
        )

    # L1 full view (authorized)
    if user:
        field_boundary_geo_json = Utils.get_geo_json(wkt)
        masking_level = "L1"
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
            "MaskingLevel": masking_level,
            "Geo Data": filtered_geo_data,
            "Geo JSON": field_boundary_geo_json
        }

    # L0 masked view (public)
    centroid = Utils.fetch_field_centroid_by_wkt(wkt)
    s2_l10_data = S2Service.get_s2_level_10_polygon(lat=centroid[1], long=centroid[0])
    
    area_ha = round(record.area_ha_approx, 1) if record.area_ha_approx is not None else None

    masked_geo_data = {
        "cell_token": s2_l10_data["token"],
        "country": record.country,
        "area_ha": area_ha
    }
    
    return {
        "message": "Field fetched successfully (masked).",
        "GEO Id": record.geo_id,
        "GEO Id Short": record.geo_id_short,
        "MaskingLevel": record.mask_level or "L0",
        "Geo Data": masked_geo_data,
        "Geo JSON": s2_l10_data["geojson"]
    }


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


@router.get("/resolve/{geo_id}", response_model=FetchFieldResponse, tags=["Field Fetch"])
@router.get("/fetch-field/{geo_id}", response_model=FetchFieldResponse, tags=["Field Fetch"])
async def fetch_field(
    geo_id: str,
    s2_index: Optional[str] = Query(None, description="Comma-separated S2 levels to fetch (e.g., '13,20')"),
    user: dict | None = Depends(get_current_user),
    x_field_grant: Optional[str] = Header(None, alias="X-Field-Grant"),
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
        
        import os
        has_l1 = bool(x_field_grant and verify_field_grant(x_field_grant, record.geo_id))
        if not has_l1 and os.getenv("AR_ACCOUNT_L1") == "legacy" and user is not None:
            has_l1 = True

        return apply_masking_logic(record, {"granted": True} if has_l1 else None, s2_index)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Fetch Field Error: {str(e)}"
        )


@router.get("/fetch-field-wkt/{geo_id}", tags=["Field Fetch"])
async def fetch_field_wkt(geo_id: str, user: dict | None = Depends(get_current_user), x_field_grant: Optional[str] = Header(None, alias="X-Field-Grant"), db: Session = Depends(get_db)):
    try:
        record = db.query(GeoID).filter(
            or_(GeoID.geo_id == geo_id, GeoID.geo_id_short == geo_id)
        ).first()
        
        if not record:
            raise HTTPException(status_code=404, detail="Field not found.")
            
        import os
        has_l1 = bool(x_field_grant and verify_field_grant(x_field_grant, record.geo_id))
        if not has_l1 and os.getenv("AR_ACCOUNT_L1") == "legacy" and user is not None:
            has_l1 = True

        if has_l1:
            wkt = record.geo_data.get('wkt')
            masking_level = "L1"
        else:
            wkt = None
            masking_level = record.mask_level or "L0"
            
        return {
            "message": "WKT fetched successfully." if wkt else "WKT hidden in L0 mask.",
            "GEO Id": record.geo_id,
            "GEO Id Short": record.geo_id_short,
            "MaskingLevel": masking_level,
            "WKT": wkt
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Fetch Field WKT Error: {str(e)}")


@router.get("/fetch-field-centroid/{geo_id}", tags=["Field Fetch"])
async def fetch_field_centroid(geo_id: str, user: dict | None = Depends(get_current_user), x_field_grant: Optional[str] = Header(None, alias="X-Field-Grant"), db: Session = Depends(get_db)):
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
        
        import os
        has_l1 = bool(x_field_grant and verify_field_grant(x_field_grant, record.geo_id))
        if not has_l1 and os.getenv("AR_ACCOUNT_L1") == "legacy" and user is not None:
            has_l1 = True

        if has_l1:
            return {
                "message": "Centroid fetched successfully.",
                "GEO Id": record.geo_id,
                "GEO Id Short": record.geo_id_short,
                "MaskingLevel": "L1",
                "Centroid": centroid,
            }
        else:
            s2_l10_data = S2Service.get_s2_level_10_polygon(lat=centroid[1], long=centroid[0])
            from shapely.geometry import shape
            l10_centroid = shape(s2_l10_data["geojson"]).centroid
            return {
                "message": "Centroid fetched successfully (masked).",
                "GEO Id": record.geo_id,
                "GEO Id Short": record.geo_id_short,
                "MaskingLevel": "L0",
                "Centroid": [l10_centroid.x, l10_centroid.y],
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Fetch Field Centroid Error: {str(e)}")

@router.get("/geoid/{geo_id}/eudr-export", tags=["Field Fetch"])
async def eudr_export(geo_id: str, user: dict | None = Depends(get_current_user), x_field_grant: Optional[str] = Header(None, alias="X-Field-Grant"), db: Session = Depends(get_db)):
        
    try:
        record = db.query(GeoID).filter(
            or_(GeoID.geo_id == geo_id, GeoID.geo_id_short == geo_id)
        ).first()
        
        if not record:
            raise HTTPException(status_code=404, detail="Field not found.")
            
        import os
        has_l1 = bool(x_field_grant and verify_field_grant(x_field_grant, record.geo_id))
        if not has_l1 and os.getenv("AR_ACCOUNT_L1") == "legacy" and user is not None:
            has_l1 = True
            
        if not has_l1:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="L1 authorization required"
            )

        field_wkt = record.geo_data.get('wkt')
        if not field_wkt:
            raise HTTPException(status_code=500, detail="Stored geometry is missing WKT data.")
            
        eudr_geojson = Utils.get_eudr_multipolygon(field_wkt)
        
        return {
            "message": "EUDR export generated successfully.",
            "GEO Id": record.geo_id,
            "GEO Id Short": record.geo_id_short,
            "MaskingLevel": "L1",
            "Geo JSON": eudr_geojson
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"EUDR Export Error: {str(e)}")

