# Licensed under the EUPL, Version 1.2 or – as soon they will be approved by
# the European Commission - subsequent versions of the EUPL (the "Licence");
# You may not use this work except in compliance with the Licence.
# You may obtain a copy of the Licence at:
# https://joinup.ec.europa.eu/software/page/eupl

import hashlib
import json
import os

import geojson
import geopandas as gpd
from dotenv import load_dotenv
from shapely import ops, wkb
from shapely.geometry import Point, mapping
from shapely.wkt import loads as load_wkt
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import geoid_v2
from app.models.geo_id_model import GeoID, GeoIDAlias

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
    _wrs_gdf = None

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
            if Utils._wrs_gdf is None:
                wrs_gdf = gpd.read_file(world_shp_file)
                Utils._wrs_gdf = wrs_gdf.to_crs(4326)
            
            matches = Utils._wrs_gdf[Utils._wrs_gdf.contains(p)]
            if not matches.empty:
                return matches.reset_index(drop=True).CNTRY_NAME.iloc[0]
            return ''
        except Exception as e:  # noqa: BLE001
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
    def generate_geo_id_v2(wkt_string: str) -> str:
        return geoid_v2.geo_id(wkt_string)

    @staticmethod
    def generate_geo_id_v2_with_tokens(wkt_string: str):
        """Delegates to app.geoid_v2, which is the single implementation.

        The earlier inline version passed only the exterior ring to S2Loop and
        kept only the first part of a MultiPolygon, so a field with a hole
        covered its hole and a multi-part field took the identity of one
        fragment. Both matter on real data, because make_valid() *produces*
        MultiPolygons from self-intersecting input.
        """
        return geoid_v2.geo_id_with_tokens(wkt_string)

    @staticmethod
    def lookup_geo_ids(db: Session, geo_id_to_lookup: str):

        record = db.query(GeoID).filter(GeoID.geo_id == geo_id_to_lookup).first()
        if record:
            return record.geo_data.get('wkt')
        return None

    @staticmethod
    def records_s2_cell_tokens(s2_cell_tokens_dict: dict) -> list:
        """Flatten the per-level covers into the s2_cells column.

        Only list values are flattened. The indices dict also carries the regime
        label, which is a bare string -- extending a list with it would splat it
        into individual characters and write "v" and "2" in as if they were cell
        tokens.
        """
        all_tokens = []
        for s2_cell_tokens in s2_cell_tokens_dict.values():
            if isinstance(s2_cell_tokens, (list, tuple, set)):
                all_tokens.extend(s2_cell_tokens)

        return list(set(all_tokens))

    @staticmethod
    def register_field_boundary(
        db: Session, geo_id: str, geo_id_short: str, content_hash: str, 
        indices: dict, records_list: list, field_wkt: str, country: str, 
        boundary_type: str, field_name: str | None = None, area_ha_approx: float | None = None, commit: bool = True
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
            if commit:
                db.commit()
            return geo_data
        except Exception:
            if commit:
                db.rollback()
            raise

    @staticmethod
    def fetch_geo_ids_for_cell_tokens(db: Session, s2_cell_tokens: list, domain: str = "") -> list:

        records = db.query(GeoID.geo_id).filter(
            GeoID.s2_cells.overlap(s2_cell_tokens)
        ).all()
        
        return [r.geo_id for r in records]

    @staticmethod
    def check_percentage_match(db: Session, matched_geo_ids: list, s2_index_list: list, resolution_level: int, threshold: int, area_new: float = 0.0) -> dict:
        """Classify candidates as same_as or child_of by area-exact cover overlap.

        Two changes from the token-set version.

        OVERLAP IS NOW AREA-EXACT. Set intersection over tokens cannot see
        ancestor/descendant overlap: one L16 cell and its own four L17 children
        cover the identical region and share no token, so the old arithmetic
        scored them as zero. That was harmless while every cover sat at a single
        level, and is wrong the moment v2 stores normalized multi-level covers.
        On uniform-level covers the two agree bit for bit, so no v1 decision
        changes.

        THE AREA PRE-FILTER IS GONE. It gated the entire same_as branch behind
        `area_ratio >= threshold`, and area_ratio is 0 whenever either area is
        missing -- so a field with no recorded area could never resolve to an
        existing one, no matter how exactly the geometry matched. It silently
        turned every import with absent area into a duplicate. It existed as a
        cheap prune, and exact IoU makes it redundant as well as harmful.

        `area_new` is accepted for signature compatibility and no longer read.
        """
        result = {"same_as": [], "child_of": []}
        if not matched_geo_ids or not s2_index_list:
            return result

        candidates = db.query(GeoID.id, GeoID.geo_id, GeoID.geo_data, GeoID.s2_cells, GeoID.created_at)\
                       .filter(GeoID.geo_id.in_(matched_geo_ids)).all()

        for cand_id, cand_geo_id, cand_geo_data, cand_s2_cells, cand_created_at in candidates:
            cand_tokens = Utils._comparison_cover(cand_geo_data, cand_s2_cells, resolution_level)
            if not cand_tokens:
                continue

            iou, containment = geoid_v2.iou_and_containment(s2_index_list, cand_tokens)

            if iou * 100.0 >= threshold:
                result["same_as"].append((cand_geo_id, cand_created_at, cand_id))
            elif containment * 100.0 >= threshold:
                result["child_of"].append((cand_geo_id, cand_created_at, cand_id))

        return result

    @staticmethod
    def _comparison_cover(geo_data: dict | None, s2_cells: list | None, resolution_level: int) -> list:
        """The cover to compare a candidate against.

        Prefers the canonical v2 cover, falls back to the stored fixed-level
        index, and finally to s2_cells. The fallbacks are what let a v2
        registration resolve against fields registered under v1, which is the
        whole point of keeping them: during the migration window both regimes
        are present in the same table.
        """
        geo_data = geo_data or {}
        return (
            geo_data.get(geoid_v2.COVER_KEY)
            or geo_data.get(str(resolution_level))
            or s2_cells
            or []
        )

    @staticmethod
    def resolve_or_register(db: Session, geo_id: str, indices: dict, threshold: int, area_ha_approx: float, payload: dict, content_hash: str):
        """Resolve a new cover against existing fields, or report it as new.

        Compares on the canonical v2 cover when the caller supplied one, and on
        the fixed L20 index otherwise. Blocking stays at L13 either way: identity
        is the fine cover, and L13 is only the candidate pre-filter that the
        existing s2_cells index already supports.
        """
        probe = indices.get(geoid_v2.COVER_KEY) or indices[20]
        blocking = (
            geoid_v2.blocking_key(probe)
            if geoid_v2.COVER_KEY in indices
            else indices[13]
        )

        matched_geo_ids = Utils.fetch_geo_ids_for_cell_tokens(db, blocking)
        matches = Utils.check_percentage_match(db, matched_geo_ids, probe, 20, threshold, area_ha_approx)
        
        if matches["same_as"]:
            # Resolve to earliest
            matches["same_as"].sort(key=lambda x: (x[1], x[2])) # Sort by created_at, then id
            canonical_geo_id = matches["same_as"][0][0]
            
            # Write alias
            alias_record = GeoIDAlias(
                canonical_geo_id=canonical_geo_id,
                alias_content_hash=content_hash,
                submitter=payload.get("submitter"),
                accuracy_class=payload.get("accuracy_class"),
                relation="same_as"
            )
            db.add(alias_record)
            db.commit()
            return "resolved", canonical_geo_id
            
        if matches["child_of"]:
            matches["child_of"].sort(key=lambda x: (x[1], x[2]))
            parent_geo_id = matches["child_of"][0][0]
            
            alias_record = GeoIDAlias(
                canonical_geo_id=parent_geo_id,
                alias_content_hash=content_hash,
                submitter=payload.get("submitter"),
                accuracy_class=payload.get("accuracy_class"),
                relation="child_of"
            )
            db.add(alias_record)
            return "nested", None
            
        return "new", None

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
            geo_data.pop(str_key, None)
        return geo_data
    
    @staticmethod
    def geojson_to_wkt(geojson_feature):

        try:
            from shapely.geometry import shape
            geometry = shape(geojson_feature['geometry'])
            return geometry.wkt
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Failed to convert GeoJSON to WKT: {e!s}")

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
        domain: str | None = None, s2_index: str | None = None, boundary_type: str | None = None
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
        except Exception as e:  # noqa: BLE001
            print(f"Analytics Error: Ensure your GeoID model has a 'created_at' column. Details: {e}")
            return []

    @staticmethod
    def get_eudr_multipolygon(wkt_string: str) -> dict:
        import shapely
        from shapely.geometry import MultiPolygon, Polygon, mapping
        geom = load_wkt(wkt_string)
        
        # Ensure 2D and 6-decimal precision
        geom = shapely.ops.transform(lambda x, y, *args: (round(x, 6), round(y, 6)), geom)
        
        if isinstance(geom, Polygon):
            geom = MultiPolygon([geom])
            
        if isinstance(geom, MultiPolygon):
            polys = []
            for poly in geom.geoms:
                polys.append(shapely.geometry.polygon.orient(poly, sign=1.0))
            geom = MultiPolygon(polys)
            
        geojson_dict = {"type": "Feature"}
        geojson_string = geojson.dumps(mapping(geom))
        geojson_dict["geometry"] = json.loads(geojson_string)
        return geojson_dict