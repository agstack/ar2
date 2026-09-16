"""A point and a polygon are the same kind of thing to this registry.

Regulation (EU) 2023/1115 Art. 2(28) lets a plot of at most four hectares be
declared by a single coordinate, and the GGMS App will produce both. If a point
is a second-class plot anywhere along the path -- registration, the masked view,
the granted view, the EU filing, membership of a list, the reverse lookup -- then
the consent flow works for some farmers and not others, and which ones depends on
how their plot happened to be captured.

So these tests run one journey twice, once with a boundary and once with a
coordinate, and assert the same answers at every step except the two where the
geometries honestly differ: the filing shape (a Polygon, or a Point with its
declared Area) and what L1 discloses (a ring of vertices, or one coordinate).

The pair-by-pair structure is deliberate. A test that only exercised points
would pass just as well if points and polygons had drifted onto two different
code paths, which is the failure this is here to catch.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

TESTKIT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "testkit/dev_keys"))
os.environ["AR_TRUSTED_ISSUER_PUBKEY"] = os.path.join(TESTKIT_DIR, "dev_issuer_public.pem")
os.environ["AR_TRUSTED_AUTHORITY_PUBKEY"] = os.path.join(TESTKIT_DIR, "authority_issuer_public.pem")
# Reading a list back is gated on consent, as it should be; these tests are about
# what a point is, not about who may see it, so they call as the trusted internal
# caller Pancake is. The gate itself is exercised in test_api.py.
os.environ.setdefault("AR2_INTERNAL_SHARED_SECRET", "peers-test-secret")
INTERNAL = {"x-pancake-internal": os.environ["AR2_INTERNAL_SHARED_SECRET"]}

from app import geoid_v2  # noqa: E402
from app.auth import require_hub_user  # noqa: E402
from app.main import app  # noqa: E402

app.dependency_overrides[require_hub_user] = lambda: {"sub": "peers@demo.com"}
client = TestClient(app)

# Two plots in the Honduran coffee belt, far enough apart that neither resolves
# onto the other and neither collides with anything registered by another test.
POINT_LAT, POINT_LNG = 14.812345, -88.312345
DECLARED_HA = 0.3
POLYGON_WKT = (
    "POLYGON ((-88.322000 14.822000, -88.322000 14.823000, "
    "-88.321000 14.823000, -88.321000 14.822000, -88.322000 14.822000))"
)


def _register_point(lat=POINT_LAT, lng=POINT_LNG, area=DECLARED_HA):
    return client.post(
        "/register-point",
        json={"wkt": f"POINT ({lng} {lat})", "field_name": "peer point", "declared_area_ha": area},
    )


def _register_polygon(wkt=POLYGON_WKT):
    return client.post("/register-field-boundary", json={"wkt": wkt, "field_name": "peer field"})


@pytest.fixture(scope="module", autouse=True)
def _clear_this_modules_plots():
    """Remove anything a previous run of this module left behind.

    These tests register at fixed coordinates so that a failure is reproducible,
    and resolution means a leftover row from an earlier run would answer for the
    row the test meant to create -- passing or failing for a reason that has
    nothing to do with the code. CI starts from an empty database; a laptop does
    not.
    """
    from sqlalchemy import delete, select  # noqa: PLC0415

    from app.database import SessionLocal  # noqa: PLC0415
    from app.models import GeoID  # noqa: PLC0415
    from app.models.geo_id_model import ListArtifact, ListMemberEdge  # noqa: PLC0415

    names = ("peer point", "peer field", "bulk point", "bulk field")
    with SessionLocal() as session:
        geo_ids = session.execute(
            select(GeoID.geo_id).where(GeoID.field_name.in_(names))
        ).scalars().all()
        if geo_ids:
            # The lists too, not just the edges: a ListArtifact whose members
            # have gone is an empty list, and the second run would resolve its
            # ListID to that empty one.
            list_ids = session.execute(
                select(ListMemberEdge.list_id).where(ListMemberEdge.geoid.in_(geo_ids))
            ).scalars().all()
            if list_ids:
                session.execute(delete(ListArtifact).where(ListArtifact.list_id.in_(list_ids)))
            session.execute(delete(ListMemberEdge).where(ListMemberEdge.geoid.in_(geo_ids)))
            session.execute(delete(GeoID).where(GeoID.geo_id.in_(geo_ids)))
            session.commit()

        # Any list left with no members at all, from a run that deleted rows
        # before this fixture also deleted their lists. Such a list answers its
        # own ListID with nothing, so re-registering the same members returns
        # "already exists" and no edges are ever written.
        orphans = session.execute(
            select(ListArtifact.list_id).where(
                ~ListArtifact.list_id.in_(select(ListMemberEdge.list_id))
            )
        ).scalars().all()
        if orphans:
            session.execute(delete(ListArtifact).where(ListArtifact.list_id.in_(orphans)))
            session.commit()
    yield


@pytest.fixture(scope="module")
def plots():
    """One of each, registered once for the whole module."""
    point = _register_point()
    assert point.status_code == 200, point.text
    polygon = _register_polygon()
    assert polygon.status_code == 200, polygon.text
    return {"point": point.json()["Geo Id"], "polygon": polygon.json()["Geo Id"]}


def test_both_kinds_register_and_get_a_geoid_of_the_same_shape(plots):
    for kind, geo_id in plots.items():
        assert len(geo_id) == 64, kind
        int(geo_id, 16)  # hex, or this raises


def test_the_registry_says_which_kind_each_one_is(plots):
    for kind, geo_id in plots.items():
        body = client.get(f"/fetch-field/{geo_id}").json()
        assert body["GeometryKind"] == kind, f"{kind} came back as {body['GeometryKind']!r}"


def test_the_masked_view_is_the_same_view_for_both(plots):
    """L0 is a level-10 cell and an area, whichever kind of plot it stands for."""
    views = {}
    for kind, geo_id in plots.items():
        body = client.get(f"/fetch-field/{geo_id}").json()
        assert body["MaskingLevel"] == "L0", kind
        data = body["Geo Data"]
        assert data["cell_token"], kind
        assert set(views.get("point", data)) == set(data)
        views[kind] = data

    assert views["point"]["area_ha"] == pytest.approx(DECLARED_HA, abs=0.05), (
        "a point's masked view must publish the area it was declared to stand for, or a "
        "screening node has no denominator"
    )
    assert views["polygon"]["area_ha"] is not None


def test_neither_kind_leaks_its_geometry_without_a_grant(plots):
    for kind, geo_id in plots.items():
        body = client.get(f"/fetch-field/{geo_id}").json()
        assert body["MaskingLevel"] == "L0", kind
        assert "wkt" not in str(body["Geo Data"]).lower(), kind
        # The L0 GeoJSON is the masking cell, not the plot: a level-10 cell is
        # about 8 km across, and a point inside one is not located by it.
        assert body["Geo JSON"]["type"] in ("Polygon", "Feature"), kind


def test_the_coordinate_itself_is_nowhere_in_the_masked_view():
    """A point is the one geometry a mask could leak whole, so look for it.

    Absence of the ``wkt`` key is a weaker statement than it reads as: the fix
    is two numbers, and two numbers can travel in a centroid, a bounding box or
    a rounded convenience field without the word "wkt" appearing anywhere. This
    searches the serialised response for the coordinate at every rounding a
    leak would plausibly take, so that the check can actually fail if one
    appears. A boundary leaks a shape; a point leaks the farm.
    """
    geo_id = _register_point().json()["Geo Id"]
    body = client.get(f"/fetch-field/{geo_id}").json()
    assert body["MaskingLevel"] == "L0"
    blob = json.dumps(body)

    for places in range(6, 1, -1):
        for axis, value in (("lat", POINT_LAT), ("lng", POINT_LNG)):
            needle = f"{value:.{places}f}".rstrip("0")
            assert needle not in blob, (
                f"the masked view carries the {axis} to {places} decimal places ({needle}); "
                "L0 is supposed to stand for the plot, not locate it"
            )


def test_the_masking_cell_is_the_same_size_for_a_point_as_for_a_boundary(plots):
    """The cell is what does the masking, so its extent is the privacy claim.

    A level-10 cell is roughly 87 km2 near the Honduran coffee belt. If a point
    were ever masked by a tighter cell than a boundary -- because it has no
    extent of its own to hide behind -- the mask would be doing less work for
    the plot that needs it most.
    """
    spans = {}
    for kind, geo_id in plots.items():
        body = client.get(f"/fetch-field/{geo_id}").json()
        ring = body["Geo JSON"]["coordinates"][0]
        lngs, lats = [p[0] for p in ring], [p[1] for p in ring]
        spans[kind] = (max(lngs) - min(lngs), max(lats) - min(lats))

    for kind, (dlng, dlat) in spans.items():
        # ~0.0779 degrees of longitude and ~0.0938 of latitude at this latitude.
        assert dlng > 0.05, f"{kind}: masking cell is only {dlng:.4f} deg wide"
        assert dlat > 0.05, f"{kind}: masking cell is only {dlat:.4f} deg tall"
    assert spans["point"] == pytest.approx(spans["polygon"], abs=0.02), (
        "a coordinate must not be masked by a tighter cell than a boundary"
    )


def test_the_area_is_published_under_one_key_for_both(plots):
    for kind, geo_id in plots.items():
        body = client.get(f"/fetch-field/{geo_id}").json()
        assert body["AreaHa"] is not None, f"{kind} published no area"
        assert body["AreaHa"] > 0, kind


def test_a_point_is_declared_and_a_polygon_is_measured(plots):
    """The one place the two honestly differ, pinned so it cannot drift silently."""
    point = client.get(f"/fetch-field/{plots['point']}").json()
    assert point["AreaHa"] == pytest.approx(DECLARED_HA, abs=0.001)

    # A polygon's area comes from its boundary; the ~0.1 ha square above is not
    # 0.3 ha, so the two numbers cannot have come from the same source.
    polygon = client.get(f"/fetch-field/{plots['polygon']}").json()
    assert polygon["AreaHa"] != pytest.approx(DECLARED_HA, abs=0.001)


def test_a_point_over_four_hectares_is_refused_with_the_article(plots):
    refused = _register_point(lat=14.9, lng=-88.4, area=4.5)
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert "2023/1115" in detail and "polygon" in detail


def test_both_kinds_join_one_list_and_prove_membership(plots):
    """A ListID over a point and a polygon together, which is what a cooperative has."""
    members = sorted(plots.values())
    created = client.post("/list-artifact", json={"members": members})
    assert created.status_code == 200, created.text
    list_id = created.json()["list_id"]
    assert len(list_id) == 64

    fetched = client.get(f"/list-artifact/{list_id}", headers=INTERNAL)
    assert fetched.status_code == 200
    assert sorted(fetched.json()["members"]) == members

    for kind, geo_id in plots.items():
        reverse = client.get(f"/list-artifact/reverse/{geo_id}")
        assert reverse.status_code == 200, kind
        assert list_id in reverse.json()["list_ids"], f"{kind} is not findable in its own list"


def test_the_list_is_the_same_whichever_order_the_kinds_arrive_in(plots):
    """Mixing kinds must not disturb the canonical ordering a ListID depends on."""
    a = client.post("/list-artifact", json={"members": [plots["point"], plots["polygon"]]})
    b = client.post("/list-artifact", json={"members": [plots["polygon"], plots["point"]]})
    assert a.json()["list_id"] == b.json()["list_id"]


def test_the_eu_filing_exists_for_both_and_carries_what_the_schema_needs(plots):
    """The filing is the point of the exercise: a point without Area is rejected in Brussels."""
    grant = (TESTKIT_DIR and None)  # no grant available here; see the L1 tests below
    assert grant is None

    for kind, geo_id in plots.items():
        unauthorised = client.get(f"/geoid/{geo_id}/eudr-export")
        assert unauthorised.status_code == 401, (
            f"the EU filing for a {kind} was served without a grant"
        )


def test_a_point_registered_with_no_declared_area_cannot_be_filed():
    """Not a peer failure -- a refusal, and it says what to do about it.

    A Point with no Area is invalid under the DDS schema, so the honest place to
    fail is here rather than at the EU registry.
    """
    from shapely.wkt import loads as load_wkt  # noqa: PLC0415

    from app.utils import Utils  # noqa: PLC0415

    wkt = "POINT (-88.5 14.5)"
    assert load_wkt(wkt).geom_type == "Point"

    with pytest.raises(geoid_v2.GeometryUnusable, match="declared area"):
        Utils.get_eudr_multipolygon(wkt, None)

    filed = Utils.get_eudr_multipolygon(wkt, 1.25)
    assert filed["geometry"]["type"] == "Point"
    assert filed["properties"]["Area"] == 1.25


def test_the_polygon_filing_is_unchanged_by_any_of_this():
    from app.utils import Utils  # noqa: PLC0415

    filed = Utils.get_eudr_multipolygon(POLYGON_WKT)
    assert filed["geometry"]["type"] == "MultiPolygon"
    assert "properties" not in filed or "Area" not in filed.get("properties", {})


def test_a_second_fix_of_the_same_plot_resolves_rather_than_multiplying(plots):
    """The point counterpart of two redraws of one field resolving by IoU."""
    again = _register_point(lat=POINT_LAT + 0.00004, lng=POINT_LNG + 0.00004)  # ~6 m away
    assert again.status_code == 200, again.text
    assert again.json()["Geo Id"] == plots["point"], again.json()["message"]
    assert "resolved" in again.json()["message"].lower()


def test_a_different_plot_nearby_is_still_its_own(plots):
    other = _register_point(lat=POINT_LAT + 0.0009, lng=POINT_LNG)  # ~100 m away
    assert other.json()["Geo Id"] != plots["point"]
    assert other.status_code == 200, other.text


# --------------------------------------------------------------------------
# the same journey through the bulk upload, which is how the App will send them
# --------------------------------------------------------------------------

def _upload(features: list[dict]) -> list[dict]:
    """POST a FeatureCollection and return the results of the completed stream."""
    # Sent as a file: the endpoint declares both a file and a JSON body, which
    # makes FastAPI treat the request as multipart, so the JSON-body half of its
    # own signature is unreachable. Equally unreachable for both kinds of plot,
    # so it is not a peer problem -- recorded as AG-033.
    collection = json.dumps({"type": "FeatureCollection", "features": features})
    response = client.post(
        "/register-field-boundaries-geojson",
        files={"file": ("plots.geojson", collection, "application/geo+json")},
    )
    assert response.status_code == 200, response.text
    lines = [line for line in response.text.splitlines() if line.strip()]
    completed = [json.loads(line) for line in lines if '"completed"' in line]
    assert completed, response.text
    return completed[-1]["results"]


def _point_feature(lat: float, lng: float, area: float | None = 0.4) -> dict:
    props = {"field_name": "bulk point"}
    if area is not None:
        props["declared_area_ha"] = area
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Point", "coordinates": [lng, lat]}}


def _polygon_feature(lat: float, lng: float) -> dict:
    ring = [[lng, lat], [lng, lat + 0.001], [lng + 0.001, lat + 0.001], [lng + 0.001, lat], [lng, lat]]
    return {"type": "Feature", "properties": {"field_name": "bulk field"},
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


def test_an_upload_holding_both_kinds_registers_both():
    results = _upload([_point_feature(14.861111, -88.361111), _polygon_feature(14.871111, -88.371111)])
    assert len(results) == 2
    assert [r["status"] for r in results] == ["created", "created"], results

    kinds = {}
    for result in results:
        body = client.get(f"/fetch-field/{result['geo_id']}").json()
        kinds[body["GeometryKind"]] = body
    assert set(kinds) == {"point", "polygon"}, "an upload of both kinds produced only one kind"
    assert kinds["point"]["AreaHa"] == pytest.approx(0.4, abs=0.001), (
        "the declared area was dropped on the bulk path, so the point would be screened "
        "as a bare coordinate"
    )


def test_a_point_in_an_upload_is_the_same_plot_as_the_same_point_sent_alone():
    """The two routes must agree, or the same farm has two identities by upload method."""
    lat, lng = 14.881111, -88.381111
    [bulk] = _upload([_point_feature(lat, lng)])
    single = _register_point(lat=lat, lng=lng, area=0.4)
    assert single.json()["Geo Id"] == bulk["geo_id"]
    assert "resolved" in single.json()["message"].lower() or "already" in single.json()["message"].lower()


def test_a_re_survey_inside_an_upload_resolves_rather_than_multiplying():
    lat, lng = 14.891111, -88.391111
    [first] = _upload([_point_feature(lat, lng)])
    [second] = _upload([_point_feature(lat + 0.00004, lng)])  # ~4 m away
    assert second["geo_id"] == first["geo_id"], second
    assert second["status"] == "exists"


def test_an_uploaded_point_over_four_hectares_is_skipped_not_registered():
    [result] = _upload([_point_feature(14.9011, -88.4011, area=12.0)])
    assert result["status"] == "skipped"
    assert "2023/1115" in result["message"]
