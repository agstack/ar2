import json
import hashlib
import base64
import pyproj
import geojson
import geopandas as gpd
from shapely.wkt import loads as load_wkt
from shapely.geometry import mapping, shape, Point
from shapely import ops, wkb
from functools import partial
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import GeoID

import os
from dotenv import load_dotenv

load_dotenv()



class GeoDataUtils:

    @staticmethod
    def generate_short_geo_id(geo_id: str) -> str:
        hash_object = hashlib.sha256(geo_id.encode('utf-8'))
        return hash_object.hexdigest()[:16]

    @staticmethod
    def generate_content_hash(wkt_string: str) -> str:
        geom = load_wkt(wkt_string)
        canonical_wkb = wkb.dumps(geom)
        return hashlib.sha256(canonical_wkb).hexdigest()

class Utils:

    @staticmethod
    def get_geo_json(field_wkt: str) -> dict:
        geojson_dict = {"type": "Feature"}
        geojson_string = geojson.dumps(mapping(load_wkt(field_wkt)))
        geojson_dict["geometry"] = json.loads(geojson_string)
        return geojson_dict

    @staticmethod
    def get_country_from_point(coords: list) -> str:

        p = Point(coords)
        world_shp_file = os.getenv("world_shp_file_PATH")
        
        try:
            wrs_gdf = gpd.read_file(world_shp_file)
            wrs_gdf = wrs_gdf.to_crs(4326)
            return wrs_gdf[wrs_gdf.contains(p)].reset_index(drop=True).CNTRY_NAME.iloc[0]
        except Exception as e:
            print(f"Country detection error: {e}")
            return ''

    @staticmethod
    def get_are_in_acres(wkt: str) -> float:
        from pyproj import Proj, Transformer
        
        geom = load_wkt(wkt)
        
        proj_in = Proj('EPSG:4326')
        proj_out = Proj(proj='aea', lat_1=geom.bounds[1], lat_2=geom.bounds[3])
        
        transformer = Transformer.from_proj(proj_in, proj_out, always_xy=True)
        
        geom_area = ops.transform(transformer.transform, geom)

        area_in_sq_km = geom_area.area / 1000000
        area_in_acres = area_in_sq_km * 247.105
        
        return area_in_acres

    @staticmethod
    def generate_geo_id(s2_cell_tokens: list) -> str:
        s2_tuple = tuple(s2_cell_tokens)
        m = hashlib.sha256()
        for s in s2_tuple:
            m.update(s.encode())
        return m.hexdigest()

    @staticmethod
    def lookup_geo_ids(db: Session, geo_id_to_lookup: str):

        record = db.query(GeoID).filter(GeoID.geo_id == geo_id_to_lookup).first()
        if record:
            return record.geo_data.get('wkt')
        return None

    @staticmethod
    def records_s2_cell_tokens(s2_cell_tokens_dict: dict) -> list:

        all_tokens = []
        for res_level, s2_cell_tokens in s2_cell_tokens_dict.items():
            all_tokens.extend(s2_cell_tokens)
        
        return list(set(all_tokens))

    @staticmethod
    def register_field_boundary(
        db: Session, geo_id: str, geo_id_short: str, content_hash: str, 
        indices: dict, records_list: list, field_wkt: str, country: str, 
        boundary_type: str, field_name: str = None, area_ha_approx: float = None  # <--- Added parameter
    ):

        try:
            geo_data = {'wkt': field_wkt}
            for res_level, s2_cell_tokens in indices.items():
                geo_data[str(res_level)] = s2_cell_tokens

            geo_id_record = GeoID(
                geo_id=geo_id,
                geo_id_short=geo_id_short,
                content_hash=content_hash,
                country=country,
                boundary_type=boundary_type,
                field_name=field_name,
                area_ha_approx=area_ha_approx,
                geo_data=geo_data,
                s2_cells=records_list
            )

            db.add(geo_id_record)
            db.commit()
            
            return geo_data
        except Exception as e:
            db.rollback()
            raise e

    @staticmethod
    def fetch_geo_ids_for_cell_tokens(db: Session, s2_cell_tokens: list, domain: str = "") -> list:

        records = db.query(GeoID.geo_id).filter(
            GeoID.s2_cells.overlap(s2_cell_tokens)
        ).all()
        
        return [r.geo_id for r in records]

    @staticmethod
    def check_percentage_match(db: Session, matched_geo_ids: list, s2_index_list: list, resolution_level: int, threshold: int) -> list:

        percentage_matched_geo_ids = []
        s2_index_set = set(s2_index_list)

        for matched_geo_id in matched_geo_ids:
            record = db.query(GeoID).filter(GeoID.geo_id == matched_geo_id).first()
            if not record:
                continue
            
            geo_id_cell_tokens = record.geo_data.get(str(resolution_level), [])
            geo_id_cell_set = set(geo_id_cell_tokens)
            
            if not geo_id_cell_set:
                continue

            intersection = len(s2_index_set & geo_id_cell_set)
            union = len(s2_index_set | geo_id_cell_set)
            percentage_match = (intersection / float(union)) * 100
            
            if percentage_match > threshold:
                percentage_matched_geo_ids.append(matched_geo_id)
                
        return percentage_matched_geo_ids

    @staticmethod
    def get_s2_indexes_to_remove(s2_indexes: list):
        valid_s2_indexes_set = {8, 13, 15, 18, 19, 20}
        s2_indexes_set = set(s2_indexes)
        if valid_s2_indexes_set & s2_indexes_set:
            return list(valid_s2_indexes_set - s2_indexes_set)
        return -1

    @staticmethod
    def get_specific_s2_index_geo_data(geo_data: dict, s2_indexes_to_remove: list) -> dict:

        for key in s2_indexes_to_remove:
            str_key = str(key)
            if str_key in geo_data:
                del geo_data[str_key]
        return geo_data
    
    @staticmethod
    def geojson_to_wkt(geojson_feature):

        try:
            from shapely.geometry import shape
            geometry = shape(geojson_feature['geometry'])
            return geometry.wkt
        except Exception as e:
            raise ValueError(f"Failed to convert GeoJSON to WKT: {str(e)}")

    @staticmethod
    def get_percentage_overlap_two_fields(db: Session, geo_id_field_1: str, geo_id_field_2: str) -> float:

        record_1 = db.query(GeoID).filter(GeoID.geo_id == geo_id_field_1).first()
        record_2 = db.query(GeoID).filter(GeoID.geo_id == geo_id_field_2).first()

        if not record_1 or not record_2:
            raise AttributeError('Please provide valid Geo Ids.')

        field_1_tokens = set(record_1.geo_data.get('20', []))
        field_2_tokens = set(record_2.geo_data.get('20', []))

        if not field_1_tokens or not field_2_tokens:
            return 0.0

        overlap = field_1_tokens & field_2_tokens
        
        if len(field_1_tokens) > len(field_2_tokens):
            percentage_overlap = (len(overlap) / len(field_1_tokens)) * 100
        else:
            percentage_overlap = (len(overlap) / len(field_2_tokens)) * 100

        return round(percentage_overlap, 2)

    @staticmethod
    def fetch_field_centroid_by_wkt(field_wkt: str) -> list:

        geom = load_wkt(field_wkt)
        centroid = geom.centroid
        return [centroid.x, centroid.y]

    @staticmethod
    def fetch_fields_for_a_point_two_way(
        db: Session, s2_cell_token_13: str, s2_cell_token_20: str, 
        domain: str = None, s2_index: str = None, boundary_type: str = None
    ) -> list:

        query = db.query(GeoID).filter(GeoID.s2_cells.contains([s2_cell_token_13]))
        
        if boundary_type:
            query = query.filter(GeoID.boundary_type == boundary_type)

        records = query.all()
        fetched_fields = []
        
        for record in records:
            l20_tokens = set(record.geo_data.get('20', []))
            if s2_cell_token_20 in l20_tokens:
                field_info = {
                    "Geo Id": record.geo_id,
                    "Geo Id Short": record.geo_id_short,
                    "Field Name": record.field_name,
                    "Geo JSON": Utils.get_geo_json(record.geo_data.get('wkt'))
                }
                fetched_fields.append(field_info)
                
        return fetched_fields

    @staticmethod
    def get_row_count_by_country(db: Session) -> list:
        results = db.query(
            GeoID.country, 
            func.count(GeoID.geo_id)
        ).group_by(GeoID.country).all()
        
        return [{"country": r[0] if r[0] else "Unknown", "count": r[1]} for r in results]

    @staticmethod
    def get_row_count_by_month(db: Session) -> list:
        try:
            # Groups by YYYY-MM format
            results = db.query(
                func.to_char(GeoID.created_at, 'YYYY-MM').label('month'),
                func.count(GeoID.geo_id)
            ).group_by('month').order_by('month').all()
            
            return [{"month": r[0], "count": r[1]} for r in results]
        except Exception as e:
            print(f"Analytics Error: Ensure your GeoID model has a 'created_at' column. Details: {e}")
            return []