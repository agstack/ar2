# Review packets

One packet per completed task. Each is a pair of committed files:

    packets/<task>.json        the packet: change, evidence, gaps, decision
    packets/<task>.run.json    the harness run backing it

Produce one with:

    harness/bin/stomata                  # so the run record is current
    harness/bin/stomata packet new \
      --author "<you>" --reviewer "<who checks it>" \
      --title "<what you did>" --out packets/<task>.json
    harness/bin/stomata packet check packets/<task>.json
    git add packets/ && git commit

Both files are committed on purpose. `.stomata/state.json` is untracked because it
is rewritten on every run, so a packet pointing at it would resolve for its author
and for nobody else — validation passing on one machine and failing on the
reviewer's. The copy is what makes the evidence checkable by the person who has to
check it.

`packet check` refuses a packet whose run is missing, whose run failed, or whose
pointers do not resolve, and it warns when the run describes a different commit
than the change claims.

Format and a worked example: `harness/docs/REVIEW_PACKETS.md`.
