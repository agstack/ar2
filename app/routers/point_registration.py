# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import json
import random
from fastapi import APIRouter, HTTPException, Depends, Header, Query, status, UploadFile, File, Body
from sqlalchemy.orm import Session
from typing import Optional, Dict, Any

from app.schemas import PointRegistrationRequest
from app.database import get_db
from app.models import GeoID
from app.utils import Utils, GeoDataUtils
from app.s2_services import S2Service

from fastapi.responses import StreamingResponse
import asyncio
import time

router = APIRouter(prefix="", tags=["Point Registration"])


@router.post("/register-point", tags=["Point Registration"])
async def register_point(
    payload: PointRegistrationRequest,
    automated_field: Optional[int] = Header(None, alias="AUTOMATED-FIELD"),
    db: Session = Depends(get_db)
    ):

    try:
        boundary_type = "automated" if automated_field else "manual"
        point_wkt = payload.wkt
        
        field_name = payload.field_name
        if not field_name:
            field_name = f"point_{random.randint(1000, 9999)}"
            
        # points have exactly 0 area
        area_ha = 0.0 
        
        content_hash = GeoDataUtils.generate_content_hash(point_wkt)
        existing_record = db.query(GeoID).filter(GeoID.content_hash == content_hash).first()
        if existing_record:
            return {
                "message": "Point already registered.",
                "Geo Id": existing_record.geo_id,
                "Geo Id Short": existing_record.geo_id_short,
                "Geo JSON": Utils.get_geo_json(existing_record.geo_data.get('wkt', point_wkt))
            }

        point_geo_json = Utils.get_geo_json(point_wkt)
        lng = point_geo_json['geometry']['coordinates'][0]
        lat = point_geo_json['geometry']['coordinates'][1]
        country = Utils.get_country_from_point([lng, lat])

        indices = {
            8: S2Service.wkt_to_cell_tokens(point_wkt, 8, point=True),
            13: S2Service.wkt_to_cell_tokens(point_wkt, 13, point=True),
            15: S2Service.wkt_to_cell_tokens(point_wkt, 15, point=True),
            18: S2Service.wkt_to_cell_tokens(point_wkt, 18, point=True),
            19: S2Service.wkt_to_cell_tokens(point_wkt, 19, point=True),
            20: S2Service.wkt_to_cell_tokens(point_wkt, 20, point=True),
            30: S2Service.wkt_to_cell_tokens(point_wkt, 30, point=True)
        }

        geo_id = Utils.generate_geo_id(indices[30])
        geo_id_l20 = Utils.generate_geo_id(indices[20])
        geo_id_short = GeoDataUtils.generate_short_geo_id(geo_id)
        records_list = Utils.records_s2_cell_tokens(indices)

        geo_id_exists_wkt = Utils.lookup_geo_ids(db, geo_id)
        if not geo_id_exists_wkt:
            geo_data = Utils.register_field_boundary(
                db=db, geo_id=geo_id, geo_id_short=geo_id_short, content_hash=content_hash,
                indices=indices, records_list=records_list, field_wkt=point_wkt,
                country=country, boundary_type=boundary_type, field_name=field_name,
                area_ha_approx=area_ha
            )

            response_payload = {
                "message": "Point registered successfully.",
                "Geo Id": geo_id,
                "Geo Id Short": geo_id_short,
                "Geo JSON": point_geo_json
            }

            if payload.s2_index:
                s2_index_to_fetch = [int(i) for i in payload.s2_index.split(',')]
                s2_indexes_to_remove = Utils.get_s2_indexes_to_remove(s2_index_to_fetch)
                if s2_indexes_to_remove != -1:
                    s2_data = dict(geo_data)
                    s2_data = Utils.get_specific_s2_index_geo_data(s2_data, s2_indexes_to_remove)
                    if "wkt" in s2_data:
                        s2_data.pop("wkt")
                    response_payload["S2 Cell Tokens"] = s2_data

            return response_payload
        else:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Point already registered.",
                    "Geo Id": geo_id_l20,
                    "Geo Id Short": GeoDataUtils.generate_short_geo_id(geo_id_l20),
                    "Geo JSON requested": point_geo_json,
                    "Geo JSON registered": Utils.get_geo_json(geo_id_exists_wkt)
                }
            )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Register Point Error: {str(e)}")


# @router.post("/register-points-geojson", tags=["Point Registration"])
# async def register_points_geojson(
#     file: Optional[UploadFile] = File(None),
#     payload: Optional[Dict[str, Any]] = Body(None),
#     automated_field: Optional[int] = Header(None, alias="AUTOMATED-FIELD"),
#     db: Session = Depends(get_db)
#     ):

#     try:
#         data = None
#         if file:
#             contents = await file.read()
#             data = json.loads(contents.decode('utf-8'))
#         elif payload:
#             data = payload
#         else:
#             raise HTTPException(status_code=400, detail="No GeoJSON file or payload provided")

#         if data.get('type') != 'FeatureCollection' or 'features' not in data:
#             raise HTTPException(status_code=400, detail="Invalid GeoJSON FeatureCollection format")

#         boundary_type = "automated" if automated_field else "manual"
#         results = []

#         for feature in data['features']:
#             try:
#                 geometry_type = feature.get('geometry', {}).get('type')
                
#                 if geometry_type != 'Point':
#                     results.append({
#                         "status": "skipped",
#                         "message": f"Geometry type {geometry_type} is not supported in bulk points API",
#                         "geo_json": feature
#                     })
#                     continue

#                 point_wkt = Utils.geojson_to_wkt(feature)
                
#                 properties = feature.get('properties', {})
#                 field_name = properties.get('field_name')
#                 if not field_name:
#                     field_name = f"point_{random.randint(1000, 9999)}"
                
#                 # points have exactly 0 area
#                 area_ha = 0.0

#                 content_hash = GeoDataUtils.generate_content_hash(point_wkt)
#                 existing_record = db.query(GeoID).filter(GeoID.content_hash == content_hash).first()
#                 if existing_record:
#                     results.append({
#                         "status": "exists",
#                         "message": "Exact geometry already registered.",
#                         "geo_id": existing_record.geo_id,
#                         "geo_json": feature
#                     })
#                     continue

#                 lat = feature['geometry']['coordinates'][1]
#                 lng = feature['geometry']['coordinates'][0]
#                 country = Utils.get_country_from_point([lng, lat])

#                 indices = {
#                     8: S2Service.wkt_to_cell_tokens(point_wkt, 8, point=True),
#                     13: S2Service.wkt_to_cell_tokens(point_wkt, 13, point=True),
#                     15: S2Service.wkt_to_cell_tokens(point_wkt, 15, point=True),
#                     18: S2Service.wkt_to_cell_tokens(point_wkt, 18, point=True),
#                     19: S2Service.wkt_to_cell_tokens(point_wkt, 19, point=True),
#                     20: S2Service.wkt_to_cell_tokens(point_wkt, 20, point=True),
#                     30: S2Service.wkt_to_cell_tokens(point_wkt, 30, point=True),
#                 }

#                 geo_id = Utils.generate_geo_id(indices[30])
#                 geo_id_l20 = Utils.generate_geo_id(indices[20])
#                 geo_id_short = GeoDataUtils.generate_short_geo_id(geo_id)
#                 records_list = Utils.records_s2_cell_tokens(indices)
                
#                 s2_index = feature.get('properties', {}).get('s2_index')
#                 s2_indexes_to_remove = -1
#                 if s2_index:
#                     s2_index_to_fetch = [int(i) for i in str(s2_index).split(',')]
#                     s2_indexes_to_remove = Utils.get_s2_indexes_to_remove(s2_index_to_fetch)

#                 geo_id_exists_wkt = Utils.lookup_geo_ids(db, geo_id)
#                 if not geo_id_exists_wkt:
#                     geo_data = Utils.register_field_boundary(
#                         db=db, geo_id=geo_id, geo_id_short=geo_id_short, content_hash=content_hash,
#                         indices=indices, records_list=records_list, field_wkt=point_wkt, 
#                         country=country, boundary_type=boundary_type, field_name=field_name,
#                         area_ha_approx=area_ha
#                     )
                    
#                     geo_data_to_return = None
#                     if s2_index and s2_indexes_to_remove != -1:
#                         geo_data_to_return = Utils.get_specific_s2_index_geo_data(dict(geo_data), s2_indexes_to_remove)
#                         if isinstance(geo_data_to_return, dict) and "wkt" in geo_data_to_return:
#                             geo_data_to_return.pop("wkt")

#                     results.append({
#                         "status": "created",
#                         "message": "Point registered successfully.",
#                         "Geo Id": geo_id,
#                         "S2 Cell Tokens": geo_data_to_return,
#                         "Geo JSON": feature
#                     })
#                 else:
#                     results.append({
#                         "status": "exists",
#                         "message": "Point already registered.",
#                         "Geo Id": geo_id_l20,
#                         "Geo JSON requested": feature,
#                         "Geo JSON registered": Utils.get_geo_json(geo_id_exists_wkt)
#                     })

#             except Exception as point_error:
#                 db.rollback()
#                 results.append({
#                     "status": "error",
#                     "message": str(point_error),
#                     "geo_json": feature
#                 })

#         return {
#             "message": "Bulk point registration completed",
#             "results": results
#         }

#     except HTTPException:
#         raise
#     except Exception as e:
#         raise HTTPException(status_code=400, detail=f"Bulk Register Points Error: {str(e)}")


@router.post("/register-points-geojson", tags=["Point Registration"])
async def register_points_geojson(
    file: Optional[UploadFile] = File(None),
    payload: Optional[Dict[str, Any]] = Body(None),
    automated_field: Optional[int] = Header(None, alias="AUTOMATED-FIELD"),
    db: Session = Depends(get_db)
):
    try:
        data = None
        if file:
            contents = await file.read()
            data = json.loads(contents.decode('utf-8'))
        elif payload:
            data = payload
        else:
            raise HTTPException(status_code=400, detail="No GeoJSON file or payload provided")

        if data.get('type') != 'FeatureCollection' or 'features' not in data:
            raise HTTPException(status_code=400, detail="Invalid GeoJSON FeatureCollection format")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Request Error: {str(e)}")

    boundary_type = "automated" if automated_field else "manual"
    features = data['features']
    total_features = len(features)

    async def process_and_stream():
        results = []
        
        yield json.dumps({
            "status": "started", 
            "total": total_features, 
            "progress": 0, 
            "message": "Starting bulk point registration..."
        }) + "\n"

        start_time = time.time()
        for index, feature in enumerate(features):
            try:
                geometry_type = feature.get('geometry', {}).get('type')
                
                if geometry_type != 'Point':
                    results.append({
                        "status": "skipped",
                        "message": f"Geometry type {geometry_type} is not supported in bulk points API",
                        "geo_json": feature
                    })
                    yield json.dumps({
                        "status": "processing", 
                        "progress": index + 1, 
                        "percentage": round(((index + 1) / total_features) * 100, 2)
                    }) + "\n"
                    continue

                point_wkt = Utils.geojson_to_wkt(feature)
                
                properties = feature.get('properties', {})
                field_name = properties.get('field_name')
                if not field_name:
                    field_name = f"point_{random.randint(1000, 9999)}"
                
                area_ha = 0.0

                content_hash = GeoDataUtils.generate_content_hash(point_wkt)
                existing_record = db.query(GeoID).filter(GeoID.content_hash == content_hash).first()
                if existing_record:
                    results.append({
                        "status": "exists",
                        "message": "Exact geometry already registered.",
                        "geo_id": existing_record.geo_id,
                        "geo_json": feature
                    })
                    yield json.dumps({
                        "status": "processing", 
                        "progress": index + 1, 
                        "percentage": round(((index + 1) / total_features) * 100, 2)
                    }) + "\n"
                    continue

                lat = feature['geometry']['coordinates'][1]
                lng = feature['geometry']['coordinates'][0]
                country = Utils.get_country_from_point([lng, lat])

                indices = {
                    8: S2Service.wkt_to_cell_tokens(point_wkt, 8, point=True),
                    13: S2Service.wkt_to_cell_tokens(point_wkt, 13, point=True),
                    15: S2Service.wkt_to_cell_tokens(point_wkt, 15, point=True),
                    18: S2Service.wkt_to_cell_tokens(point_wkt, 18, point=True),
                    19: S2Service.wkt_to_cell_tokens(point_wkt, 19, point=True),
                    20: S2Service.wkt_to_cell_tokens(point_wkt, 20, point=True),
                    30: S2Service.wkt_to_cell_tokens(point_wkt, 30, point=True),
                }

                geo_id = Utils.generate_geo_id(indices[30])
                geo_id_l20 = Utils.generate_geo_id(indices[20])
                geo_id_short = GeoDataUtils.generate_short_geo_id(geo_id)
                records_list = Utils.records_s2_cell_tokens(indices)
                
                s2_index = feature.get('properties', {}).get('s2_index')
                s2_indexes_to_remove = -1
                if s2_index:
                    s2_index_to_fetch = [int(i) for i in str(s2_index).split(',')]
                    s2_indexes_to_remove = Utils.get_s2_indexes_to_remove(s2_index_to_fetch)

                geo_id_exists_wkt = Utils.lookup_geo_ids(db, geo_id)
                if not geo_id_exists_wkt:
                    geo_data = Utils.register_field_boundary(
                        db=db, geo_id=geo_id, geo_id_short=geo_id_short, content_hash=content_hash,
                        indices=indices, records_list=records_list, field_wkt=point_wkt, 
                        country=country, boundary_type=boundary_type, field_name=field_name,
                        area_ha_approx=area_ha, commit=False
                    )
                    
                    geo_data_to_return = None
                    if s2_index and s2_indexes_to_remove != -1:
                        geo_data_to_return = Utils.get_specific_s2_index_geo_data(dict(geo_data), s2_indexes_to_remove)
                        if isinstance(geo_data_to_return, dict) and "wkt" in geo_data_to_return:
                            geo_data_to_return.pop("wkt")

                    results.append({
                        "status": "created",
                        "message": "Point registered successfully.",
                        "Geo Id": geo_id,
                        "S2 Cell Tokens": geo_data_to_return,
                        "Geo JSON": feature
                    })
                else:
                    results.append({
                        "status": "exists",
                        "message": "Point already registered.",
                        "Geo Id": geo_id_l20,
                        "Geo JSON requested": feature,
                        "Geo JSON registered": Utils.get_geo_json(geo_id_exists_wkt)
                    })

            except Exception as point_error:
                db.rollback()
                results.append({
                    "status": "error",
                    "message": str(point_error),
                    "geo_json": feature
                })

            yield json.dumps({
                "status": "processing",
                "progress": index + 1,
                "total": total_features,
                "percentage": round(((index + 1) / total_features) * 100, 2)
            }) + "\n"

            await asyncio.sleep(0)

        db.commit()
        time_taken = time.time() - start_time

        yield json.dumps({
            "status": "completed",
            "message": "Bulk point registration completed",
            "time_taken_seconds": round(time_taken, 2),
            "results": results
        }) + "\n"

    return StreamingResponse(process_and_stream(), media_type="application/x-ndjson")