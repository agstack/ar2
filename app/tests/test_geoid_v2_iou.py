import pytest
from app.utils import Utils
from shapely.wkt import loads
import s2geometry as s2g

def compute_cells(wkt_string: str):
    tokens, _ = Utils.generate_geo_id_v2_with_tokens(wkt_string)
    return set(tokens)

def test_iou_fidelity():
    wkt1 = "POLYGON((0 0, 0 0.002, 0.002 0.002, 0.002 0, 0 0))" # 4ha square
    # Offset by 0.001 (50% overlap geometrically)
    wkt2 = "POLYGON((0.001 0, 0.001 0.002, 0.003 0.002, 0.003 0, 0.001 0))"
    
    geom1 = loads(wkt1)
    geom2 = loads(wkt2)
    geometric_iou = geom1.intersection(geom2).area / geom1.union(geom2).area
    
    cells1 = compute_cells(wkt1)
    cells2 = compute_cells(wkt2)
    
    intersection = len(cells1 & cells2)
    union = len(cells1 | cells2)
    cell_iou = intersection / union
    
    # We assert they are within 15% of each other
    assert abs(geometric_iou - cell_iou) < 0.15

def test_measure_threshold_bias():
    # If the user sets a 95% threshold in the AR2 system, what is the geometric overlap actually required?
    # Because S2 cells cover the boundary loosely, the cell union is slightly larger than the polygon.
    # Therefore, cell_iou is usually LOWER than geometric_iou (the cells 'fatten' the shape).
    wkt1 = "POLYGON((0 0, 0 0.002, 0.002 0.002, 0.002 0, 0 0))" 
    geom1 = loads(wkt1)
    
    cells1 = compute_cells(wkt1)
    
    # Let's shift wkt2 slowly until cell_iou drops to exactly 0.95 and measure geometric_iou
    # We will just verify the bias direction: cell_iou < geometric_iou for a shifted identical polygon
    
    # 5% shift
    wkt2 = "POLYGON((0.0001 0, 0.0001 0.002, 0.0021 0.002, 0.0021 0, 0.0001 0))"
    geom2 = loads(wkt2)
    geometric_iou = geom1.intersection(geom2).area / geom1.union(geom2).area
    
    cells2 = compute_cells(wkt2)
    cell_iou = len(cells1 & cells2) / len(cells1 | cells2)
    
    # Bias is defined here: cell intersection drops faster than geometric intersection 
    # because the non-overlapping boundary cells increase the union disproportionately.
    assert cell_iou < geometric_iou
    
    print(f"\nGeometric IoU: {geometric_iou:.3f}, Cell IoU: {cell_iou:.3f}")
    print(f"Bias: {(geometric_iou - cell_iou) * 100:.1f}%")
