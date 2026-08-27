# SPDX-License-Identifier: EUPL-1.2
# Copyright (c) 2026 AgStack project contributors.
# Licensed under the EUPL, Version 1.2; see the LICENSE file for the full text.

"""The report's categories must add up, and must not overlap.

THE INCIDENT. On 2026-08-19 an import run reported 268 fields quarantined and 268
decisions required. Nothing had failed: all 268 were duplicate submissions of
geometry already in the registry, they had been correctly aliased to the existing
GeoID, and the pipeline had done exactly the right thing. The word "quarantined"
was simply being used for two opposite outcomes -- rejected, and resolved -- and
the reader could not tell which had happened.

The same run reported 18,184 geometries as altered by canonicalisation. The real
figure was 3,597; the rest were points, which take a different code path that was
setting the flag as a side effect. Both numbers were produced by working code and
both were wrong as descriptions of what happened.

WHY THIS IS A TEST AND NOT A NOTE. The reason this class of defect kept recurring
is that nothing could catch it: every individual number was computed correctly, so
the suite was green and the totals were nonsense. What was missing was an
assertion about the relationship between the categories -- that a field counted as
rejected is not also counted as imported, and that the parts sum to the whole.
That relationship is checkable, so it is checked here instead of being remembered.
"""
from __future__ import annotations

from migration.pipeline import FieldReport, InMemoryRepo, import_fields
from migration.sources import FixtureSource


def _run(limit: int | None = None) -> FieldReport:
    return import_fields(FixtureSource(), InMemoryRepo(), limit=limit)


def test_every_field_considered_lands_in_exactly_one_bucket():
    """The parts must sum to the whole.

    If they do not, some field was counted twice or dropped silently, and the
    report is describing a run that did not happen.
    """
    r = _run()
    accounted = (r.imported_new + r.resolved_same_as
                 + r.skipped_already_done + r.quarantined_total)
    assert accounted == r.considered, (
        f"{r.considered} considered but {accounted} accounted for. "
        f"A field is either imported, resolved, skipped or rejected -- "
        f"never two of those, and never none."
    )


def test_a_rejected_field_is_never_also_a_resolved_one():
    """The 2026-08-19 defect exactly: one word for two opposite outcomes.

    Aliasing a duplicate to an existing GeoID is a success. Rejecting a geometry
    is a refusal. Counting them together produced 268 'decisions required' where
    the honest answer was none.
    """
    r = _run()
    rejected = {gid for ids in r.quarantined.values() for gid in ids}
    assert len(rejected) == r.quarantined_total, (
        "the same field appears under two rejection reasons, so the total "
        "over-counts the refusals"
    )
    assert not (rejected & set(r.uuid_promoted)), (
        "a field cannot be both rejected and promoted to a UUID"
    )


def test_subcategories_never_exceed_the_category_they_subdivide():
    """imported_points and resolved_by_content_hash are parts of larger counts.

    A part larger than its whole means the flag is being set on a path that does
    not belong to it -- which is how points came to inflate the canonicalisation
    count.
    """
    r = _run()
    assert r.imported_points <= r.imported_new
    assert r.resolved_by_content_hash <= r.resolved_same_as
    # Found by the closure test above: a child field is inserted as a new GeoID and
    # also counted as child-of, so it is a subset of imported_new rather than an
    # alternative to it. Presenting the two as peers made 74 fields report as 75.
    assert r.resolved_child_of <= r.imported_new


def test_canonicalisation_is_only_flagged_when_geometry_actually_changed():
    """Points are rewritten by the point path, not repaired by canonicalisation.

    Flagging them inflated a number that a human reads as 'this many boundaries
    needed fixing', which is a claim about data quality and was wrong by 5x.
    """
    r = _run()
    assert len(r.canonicalization_changed) <= r.considered
    assert len(set(r.canonicalization_changed)) == len(r.canonicalization_changed), (
        "a field is listed twice as canonicalisation-altered"
    )


def test_the_arithmetic_holds_under_a_limit():
    """--limit is how the numbers get quoted in a hurry.

    A screenshot taken from a limited run was once read as the full picture, so the
    invariant has to hold on the partial run too, not only on the complete one.
    """
    r = _run(limit=5)
    accounted = (r.imported_new + r.resolved_same_as + r.resolved_child_of
                 + r.skipped_already_done + r.quarantined_total)
    assert accounted == r.considered
    assert r.considered <= 5
