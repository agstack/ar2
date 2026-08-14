"""
Area-exact IoU and containment over S2 token covers.

Replaces the token-set arithmetic in ar2 `Utils.check_percentage_match`, which
has two independent defects once covers are normalized and therefore multi-level:

  1. Token equality cannot see ancestor/descendant overlap. One L16 cell and its
     own four L17 children cover the identical region and share no token, so set
     intersection scores them as zero overlap. This is the dominant error and
     leaf-weighting alone does not repair it.
  2. Cell counts stop being area once levels are mixed.

Measured on two 200 m squares offset 10 m (true IoU 0.9048):

    set cardinality (old code)              0.3171
    set intersection + leaf weighting       0.2975   <- weighting alone: no help
    cell-union intersection + leaf weights  0.8889   <- correct

On uniform-level v1 covers this is bit-identical to the old arithmetic, so
adopting it cannot change any v1 resolution decision. See
workplan/2026-08/evidence/iou_fix_20260813.py for the standalone proof.
"""

from __future__ import annotations

from dataclasses import dataclass

import s2geometry as s2g

from .geoid_v2 import leaf_cells_from_tokens

NEW = "new"
SAME_AS = "same_as"
CHILD_OF = "child_of"


def _union_from_tokens(tokens) -> s2g.S2CellUnion:
    cu = s2g.S2CellUnion()
    cu.Init([s2g.S2CellId.FromToken(t).id() for t in tokens])   # Init normalizes
    return cu


def _tokens_of(cu: s2g.S2CellUnion) -> list[str]:
    return [cid.ToToken() for cid in cu.cell_ids()]


def iou_and_containment(tokens_a, tokens_b) -> tuple[float, float]:
    """Area-exact (IoU, containment) between two covers.

    containment is intersection over the smaller region -- the nesting test
    behind child_of. Empty input yields (0.0, 0.0) rather than raising.
    """
    if not tokens_a or not tokens_b:
        return 0.0, 0.0

    a = _union_from_tokens(tokens_a)
    b = _union_from_tokens(tokens_b)

    # Cell-union intersection subdivides as needed, so ancestor/descendant
    # overlap is counted. Plain token-set intersection cannot see it.
    intersection = a.Intersection(b)

    union_tokens = set(_tokens_of(a)) | set(_tokens_of(b))
    union = _union_from_tokens(union_tokens)

    inter_leaves = leaf_cells_from_tokens(_tokens_of(intersection))
    union_leaves = leaf_cells_from_tokens(_tokens_of(union))
    if union_leaves == 0:
        return 0.0, 0.0

    smaller = min(
        leaf_cells_from_tokens(_tokens_of(a)),
        leaf_cells_from_tokens(_tokens_of(b)),
    )

    iou = inter_leaves / float(union_leaves)
    containment = inter_leaves / float(smaller) if smaller else 0.0

    # Cheap, and either one catches the whole class of level-blind bug.
    assert 0.0 <= iou <= 1.0, f"IoU out of range: {iou}"
    assert 0.0 <= containment <= 1.0, f"containment out of range: {containment}"
    return iou, containment


@dataclass(frozen=True)
class Resolution:
    outcome: str                  # new | same_as | child_of
    canonical_geo_id: str | None
    iou: float
    containment: float

    @property
    def is_new(self) -> bool:
        return self.outcome == NEW


def resolve(
    tokens: list[str],
    candidates: list[tuple[str, list[str]]],
    threshold_pct: float = 95.0,
) -> Resolution:
    """Decide whether a cover is new, the same as a candidate, or nested in one.

    `candidates` is [(geo_id, tokens)] -- already narrowed by the L13 blocking
    key. Ties resolve to the highest IoU, and same_as always wins over child_of.

    The caller is responsible for preferring the *earliest* registration among
    equal-IoU same_as matches; that ordering is a database concern, not a
    geometric one.
    """
    best_same: tuple[float, str] | None = None
    best_child: tuple[float, str] | None = None
    best_iou = 0.0
    best_cont = 0.0

    for cand_geo_id, cand_tokens in candidates:
        iou, containment = iou_and_containment(tokens, cand_tokens)
        best_iou = max(best_iou, iou)
        best_cont = max(best_cont, containment)

        if iou * 100.0 >= threshold_pct:
            if best_same is None or iou > best_same[0]:
                best_same = (iou, cand_geo_id)
        elif containment * 100.0 >= threshold_pct and (best_child is None or containment > best_child[0]):
            best_child = (containment, cand_geo_id)

    if best_same is not None:
        return Resolution(SAME_AS, best_same[1], best_same[0], best_cont)
    if best_child is not None:
        return Resolution(CHILD_OF, best_child[1], best_iou, best_child[0])
    return Resolution(NEW, None, best_iou, best_cont)
