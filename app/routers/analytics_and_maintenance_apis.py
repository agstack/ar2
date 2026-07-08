# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import GeoID

from app.utils import Utils
from sqlalchemy import or_
from app.schemas import FetchFieldsForPointRequest , OverlapRequest
from app.s2_services import S2Service

router = APIRouter(prefix="", tags=["Analytics", "Spatial Analysis", "Maintenance"])


@router.get("/fetch-registered-field-count", tags=["Analytics"])
async def fetch_registered_field_count(db: Session = Depends(get_db)):
    try:
        count = db.query(GeoID).count()
        return {
            "message": "Total count fetched successfully.",
            "count": count,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Fetch registered field count error! {str(e)}")


@router.get("/fetch-field-count-by-month", tags=["Analytics"])
async def fetch_field_count_by_month(db: Session = Depends(get_db)):
    try:
        count = Utils.get_row_count_by_month(db)
        return {
            "message": "Fetched Count By Month successfully.",
            "count": count,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Fetch field counts by month error! {str(e)}")


@router.get("/fetch-field-count-by-country", tags=["Analytics"])
async def fetch_field_count_by_country(db: Session = Depends(get_db)):
    try:
        count = Utils.get_row_count_by_country(db)
        return {
            "message": "Fetched count by country successfully.",
            "count": count,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Fetch field counts by country error! {str(e)}")


@router.post("/get-percentage-overlap-two-fields", tags=["Spatial Analysis"])
async def get_percentage_overlap_two_fields(payload: OverlapRequest, db: Session = Depends(get_db)):
    try:
        percentage_overlap = Utils.get_percentage_overlap_two_fields(
            db, payload.geo_id_field_1, payload.geo_id_field_2
        )
        return {"Percentage Overlap": f"{percentage_overlap} %"}
    except AttributeError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Get Percentage Overlap two Fields Error: {str(e)}")


@router.post("/fetch-fields-for-a-point", tags=["Spatial Analysis"])
async def fetch_fields_for_a_point(payload: FetchFieldsForPointRequest, db: Session = Depends(get_db)):
    try:
        s2_cell_token_13, s2_cell_token_20 = S2Service.get_cell_token_for_lat_long(
            payload.latitude, payload.longitude
        )
        
        fetched_fields = Utils.fetch_fields_for_a_point_two_way(
            db, s2_cell_token_13, s2_cell_token_20, 
            payload.domain, payload.s2_index, payload.boundary_type
        )
        return {"Fetched fields": fetched_fields}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Fetch Fields for a Point Error: {str(e)}")

@router.post("/populate-country-in-geo-ids", tags=["Maintenance"])
async def populate_country_in_geo_ids(db: Session = Depends(get_db)):
    try:
        rows = db.query(GeoID).filter(
            or_(GeoID.country == '', GeoID.country.is_(None))
        ).all()
        
        for row in rows:
            wkt_string = row.geo_data.get('wkt')
            if wkt_string:
                from shapely.wkt import loads as load_wkt
                from shapely.geometry import Point
                
                polygon = load_wkt(wkt_string)
                if polygon.geom_type == 'Point':
                    p = Point(polygon.coords[0])
                else:
                    p = Point(polygon.exterior.coords[0])
                    
                country = Utils.get_country_from_point([p.x, p.y])
                row.country = country
                
        db.commit()
        return {"message": f"Countries updated successfully for {len(rows)} records"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

