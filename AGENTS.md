# Working in this repository

## Before you start

Run `harness/bin/stomata brief`. It prints the section at the bottom of this file,
which is the list of defects that have already happened here more than once.

The reason this exists in a file that loads automatically, rather than in a
document someone has to remember to open: on 2026-08-14 the correct warning about
a specific failure mode was written into a guidance document, and on 2026-08-19
that exact failure happened anyway. The warning was accurate, reviewed, and
archived where nobody read it again at the moment it applied. Prose does not bind,
and a lesson nobody encounters at the right time is not a lesson.

## Before you finish

Run `harness/bin/stomata run`, or `--full` at a checkpoint to include mutations.
Twelve checks; every one exists because of a specific incident recorded in
`harness/docs/RATIONALE.md`.

Two things to know about how to treat a failure:

**Do not narrow a check to get past it.** If a guard blocks a change you believe is
correct, one of the two is wrong and it is worth a minute to work out which. Often
it is the guard — they do go stale — and then the fix is to change it deliberately
and say so in the commit message. What check J refuses is the count of assertions
falling quietly, because that is how a guard came to certify an import that could
not run.

**If a check fires on something genuinely fine, that is a bug in the check.** Fix
the check rather than working around it. A check that cries wolf becomes noise,
and noise is what discredits the checks that were earned.

## Recording a new lesson

When a defect turns out to be the second occurrence of something:

```
harness/bin/stomata lesson add --key <recurrence-key> \
  --what "what happened this time" --where "path or document"
```

Merging on a recurrence key is the mechanism, not bookkeeping. The same pain
reported under three different wordings reads as three unlucky one-offs; under one
key it reads as a pattern with three occurrences, which is what makes leaving it
unheld a visible choice rather than an oversight.

Then either mechanize it and name the check in `enforced_by`, or set
`advisory_because` and say why it cannot be. Check K accepts both and rejects
silence, because silence looks identical to having dealt with it.

Do not add a check speculatively. A check with no incident behind it becomes noise,
then gets waived, then gets deleted, and takes the credibility of the real checks
with it.

## Do not edit the section below

It is generated from `.stomata/lessons.json` by `stomata brief --write`. Edit the
ledger and regenerate; check L fails when the two disagree. Hand-editing it would
create two copies of one fact drifting apart, which is `twin-divergence` — the most
frequent lesson in the ledger, and one this file would then be committing itself.

---


<!-- stomata:lessons:begin -->

These are defects that have already happened here, more than once each. They
are generated from `.stomata/lessons.json` by `stomata brief --write`; edit the
ledger, not this block, or check L will fail on the next run.

### gate-reports-green-while-blind (7 occurrences)

**When:** A check, test or assertion reports success.

**Do:** Establish that it can fail. Show it failing on purpose, or show the count it produces changing when the underlying thing changes. A gate that cannot distinguish good from bad is worse than no gate, because it is believed.

*Enforced by: harness/stomata/checks.py, harness/tests/test_stomata.py*

### twin-divergence (6 occurrences)

**When:** One fact is represented in two places -- a mirror and the system it mirrors, a function and its caller, the same logic in two packages, a value in two config files.

**Do:** Change both in the same commit, and add a test that fails when they disagree. Do not rely on remembering the second one, because the second one is what gets forgotten.

*Enforced by: migration/tests/test_points_are_importable.py, migration/tests/test_schema_drift.py::test_mirrored_constraints_match_the_real_schema, stomata.json*

### label-diverges-from-outcome (5 occurrences)

**When:** You are naming or counting what a run did, in a report, a log line, a status field or a document.

**Do:** Make the word mean one outcome. If a category can contain both a success and a failure, split it. Assert that the categories sum to the total and do not overlap, because every individual number can be computed correctly and the totals still describe a run that did not happen.

*Enforced by: migration/tests/test_report_arithmetic.py*

### claim-not-in-the-evidence (4 occurrences)

**When:** You are about to quote a number, or cite a run, screenshot or log as evidence.

**Do:** Open the artifact and find the number in it. Cite the run that produced it, at the commit under review. A figure that cannot be located in the evidence is a recollection, and recollections drift toward what we hoped.

*Enforced by: harness/stomata/packet.py, harness/tests/test_stomata.py*

### prose-warning-instead-of-a-check (3 occurrences)

**When:** You have identified a failure mode and are about to write it down in a document, review comment or email.

**Do:** Write the check as well, in the same change, or record explicitly that it cannot be mechanized and why. A paragraph is read once by whoever was in the conversation and then archived; it does not bind the person who arrives next, and it does not bind its own author a week later.

*Enforced by: harness/stomata/checks.py, harness/stomata/lessons.py, harness/tests/test_lessons.py*

### guard-weakened-to-pass (2 occurrences)

**When:** A guard, assertion or lint rule stands between you and a change you believe is correct.

**Do:** Change the claim or change the code, never quietly narrow the guard. If the guard is genuinely stale, say so in the commit message and re-record the baseline, so that weakening it costs a sentence someone can read.

*Enforced by: harness/stomata/checks.py, harness/tests/test_lessons.py::test_removing_an_assertion_from_a_guard_fails*

<!-- stomata:lessons:end -->
