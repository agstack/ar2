# SPDX-License-Identifier: EUPL-1.2
# Copyright (c) 2026 AgStack project contributors.
# Licensed under the EUPL, Version 1.2; see the LICENSE file for the full text.

"""Pins must import, not quarantine.

The first full run against the live AR 1.0 registry quarantined 14,594 of 28,282
fields -- 51.6% -- under a single reason, "unusable_geometry", and 14,229 of the
26,265 user-to-field associations then failed to join because a quarantined
field never enters the alias table that the join reads. Better than half the
registry, and better than half of every user's holdings, did not arrive.

The cause was not the data. AR 1.0 accepted point registrations, and a pin has
no area, so the polygon coverer cannot describe one. AR2's own registration
路 has always handled points; the importer had no point path at all, so every pin
reached a bare `except Exception` and was filed as broken geometry.

Two things follow, and this module holds the line on both.

First, pins import, however AR 1.0 wrote them down -- as a POINT, or as a ring
whose vertices are all the same position, which is a pin written as a boundary
rather than a broken boundary.

Second, a quarantine reason has to be specific enough to act on. "14,594
unusable" supports no decision whatever: it cannot distinguish a missing code
path from a corrupt row, and those need opposite responses. It was reported as a
question about policy when it was a question about code.
"""
from __future__ import annotations

import pytest

from migration import geoid_v2 as g2
from migration.pipeline import import_fields
from migration.repo import InMemoryRepo
from migration.sources import LegacyField


class _Fields:
    """A source of exactly the geometries handed to it."""

    def __init__(self, *wkts: str):
        self._wkts = wkts

    def iter_fields(self, limit=None):
        for i, wkt in enumerate(self._wkts):
            yield LegacyField(
                v1_geo_id=f"v1-{i}", wkt=wkt, area_ha=None, v1_kind="l13_hash")


# A pin as AR 1.0 stored it, in each of the shapes it used.
PIN_WKT = "POINT(77.5 12.9)"
PIN_AS_RING = "POLYGON((77.5 12.9, 77.5 12.9, 77.5 12.9, 77.5 12.9))"
PIN_AS_SEGMENT = "LINESTRING(77.5 12.9, 77.5 12.9)"
FIELD_WKT = "POLYGON((77.5 12.9, 77.51 12.9, 77.51 12.91, 77.5 12.91, 77.5 12.9))"


def _run(*wkts):
    return import_fields(_Fields(*wkts), InMemoryRepo())


def test_a_pin_imports_rather_than_quarantining():
    report = _run(PIN_WKT)

    assert report.quarantined_total == 0, (
        f"a point registration was rejected: {report.quarantined}. "
        "This is the 14,594 defect: AR 1.0 accepted pins and the importer "
        "could not read them back."
    )
    assert report.imported_new == 1
    assert report.imported_points == 1


@pytest.mark.parametrize("wkt", [PIN_WKT, PIN_AS_RING, PIN_AS_SEGMENT])
def test_a_pin_imports_however_ar1_wrote_it_down(wkt):
    """A collapsed ring is a pin recorded as a boundary, not a broken boundary.

    Rejecting it loses a real registration over a choice of notation the user
    never made.
    """
    report = _run(wkt)
    assert report.quarantined_total == 0, f"{wkt} was quarantined"
    assert report.imported_points == 1


def test_every_spelling_of_one_pin_lands_on_one_identifier():
    """Otherwise the import manufactures several identities for one place."""
    ids = {g2.point_geo_id_with_tokens(*g2.point_coords(w))[1]
           for w in (PIN_WKT, PIN_AS_RING, PIN_AS_SEGMENT)}
    assert len(ids) == 1, f"one pin, {len(ids)} identifiers: {ids}"


def test_the_two_implementations_agree_on_points():
    """A pin registered through the API and the same pin imported are one row.

    app.geoid_v2 serves live registration and migration.geoid_v2 serves the
    import. If they diverge, importing a pin that AR2 already holds creates a
    second identifier for one position, and the duplicate is invisible because
    both look canonical.
    """
    pytest.importorskip("app.geoid_v2", reason="AR2 app package not importable")
    from app import geoid_v2 as live

    for lat, lng in [(12.9, 77.5), (-34.6, -58.4), (0.0, 0.0), (89.9, 179.9)]:
        assert live.point_geo_id_with_tokens(lat, lng) == \
               g2.point_geo_id_with_tokens(lat, lng), \
               f"the two point implementations disagree at {lat},{lng}"


def test_a_pin_and_a_field_are_not_confused():
    """Both import, and they are different things with different identifiers."""
    report = _run(PIN_WKT, FIELD_WKT)

    assert report.quarantined_total == 0
    assert report.imported_new == 2
    assert report.imported_points == 1, "the polygon was miscounted as a pin"


def test_genuinely_broken_geometry_still_quarantines():
    """The point path must not become a way to accept anything at all.

    A line between two distinct positions is not a pin and not a field. It has
    no area and no single position, so there is nothing to identify it by, and
    admitting it under a surrogate key would put an unidentifiable row in the
    registry.
    """
    report = _run("LINESTRING(77.5 12.9, 77.6 13.0)")

    assert report.imported_new == 0
    assert report.quarantined_total == 1


def test_a_quarantine_reason_is_specific_enough_to_act_on():
    """One undifferentiated bucket is a number, not a finding.

    The live run reported 14,594 rejections under one reason, which left the
    reader unable to tell a missing code path from corrupt data -- and it was in
    fact a missing code path. Reasons carry the fault, so the count becomes a
    work list.
    """
    report = _run("LINESTRING(77.5 12.9, 77.6 13.0)", "NOT WKT AT ALL")

    assert report.quarantined_total == 2
    reasons = set(report.quarantined)
    assert reasons != {"unusable_geometry"}, (
        "every rejection was filed under the same opaque reason; the report "
        "says how many failed and nothing about why"
    )
    assert len(reasons) == 2, f"distinct faults collapsed into: {reasons}"


def test_area_is_derived_so_the_inventory_bands_mean_something():
    """Every field reporting None puts the whole registry in one band.

    The live inventory bucketed all 28,282 fields as "unknown" area, which
    describes the registry not at all. Area is no longer load-bearing for
    identity, so this is a reporting fault rather than a correctness one -- but
    a report nobody can read is not evidence.
    """
    # 1 km square at the equator, so roughly 100 ha.
    side = 1000 / 111_320.0
    square = f"POLYGON((0 0, {side} 0, {side} {side}, 0 {side}, 0 0))"

    measured = g2.area_ha(square)
    assert measured == pytest.approx(100.0, rel=0.01), measured
    assert g2.area_ha(PIN_WKT) is None, "a pin has no area and must not report 0"


# ---------------------------------------------------------------------------
# what the report says happened
# ---------------------------------------------------------------------------

def test_an_exact_duplicate_counts_as_resolved_not_rejected():
    """A duplicate that resolved correctly must not be reported as a problem.

    Identical canonical geometry aliases to the existing row, and the v1
    identifier keeps resolving. Nothing is lost and nothing is pending. It was
    filed under quarantined, which put 268 of a 28,282-field run into a bucket
    the report then listed as needing a policy decision -- making a run that
    rejected 7 fields read as a run with 275 open problems. A reader acts on the
    label, so the label has to be true.
    """
    report = _run(FIELD_WKT, FIELD_WKT)

    assert report.quarantined_total == 0, (
        f"a resolved duplicate was reported as rejected: {report.quarantined}")
    assert report.resolved_same_as == 1
    assert report.resolved_by_content_hash == 1


def test_exact_matches_are_distinguishable_from_threshold_matches():
    """Certainty and judgement should not share a number.

    An exact content match cannot change. A threshold match is a decision that a
    different threshold would make differently, and only the second is worth
    revisiting when the threshold is questioned.
    """
    report = _run(FIELD_WKT, FIELD_WKT)

    assert report.resolved_by_content_hash <= report.resolved_same_as
    threshold_matches = report.resolved_same_as - report.resolved_by_content_hash
    assert threshold_matches == 0, (
        f"identical geometry was scored as a threshold match: {threshold_matches}")


def test_a_point_is_not_counted_as_altered_geometry():
    """A pin has no boundary to repair, so the question does not apply to it.

    Asking anyway counted every one of 14,587 imported pins as altered geometry,
    moving the reported figure from 3,597 to 18,184 and converting a real signal
    about boundary repair into a headcount of points.
    """
    report = _run(PIN_WKT, PIN_AS_RING, PIN_AS_SEGMENT)

    assert report.imported_points == 3
    assert len(report.canonicalization_changed) == 0, (
        f"points were counted as altered boundaries: "
        f"{report.canonicalization_changed}")


def test_the_point_count_is_reported_at_all():
    """A registry that is half pins is a different thing to plan around.

    The total hides which it is, and on the first full run this was the single
    most consequential fact and appeared nowhere in the output.
    """
    report = _run(PIN_WKT, FIELD_WKT)

    assert hasattr(report, "imported_points")
    assert report.imported_points == 1
    assert report.imported_new == 2
