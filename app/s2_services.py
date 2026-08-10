# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import geopandas as gpd
import s2sphere as s2
import shapely
from shapely.wkt import loads


class S2Service:

    @staticmethod
    def get_bounding_box_cell_ids(latitudes, longitudes, resolution_level):
        min_level = resolution_level
        max_level = resolution_level
        r = s2.RegionCoverer()
        r.min_level = min_level
        r.max_level = max_level

        lb_lat = min(latitudes)
        ub_lat = max(latitudes)
        lb_lon = min(longitudes)
        ub_lon = max(longitudes)

        lb = s2.LatLng.from_degrees(lb_lat, lb_lon)
        ub = s2.LatLng.from_degrees(ub_lat, ub_lon)
        cell_ids = r.get_covering(s2.LatLngRect.from_point_pair(lb, ub))
        return cell_ids

    @staticmethod
    def wkt_to_cell_ids(field_wkt, resolution_level, point=False):

        try:
            poly = loads(field_wkt)
            if point:
                longs, lats = poly.coords.xy
            else:
                longs, lats = poly.exterior.coords.xy
            longs, lats = longs.tolist(), lats.tolist()
            cell_ids = S2Service.get_bounding_box_cell_ids(lats, longs, resolution_level)
            return cell_ids
        except Exception as e:
            raise Exception(e)


    @staticmethod
    def wkt_to_cell_tokens(field_wkt, resolution_level, point=False):

        try:
            s2_cell_ids = S2Service.wkt_to_cell_ids(field_wkt, resolution_level, point=point)
            s2_token_list = []
            for s2_cell_id in s2_cell_ids:
                s2_token_list.append(s2_cell_id.to_token())

            return s2_token_list
        except Exception as e:
            raise Exception(e)

    @staticmethod
    def get_boundary_coverage(s2_cell_ids, polygon, max_resolution_col_name):

        s2_index__l19_list = []
        p_gdf = gpd.GeoDataFrame()
        idx = 0
        for s2_cell_id in s2_cell_ids:
            s2_cell = s2.Cell(s2_cell_id)
            vertices = []
            for i in range(4):
                vertex = s2_cell.get_vertex(i)
                latlng = s2.LatLng.from_point(vertex)
                vertices.append((latlng.lng().degrees, latlng.lat().degrees))
            geo = shapely.geometry.Polygon(vertices)
            if polygon.intersects(geo):
                s2_index__l19_list.append(s2_cell_id.to_token())
                p_gdf.loc[idx, max_resolution_col_name] = s2_cell_id.to_token()
                p_gdf.loc[idx, 'geometry'] = geo
            idx += 1

        p_gdf.reset_index(drop=True, inplace=True)
        return p_gdf

    @staticmethod
    def get_cell_token_for_lat_long(lat, long):

        s2_cell_token_13 = s2.Cell.from_lat_lng(s2.LatLng.from_degrees(lat, long)).id().parent(13).to_token()
        s2_cell_token_20 = s2.Cell.from_lat_lng(s2.LatLng.from_degrees(lat, long)).id().parent(20).to_token()
        return s2_cell_token_13, s2_cell_token_20

    @staticmethod
    def get_cell_tokens_for_bounding_box(latitudes, longitudes, resolution_level=13):

        s2_cell_ids = S2Service.get_bounding_box_cell_ids(latitudes, longitudes, resolution_level)
        s2_token_list = []
        for s2_cell_id in s2_cell_ids:
            s2_token_list.append(s2_cell_id.to_token())
        return s2_token_list

    @staticmethod
    def get_s2_level_10_polygon(lat, long):
        latlng = s2.LatLng.from_degrees(lat, long)
        cell_id = s2.CellId.from_lat_lng(latlng).parent(10)
        cell = s2.Cell(cell_id)
        coords = []
        for i in range(4):
            vertex = cell.get_vertex(i)
            ll = s2.LatLng.from_point(vertex)
            coords.append([ll.lng().degrees, ll.lat().degrees])
        coords.append(coords[0])
        return {
            "token": cell_id.to_token(),
            "geojson": {
                "type": "Polygon",
                "coordinates": [coords]
            }
        }

