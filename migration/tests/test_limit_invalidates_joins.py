"""--limit truncates fields only, so every join figure it produces is an artifact.

`run.py` passes `--limit` to `import_fields` but not to `import_profiles`. Every
profile therefore runs against a truncated registry: most of its GeoIDs were
never imported, so they cannot resolve. The result is a characteristic and
entirely misleading shape --

    accounts created     stays at the full profile count
    fieldlists created   collapses toward zero
    join failures        inflates toward the total association count

-- which looks exactly like a broken join key or a corpus of profiles that own no
fields, and is neither. `--sample` does not have this problem: it draws fields
and the profiles that own them together, so the two phases stay consistent.

These tests pin the shape, so nobody debugs it a second time, and pin the warning
that `run.py` now prints.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from migration.pipeline import import_fields, import_profiles
from migration.repo import InMemoryRepo
from migration.run import LIMIT_WARNING, main
from migration.sample import SampledSource, build_sample
from migration.sources import FixtureSource


def _run(limit=None):
    """Exactly what run.py does: limit the fields, never the profiles."""
    src = FixtureSource()
    repo = InMemoryRepo()
    import_fields(src, repo, limit=limit)
    return import_profiles(src, repo)


def test_unlimited_run_has_only_the_deliberate_join_failures():
    """Baseline. The fixture plants orphan refs on purpose; nothing else fails."""
    p = _run()
    assert p.fieldlists_created > 0
    assert p.join_failure_total == 2, (
        "the fixture plants exactly two orphan profile refs; a different number "
        "means the join changed, not the limit"
    )


def test_limit_inflates_join_failures_and_collapses_fieldlists():
    full = _run()
    small = _run(limit=20)

    assert small.accounts_created == full.accounts_created, (
        "accounts should NOT drop -- profiles are not limited, which is the trap"
    )
    assert small.fieldlists_created < full.fieldlists_created
    assert small.join_failure_total > full.join_failure_total


def test_the_collapse_is_monotonic_in_the_limit():
    """Tighter limits look progressively more like a catastrophic join failure."""
    seen = [(lim, _run(limit=lim)) for lim in (None, 40, 20, 10)]
    lists = [p.fieldlists_created for _, p in seen]
    fails = [p.join_failure_total for _, p in seen]
    accounts = {p.accounts_created for _, p in seen}

    assert lists == sorted(lists, reverse=True), lists
    assert fails == sorted(fails), fails
    assert len(accounts) == 1, (
        f"account count must be independent of --limit, saw {accounts}"
    )


def test_sample_preserves_the_true_join_failure_count():
    """The coherent alternative: --sample keeps profiles with their fields."""
    src = FixtureSource()
    fields = list(src.iter_fields())
    profiles = list(src.iter_profiles())

    sample = build_sample(fields, profiles, budget=60, per_stratum_cap=200)
    repo = InMemoryRepo()
    sampled = SampledSource(src, sample)
    import_fields(sampled, repo)
    p = import_profiles(sampled, repo)

    assert len(sample.field_ids) < len(fields), "sample should be a subset"
    assert p.join_failure_total == 2, (
        "--sample must not manufacture join failures the way --limit does"
    )
    assert p.fieldlists_created > 0


# ------------------------------------------------------------- the warning

def test_the_runner_warns_when_limit_is_set(capsys):
    main(["--source", "fixture", "--limit", "20"])
    out = capsys.readouterr().out
    assert "EVERY JOIN FIGURE BELOW IS AN ARTIFACT" in out
    assert "--limit=20 IS SET" in out
    assert "an artifact and means nothing" in out


def test_the_runner_does_not_warn_without_limit(capsys):
    main(["--source", "fixture"])
    out = capsys.readouterr().out
    assert "IS AN ARTIFACT" not in out
    assert "a user who will not see one of their fields" in out


def test_the_warning_names_the_three_unreadable_figures():
    for figure in ("join failures", "fieldlists created", "associations mapped"):
        assert figure in LIMIT_WARNING
