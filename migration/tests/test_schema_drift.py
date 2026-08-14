"""
Drift guard: models.py mirrors the shipped schemas rather than importing them,
so something has to notice when the real ones change.

These tests parse the real model files with `ast` -- no imports, so no app
config, no database, no settings object -- and fail if a table this import
writes to has gained or lost a column, or if a NOT NULL / UNIQUE constraint the
pipeline relies on has moved.

Each repo is skipped if absent, so `pytest migration` works from an ar2 checkout
alone. The E2E job checks out all three, which is where the full guard runs.
"""

from __future__ import annotations

import ast
import os
import pathlib

import pytest

from ..models import Base, PancakeBase

HERE = pathlib.Path(__file__).resolve().parents[2]

PANCAKE_MODELS = pathlib.Path("services") / "pancake_services" / "grants" / "models.py"

# Searched in order. Set MIGRATION_{AR2,HUB,PANCAKE}_MODELS to override, which is
# what CI does -- sibling checkouts have to live inside the workspace there.
CANDIDATES = {
    "ar2": [HERE / "app" / "models" / "geo_id_model.py"],
    "hub": [
        HERE / "_hub" / "user_models.py",
        HERE.parent / "hub" / "user_models.py",
        HERE.parent / "ar2-hub" / "user_models.py",
    ],
    "pancake": [
        HERE / "_pancake" / PANCAKE_MODELS,
        HERE.parent / "pancake" / PANCAKE_MODELS,
    ],
}


def _locate(which: str) -> pathlib.Path | None:
    override = os.environ.get(f"MIGRATION_{which.upper()}_MODELS")
    if override:
        p = pathlib.Path(override)
        return p if p.exists() else None
    for p in CANDIDATES[which]:
        if p.exists():
            return p
    return None


def _parse_models(path: pathlib.Path) -> dict[str, dict[str, dict]]:
    """table name -> {column name -> {nullable, unique}} from source alone."""
    tree = ast.parse(path.read_text())
    out: dict[str, dict[str, dict]] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        table, columns = None, {}
        for stmt in node.body:
            targets, value = [], None
            if isinstance(stmt, ast.Assign):
                targets, value = stmt.targets, stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.target:
                targets, value = [stmt.target], stmt.value
            if not targets or not isinstance(targets[0], ast.Name):
                continue
            name = targets[0].id
            if name == "__tablename__" and isinstance(value, ast.Constant):
                table = value.value
                continue
            if not isinstance(value, ast.Call):
                continue
            fn = value.func
            fname = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if fname not in ("Column", "mapped_column"):
                continue
            meta = {"nullable": None, "unique": False}
            for kw in value.keywords:
                if kw.arg in ("nullable", "unique") and isinstance(kw.value, ast.Constant):
                    meta[kw.arg] = kw.value.value
            columns[name] = meta
        if table:
            out[table] = columns
    return out


def _mirror(base, table: str) -> set[str]:
    return {c.name for c in base.metadata.tables[table].columns}


@pytest.mark.parametrize("which,table,mirror_base", [
    ("ar2", "geo_ids", Base),
    ("hub", "users", Base),
    ("pancake", "users", PancakeBase),
    ("pancake", "fieldlists", PancakeBase),
    ("ar2", "listmember_edge", Base),
])
def test_mirrored_columns_match_the_real_schema(which, table, mirror_base):
    path = _locate(which)
    if path is None:
        pytest.skip(f"{which} checkout not present")

    real = _parse_models(path)
    assert table in real, f"{table} missing from {path}"

    real_cols = set(real[table])
    ours = _mirror(mirror_base, table)

    assert ours == real_cols, (
        f"{which}.{table} has drifted.\n"
        f"  in the real schema, missing here: {sorted(real_cols - ours)}\n"
        f"  here but not in the real schema:  {sorted(ours - real_cols)}\n"
        f"  source: {path}"
    )


def test_hub_constraints_the_import_depends_on_are_still_there():
    """The rejection rules in db_repo exist because of these four constraints.

    If any of them relaxes, accounts this import currently quarantines would
    import cleanly and the quarantine becomes a false positive.
    """
    path = _locate("hub")
    if path is None:
        pytest.skip("hub checkout not present")

    users = _parse_models(path)["users"]
    for col in ("email", "phone", "first_name", "last_name", "password_hash"):
        assert users[col]["nullable"] is False, f"hub.users.{col} is no longer NOT NULL"
    for col in ("email", "phone"):
        assert users[col]["unique"] is True, f"hub.users.{col} is no longer UNIQUE"


def test_ar2_uniqueness_the_import_depends_on_is_still_there():
    path = _locate("ar2")
    if path is None:
        pytest.skip("ar2 model file not found")

    geo_ids = _parse_models(path)["geo_ids"]
    for col in ("geo_id", "geo_id_short", "content_hash"):
        assert geo_ids[col]["unique"] is True, f"geo_ids.{col} is no longer UNIQUE"


def test_regime_alias_table_is_ours_and_not_ar2s():
    """geo_id_regime_alias must NOT appear in ar2 yet.

    If it does, someone has added it and this mirror should be replaced by the
    real model rather than silently diverging from it.
    """
    path = _locate("ar2")
    if path is None:
        pytest.skip("ar2 model file not found")

    real = _parse_models(path)
    if "geo_id_regime_alias" in real:
        ours = _mirror(Base, "geo_id_regime_alias")
        assert ours == set(real["geo_id_regime_alias"]), (
            "geo_id_regime_alias now exists in ar2 and differs from this mirror; "
            "import the real model instead of mirroring it")
