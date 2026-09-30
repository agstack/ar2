# Licensed under the EUPL, Version 1.2 or – as soon they will be approved by
# the European Commission - subsequent versions of the EUPL (the "Licence");
# You may not use this work except in compliance with the Licence.
# You may obtain a copy of the Licence at:
# https://joinup.ec.europa.eu/software/page/eupl

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import geoid_v2
from app.auth import get_current_user, verify_field_grant
from app.database import get_db
from app.models import GeoID
from app.s2_services import S2Service
from app.schemas import FetchFieldResponse
from app.utils import Utils

router = APIRouter(prefix="", tags=["Fetch Field"])

def _area_on_the_wire(record) -> float | None:
    """The area to publish for a plot, whichever kind it is.

    A polygon's is computed from its boundary at registration; a point's is the
    one the registrant declared, which is the only area it has and the number a
    screening node needs to know how much ground a single coordinate is standing
    for. Both arrive under one key so a consumer does not have to branch to find
    out how big the plot is.
    """
    geo_data = record.geo_data or {}
    if geoid_v2.kind_of(geo_data) == geoid_v2.KIND_POINT:
        declared = geo_data.get(geoid_v2.DECLARED_AREA_KEY)
        if declared is None:
            declared = record.area_ha_approx
        return round(declared, 4) if declared else None
    return round(record.area_ha_approx, 4) if record.area_ha_approx is not None else None


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
            "GeometryKind": geoid_v2.kind_of(record.geo_data),
            "AreaHa": _area_on_the_wire(record),
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
        "area_ha": area_ha,
        # Which of the two kinds of plot this is, at L0 as well as L1. A reader
        # entitled only to the masked view still has to know whether the cell
        # stands for a boundary or for a coordinate with a declared area: it is
        # the difference between a screen that read the plot and one that read a
        # sample of it.
        geoid_v2.KIND_KEY: geoid_v2.kind_of(record.geo_data),
    }
    
    return {
        "message": "Field fetched successfully (masked).",
        "GEO Id": record.geo_id,
        "GEO Id Short": record.geo_id_short,
        "MaskingLevel": record.mask_level or "L0",
        "GeometryKind": masked_geo_data[geoid_v2.KIND_KEY],
        "AreaHa": area_ha,
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
    s2_index: str | None = Query(None, description="Comma-separated S2 levels to fetch (e.g., '13,20')"),
    user: dict | None = Depends(get_current_user),
    x_field_grant: str | None = Header(None, alias="X-Field-Grant"),
    db: Session = Depends(get_db)
):
    try:
        record = db.query(GeoID).filter(
            or_(
                GeoID.geo_id == geo_id,
                GeoID.geo_id_short == geo_id
            )
        ).first()
        
        # ADDED ALIAS CHECK HERE:
        if not record:
            from app.models.geo_id_model import GeoIDAlias
            alias = db.query(GeoIDAlias).filter(GeoIDAlias.alias_content_hash == geo_id).first()
            if alias:
                record = db.query(GeoID).filter(GeoID.geo_id == alias.canonical_geo_id).first()
        
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
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Fetch Field Error: {e!s}"
        )


@router.get("/fetch-field-wkt/{geo_id}", tags=["Field Fetch"])
async def fetch_field_wkt(geo_id: str, user: dict | None = Depends(get_current_user), x_field_grant: str | None = Header(None, alias="X-Field-Grant"), db: Session = Depends(get_db)):
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
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Fetch Field WKT Error: {e!s}")


@router.get("/fetch-field-centroid/{geo_id}", tags=["Field Fetch"])
async def fetch_field_centroid(geo_id: str, user: dict | None = Depends(get_current_user), x_field_grant: str | None = Header(None, alias="X-Field-Grant"), db: Session = Depends(get_db)):
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
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Fetch Field Centroid Error: {e!s}")

@router.get("/geoid/{geo_id}/eudr-export", tags=["Field Fetch"])
async def eudr_export(geo_id: str, user: dict | None = Depends(get_current_user), x_field_grant: str | None = Header(None, alias="X-Field-Grant"), db: Session = Depends(get_db)):
        
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

        kind = geoid_v2.kind_of(record.geo_data)
        try:
            eudr_geojson = Utils.get_eudr_multipolygon(field_wkt, _area_on_the_wire(record))
        except geoid_v2.GeometryUnusable as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return {
            "message": "EUDR export generated successfully.",
            "GEO Id": record.geo_id,
            "GEO Id Short": record.geo_id_short,
            "MaskingLevel": "L1",
            "GeometryKind": kind,
            "AreaHa": _area_on_the_wire(record),
            "Geo JSON": eudr_geojson
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"EUDR Export Error: {e!s}")

