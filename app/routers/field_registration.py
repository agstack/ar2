# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import asyncio
import json
import random
import time
import uuid
from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Header,
    HTTPException,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app import geoid_v2
from app.auth import require_hub_user
from app.database import get_db
from app.models import GeoID
from app.s2_services import S2Service
from app.schemas import (
    FieldRegistrationRequest,
    FieldRegistrationResponse,
)
from app.utils import GeoDataUtils, Utils

router = APIRouter(prefix="", tags=["Field Registration"])


@router.post("/register-field-boundary", response_model=FieldRegistrationResponse)
async def register_field_boundary(
    payload: FieldRegistrationRequest,
    automated_field: int | None = Header(None, alias="AUTOMATED-FIELD"),
    user: dict = Depends(require_hub_user),
    db: Session = Depends(get_db)
    ):
    try:
        boundary_type = "automated" if automated_field else "manual"
        
        field_name = payload.field_name
        if not field_name:
            field_name = f"field_{random.randint(1000, 9999)}"

        content_hash = GeoDataUtils.generate_content_hash(payload.wkt)
        existing_record = db.query(GeoID).filter(GeoID.content_hash == content_hash).first()
        
        if existing_record:
            response_data = {
                "message": "Exact geometry already registered.",
                "Geo Id": existing_record.geo_id,
                "Geo Id Short": existing_record.geo_id_short,
                "matched geo ids": [existing_record.geo_id],
                "S2 Cell Tokens": None
            }

            if payload.return_s2_indices and payload.s2_index:
                s2_index_to_fetch = [int(i) for i in payload.s2_index.split(',')]
                s2_indexes_to_remove = Utils.get_s2_indexes_to_remove(s2_index_to_fetch)
                
                if s2_indexes_to_remove != -1:
                    s2_data = dict(existing_record.geo_data) 
                    s2_data = Utils.get_specific_s2_index_geo_data(s2_data, s2_indexes_to_remove)
                    if isinstance(s2_data, dict) and "wkt" in s2_data:
                        s2_data.pop("wkt")
                    response_data["S2 Cell Tokens"] = s2_data

            return response_data

        field_boundary_geo_json = Utils.get_geo_json(payload.wkt)
        lng = field_boundary_geo_json['geometry']['coordinates'][0][0][0]
        lat = field_boundary_geo_json['geometry']['coordinates'][0][0][1]
        
        country = Utils.get_country_from_point([lng, lat])
        area_in_acres = Utils.get_are_in_acres(payload.wkt)

        area_ha = area_in_acres * 0.404686

        if area_in_acres > 1000:
            raise HTTPException(status_code=400, detail="Cannot register a field with Area greater than 1000 acres")

        # The identity is the v2 cover: the polygon's own normalized S2 cell
        # union, hashed. The fixed-level indices below are retained because the
        # API exposes them and because blocking and legacy comparison use them,
        # but they no longer decide identity.
        try:
            v2_tokens, geo_id = geoid_v2.geo_id_with_tokens(payload.wkt)
        except geoid_v2.GeometryUnusable as exc:
            # Refusing is the point. The old path minted a uuid4() here, which
            # produced a registration whose identifier was derived from nothing
            # and could never be recomputed or matched.
            raise HTTPException(
                status_code=422,
                detail=f"geometry cannot yield a content-derived GeoID: {exc}"
            ) from exc

        indices = {
            13: S2Service.wkt_to_cell_tokens(payload.wkt, 13),
            20: S2Service.wkt_to_cell_tokens(payload.wkt, 20),
            geoid_v2.COVER_KEY: v2_tokens,
            geoid_v2.REGIME_KEY: geoid_v2.REGIME_VERSION,
        }

        geo_id_short = geoid_v2.geo_id_short(geo_id)

        # No cascade and no collision escape hatch below. Under v1 a repeated
        # GeoID meant two different fields had landed in the same L13 cells --
        # ~1.2 km across, so routine -- and the code escaped by hashing L20 and
        # then, failing that, by minting a uuid4(). Under v2 a repeated GeoID
        # means the canonicalized geometry is identical, which resolution already
        # reports as same_as at IoU 1.0. So the duplicate is an answer rather
        # than a problem, and it still records the submission lineage.
        action, canonical_geo_id = Utils.resolve_or_register(
            db=db,
            geo_id=geo_id,
            indices=indices,
            threshold=payload.threshold,
            area_ha_approx=area_ha,
            payload=payload.model_dump() if hasattr(payload, "model_dump") else payload.dict(),
            content_hash=content_hash
        )

        if action == "resolved":
            return {
                "message": "Resolved to existing field",
                "Geo Id": canonical_geo_id,
                "resolved_from_threshold": payload.threshold
            }

        target_geo_id = geo_id
        target_geo_id_short = geo_id_short

        if payload.return_s2_indices:
            indices.update({
                8: S2Service.wkt_to_cell_tokens(payload.wkt, 8),
                15: S2Service.wkt_to_cell_tokens(payload.wkt, 15),
                18: S2Service.wkt_to_cell_tokens(payload.wkt, 18),
                19: S2Service.wkt_to_cell_tokens(payload.wkt, 19),
            })
            
        records_list = Utils.records_s2_cell_tokens(indices)

        geo_data = Utils.register_field_boundary(
            db=db, geo_id=target_geo_id, geo_id_short=target_geo_id_short, content_hash=content_hash,
            indices=indices, records_list=records_list, field_wkt=payload.wkt, 
            country=country, boundary_type=boundary_type, field_name=payload.field_name, area_ha_approx=area_ha,
            commit=True
        )

        response_data = {"message": "Field Boundary registered successfully.", "Geo Id": target_geo_id, "Geo Id Short": target_geo_id_short}

        if payload.return_s2_indices and payload.s2_index:
            s2_index_to_fetch = [int(i) for i in payload.s2_index.split(',')]
            s2_indexes_to_remove = Utils.get_s2_indexes_to_remove(s2_index_to_fetch)
            if s2_indexes_to_remove != -1:
                s2_data = dict(geo_data)
                s2_data = Utils.get_specific_s2_index_geo_data(s2_data, s2_indexes_to_remove)
                if isinstance(s2_data, dict) and "wkt" in s2_data:
                    s2_data.pop("wkt")
                response_data["S2 Cell Tokens"] = s2_data

        return response_data

    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Register Field Boundary Error: {e!s}")


# @router.post("/register-field-boundaries-geojson", tags=["Field Registration"])
# async def register_field_boundaries_geojson(
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

#         threshold = data.get('threshold', 95)
#         resolution_level = 20
#         boundary_type = "automated" if automated_field else "manual"
#         results = []

#         for feature in data['features']:
#             try:
#                 field_wkt = Utils.geojson_to_wkt(feature)
#                 geometry_type = feature['geometry']['type']
                
#                 properties = feature.get('properties', {})
#                 field_name = properties.get('field_name')
#                 if not field_name:
#                     field_name = f"field_{random.randint(1000, 9999)}"
                
#                 content_hash = GeoDataUtils.generate_content_hash(field_wkt)
#                 existing_record = db.query(GeoID).filter(GeoID.content_hash == content_hash).first()
#                 if existing_record:
#                     results.append({
#                         "status": "exists",
#                         "message": "Exact geometry already registered.",
#                         "geo_id": existing_record.geo_id,
#                         "geo_json": feature
#                     })
#                     continue

#                 if geometry_type == 'Point':
#                     lat = feature['geometry']['coordinates'][1]
#                     lng = feature['geometry']['coordinates'][0]
#                     indices = {
#                         8: S2Service.wkt_to_cell_tokens(field_wkt, 8, point=True),
#                         13: S2Service.wkt_to_cell_tokens(field_wkt, 13, point=True),
#                         15: S2Service.wkt_to_cell_tokens(field_wkt, 15, point=True),
#                         18: S2Service.wkt_to_cell_tokens(field_wkt, 18, point=True),
#                         19: S2Service.wkt_to_cell_tokens(field_wkt, 19, point=True),
#                         20: S2Service.wkt_to_cell_tokens(field_wkt, 20, point=True),
#                         30: S2Service.wkt_to_cell_tokens(field_wkt, 30, point=True)
#                     }
#                     geo_id = Utils.generate_geo_id(indices[30]) 
#                 else: 
#                     lat = feature['geometry']['coordinates'][0][0][1]
#                     lng = feature['geometry']['coordinates'][0][0][0]
#                     indices = {
#                         8: S2Service.wkt_to_cell_tokens(field_wkt, 8),
#                         13: S2Service.wkt_to_cell_tokens(field_wkt, 13),
#                         15: S2Service.wkt_to_cell_tokens(field_wkt, 15),
#                         18: S2Service.wkt_to_cell_tokens(field_wkt, 18),
#                         19: S2Service.wkt_to_cell_tokens(field_wkt, 19),
#                         20: S2Service.wkt_to_cell_tokens(field_wkt, 20)
#                     }
#                     geo_id = Utils.generate_geo_id(indices[13]) 

#                 country = Utils.get_country_from_point([lng, lat])
#                 area_ha = None
#                 if geometry_type != 'Point':
#                     area_in_acres = Utils.get_are_in_acres(field_wkt)
#                     area_ha = area_in_acres * 0.404686
#                     if area_in_acres > 1000:
#                         results.append({
#                             "status": "skipped",
#                             "message": "Cannot register a field with Area greater than 1000 acres",
#                             "field_area_acres": area_in_acres,
#                             "geo_json": feature
#                         })
#                         continue

#                 geo_id_l20 = Utils.generate_geo_id(indices[20])
#                 geo_id_short = GeoDataUtils.generate_short_geo_id(geo_id)
#                 geo_id_l20_short = GeoDataUtils.generate_short_geo_id(geo_id_l20)
                
#                 s2_index = feature.get('properties', {}).get('s2_index')
#                 s2_indexes_to_remove = -1
#                 if s2_index:
#                     s2_index_to_fetch = [int(i) for i in str(s2_index).split(',')]
#                     s2_indexes_to_remove = Utils.get_s2_indexes_to_remove(s2_index_to_fetch)

#                 records_list = Utils.records_s2_cell_tokens(indices)

#                 geo_id_exists_wkt = Utils.lookup_geo_ids(db, geo_id)
#                 if not geo_id_exists_wkt:
#                     geo_data = Utils.register_field_boundary(
#                         db=db, geo_id=geo_id, geo_id_short=geo_id_short, content_hash=content_hash,
#                         indices=indices, records_list=records_list, field_wkt=field_wkt, 
#                         country=country, boundary_type=boundary_type, field_name=field_name,area_ha_approx=area_ha
#                     )
                    
#                     geo_data_to_return = None
#                     if s2_index and s2_indexes_to_remove != -1:
#                         geo_data_to_return = Utils.get_specific_s2_index_geo_data(dict(geo_data), s2_indexes_to_remove)
#                         if isinstance(geo_data_to_return, dict) and "wkt" in geo_data_to_return:
#                             geo_data_to_return.pop("wkt")

#                     results.append({
#                         "status": "created",
#                         "message": "Field Boundary registered successfully",
#                         "geo_id": geo_id,
#                         "s2_cell_tokens": geo_data_to_return,
#                         "geo_json": feature
#                     })
#                     continue

#                 if geometry_type != 'Point':
#                     s2_index_to_check = indices[20]
#                     matched_geo_ids = Utils.fetch_geo_ids_for_cell_tokens(db, s2_index_to_check)
#                     percentage_matched_geo_ids = Utils.check_percentage_match(db, matched_geo_ids, s2_index_to_check, resolution_level, threshold)
                    
#                     if len(percentage_matched_geo_ids) > 0:
#                         results.append({
#                             "status": "exists",
#                             "message": "Threshold matched for already registered Field Boundary(ies)",
#                             "matched_geo_ids": percentage_matched_geo_ids,
#                             "geo_json": feature
#                         })
#                         continue

#                 geo_id_exists_wkt_l20 = Utils.lookup_geo_ids(db, geo_id_l20)
#                 if not geo_id_exists_wkt_l20:
#                     geo_data = Utils.register_field_boundary(
#                         db=db, geo_id=geo_id_l20, geo_id_short=geo_id_l20_short, content_hash=content_hash,
#                         indices=indices, records_list=records_list, field_wkt=field_wkt, 
#                         country=country, boundary_type=boundary_type, field_name=field_name,area_ha_approx=area_ha
#                     )
                    
#                     geo_data_to_return = None
#                     if s2_index and s2_indexes_to_remove != -1:
#                         geo_data_to_return = Utils.get_specific_s2_index_geo_data(dict(geo_data), s2_indexes_to_remove)
#                         if isinstance(geo_data_to_return, dict) and "wkt" in geo_data_to_return:
#                             geo_data_to_return.pop("wkt")

#                     results.append({
#                         "status": "created",
#                         "message": "Field Boundary registered successfully",
#                         "geo_id": geo_id_l20,
#                         "s2_cell_tokens": geo_data_to_return,
#                         "geo_json": feature
#                     })
#                 else:
#                     results.append({
#                         "status": "exists",
#                         "message": "Field Boundary already registered",
#                         "geo_id": geo_id_l20,
#                         "geo_json_requested": feature
#                     })

#             except Exception as field_error:
#                 db.rollback() 
#                 results.append({
#                     "status": "error",
#                     "message": str(field_error),
#                     "geo_json": feature
#                 })

#         return {"message": "Bulk registration completed", "results": results}

#     except HTTPException:
#         raise
#     except Exception as e:
#         raise HTTPException(status_code=400, detail=f"Bulk Register Error: {str(e)}")

@router.post("/register-field-boundaries-geojson", tags=["Field Registration"])
async def register_field_boundaries_geojson(
    file: UploadFile | None = File(None),
    payload: dict[str, Any] | None = Body(None),
    automated_field: int | None = Header(None, alias="AUTOMATED-FIELD"),
    user: dict = Depends(require_hub_user),
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
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Request Error: {e!s}")

    threshold = data.get('threshold', 95)
    boundary_type = "automated" if automated_field else "manual"
    features = data['features']
    total_features = len(features)

    async def process_and_stream():
        results = []
        
        yield json.dumps({
            "status": "started", 
            "total": total_features, 
            "progress": 0, 
            "message": "Starting bulk registration..."
        }) + "\n"

        start_time = time.time()
        for index, feature in enumerate(features):
            try:
                field_wkt = Utils.geojson_to_wkt(feature)
                geometry_type = feature['geometry']['type']
                
                properties = feature.get('properties', {})
                field_name = properties.get('field_name')
                if not field_name:
                    field_name = f"field_{random.randint(1000, 9999)}"
                
                content_hash = GeoDataUtils.generate_content_hash(field_wkt)
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

                if geometry_type == 'Point':
                    lat = feature['geometry']['coordinates'][1]
                    lng = feature['geometry']['coordinates'][0]
                    indices = {
                        8: S2Service.wkt_to_cell_tokens(field_wkt, 8, point=True),
                        13: S2Service.wkt_to_cell_tokens(field_wkt, 13, point=True),
                        15: S2Service.wkt_to_cell_tokens(field_wkt, 15, point=True),
                        18: S2Service.wkt_to_cell_tokens(field_wkt, 18, point=True),
                        19: S2Service.wkt_to_cell_tokens(field_wkt, 19, point=True),
                        20: S2Service.wkt_to_cell_tokens(field_wkt, 20, point=True),
                        30: S2Service.wkt_to_cell_tokens(field_wkt, 30, point=True)
                    }
                    # A point has no area, so the polygon coverer cannot be used.
                    # Its leaf cell is still content-derived and deterministic,
                    # and it is a descendant of any containing field's cover, so
                    # the existing ancestor probe relates the two unchanged.
                    v2_tokens, geo_id = geoid_v2.point_geo_id_with_tokens(lat, lng)
                    indices[geoid_v2.COVER_KEY] = v2_tokens
                    indices[geoid_v2.REGIME_KEY] = geoid_v2.REGIME_VERSION
                else: 
                    lat = feature['geometry']['coordinates'][0][0][1]
                    lng = feature['geometry']['coordinates'][0][0][0]
                    indices = {
                        8: S2Service.wkt_to_cell_tokens(field_wkt, 8),
                        13: S2Service.wkt_to_cell_tokens(field_wkt, 13),
                        15: S2Service.wkt_to_cell_tokens(field_wkt, 15),
                        18: S2Service.wkt_to_cell_tokens(field_wkt, 18),
                        19: S2Service.wkt_to_cell_tokens(field_wkt, 19),
                        20: S2Service.wkt_to_cell_tokens(field_wkt, 20)
                    }
                    try:
                        v2_tokens, geo_id = geoid_v2.geo_id_with_tokens(field_wkt)
                    except geoid_v2.GeometryUnusable as exc:
                        results.append({
                            "status": "skipped",
                            "message": f"geometry cannot yield a content-derived GeoID: {exc}",
                            "geo_json": feature
                        })
                        yield json.dumps({
                            "status": "processing",
                            "progress": index + 1,
                            "percentage": round(((index + 1) / total_features) * 100, 2)
                        }) + "\n"
                        continue
                    indices[geoid_v2.COVER_KEY] = v2_tokens
                    indices[geoid_v2.REGIME_KEY] = geoid_v2.REGIME_VERSION

                country = Utils.get_country_from_point([lng, lat])
                area_ha = None
                if geometry_type != 'Point':
                    area_in_acres = Utils.get_are_in_acres(field_wkt)
                    area_ha = area_in_acres * 0.404686
                    if area_in_acres > 1000:
                        results.append({
                            "status": "skipped",
                            "message": "Cannot register a field with Area greater than 1000 acres",
                            "field_area_acres": area_in_acres,
                            "geo_json": feature
                        })
                        yield json.dumps({
                            "status": "processing", 
                            "progress": index + 1, 
                            "percentage": round(((index + 1) / total_features) * 100, 2)
                        }) + "\n"
                        continue

                geo_id_short = geoid_v2.geo_id_short(geo_id)
                
                s2_index = feature.get('properties', {}).get('s2_index')
                s2_indexes_to_remove = -1
                if s2_index:
                    s2_index_to_fetch = [int(i) for i in str(s2_index).split(',')]
                    s2_indexes_to_remove = Utils.get_s2_indexes_to_remove(s2_index_to_fetch)

                records_list = Utils.records_s2_cell_tokens(indices)

                if geometry_type != 'Point':
                    payload_dict = {
                        "threshold": threshold,
                        "submitter": feature.get('properties', {}).get('submitter'),
                        "accuracy_class": feature.get('properties', {}).get('accuracy_class')
                    }
                    action, canonical_geo_id = Utils.resolve_or_register(
                        db=db,
                        geo_id=geo_id,
                        indices=indices,
                        threshold=threshold,
                        area_ha_approx=area_ha,
                        payload=payload_dict,
                        content_hash=content_hash
                    )
                    
                    if action == "resolved":
                        results.append({
                            "status": "exists",
                            "message": "Resolved to existing field",
                            "geo_id": canonical_geo_id,
                            "geo_json": feature
                        })
                        yield json.dumps({
                            "status": "processing", 
                            "progress": index + 1, 
                            "percentage": round(((index + 1) / total_features) * 100, 2)
                        }) + "\n"
                        continue

                # Points get no resolution pass -- there is no area to compare --
                # so a repeat has to be caught here or it would violate the unique
                # constraint on geo_id. Two registrations of the same point are
                # the same point; that is the identity working, not a collision.
                if geometry_type == 'Point' and Utils.lookup_geo_ids(db, geo_id):
                    results.append({
                        "status": "exists",
                        "message": "Point already registered",
                        "geo_id": geo_id,
                        "geo_json": feature
                    })
                    yield json.dumps({
                        "status": "processing",
                        "progress": index + 1,
                        "percentage": round(((index + 1) / total_features) * 100, 2)
                    }) + "\n"
                    continue

                # No cascade and no uuid4() fallback: see the note on the single
                # registration path. A repeated v2 GeoID is a same_as, which
                # resolution above has already handled for polygons.
                target_geo_id = geo_id
                target_geo_id_short = geo_id_short

                records_list = Utils.records_s2_cell_tokens(indices)

                geo_data = Utils.register_field_boundary(
                    db=db, geo_id=target_geo_id, geo_id_short=target_geo_id_short, content_hash=content_hash,
                    indices=indices, records_list=records_list, field_wkt=field_wkt, 
                    country=country, boundary_type=boundary_type, field_name=field_name, area_ha_approx=area_ha,
                    commit=False
                )
                
                geo_data_to_return = None
                if s2_index and s2_indexes_to_remove != -1:
                    geo_data_to_return = Utils.get_specific_s2_index_geo_data(dict(geo_data), s2_indexes_to_remove)
                    if isinstance(geo_data_to_return, dict) and "wkt" in geo_data_to_return:
                        geo_data_to_return.pop("wkt")

                results.append({
                    "status": "created",
                    "message": "Field Boundary registered successfully",
                    "geo_id": target_geo_id,
                    "s2_cell_tokens": geo_data_to_return,
                    "geo_json": feature
                })

            except Exception as field_error:  # noqa: BLE001
                db.rollback() 
                results.append({
                    "status": "error",
                    "message": str(field_error),
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
            "message": "Bulk registration completed",
            "time_taken_seconds": round(time_taken, 2),
            "results": results
        }) + "\n"

    return StreamingResponse(process_and_stream(), media_type="application/x-ndjson")