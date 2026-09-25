# Evaluation Methodology

## What this is

A 15-case golden eval suite for the prior-auth pipeline. It scores four
behavioral dimensions — not just the final yes/no — and is designed to
catch regressions introduced by system prompt changes, model upgrades, or
structural code changes.

The cassette-based replay design means CI runs at zero API cost. You only
spend tokens when you deliberately re-record with `--live`.

---

## How ground truth is established

All expected outcomes are authored by a human reading the policy document
and the synthetic FHIR bundle. The LLM is never used to generate expected
outputs.

For each case:
1. A policy is read (e.g., `data/policies/rotator_cuff_repair_29827.md`).
2. A synthetic FHIR bundle is authored to match a specific clinical scenario.
3. The expected determination, route, per-criterion evidence status, and
   expected citations are written into the case YAML spec by hand.
4. The test suite verifies that every expected citation resolves in the
   generated bundle before any LLM call is made.

The policy documents themselves describe what a human reviewer would require.
Expected outcomes reflect that policy, not a model's interpretation of it.

---

## Metric definitions

### S1 — Determination accuracy

Does the pipeline's final decision match the expected decision?

- For intake-stage cases: did intake reject when it should?
- For eligibility-stage cases: did eligibility reject when it should?
- For determination-stage cases: is `determination.decision` equal to the
  expected `approve` or `insufficient_evidence`?

**Pass:** decision matches exactly.
**Fail:** wrong decision, or the pipeline reached the wrong stage.

### S2 — Routing accuracy

Does the pipeline's routing output match the expected route (`auto` or `hitl`)?

Only scored for determination-stage cases. Intake and eligibility rejections
do not have a route and are marked `skip`.

**Pass:** route matches.
**Fail:** wrong route, or route is absent when one was expected.

### S3 — Criterion-level evidence accuracy

For each criterion declared in `ground_truth.criteria`, does the pipeline's
evidence output match — with the right status *and* citing the right resources?

This is the "right answer, wrong reason" detector. A case can pass S1 (correct
final decision) while failing S3 if the model reached the right conclusion by
citing invented resources.

**Matching strategy:**
- `met` and `not_met` criteria are matched by citation overlap: the pipeline
  evidence item must cite at least one of the expected FHIR resource IDs.
- `insufficient` criteria with no expected citations are matched via final
  decision proxy: if all are insufficient, the determination should be
  `insufficient_evidence`.

**Pass:** all criteria match, with correct status and surviving citations.
**Fail:** any criterion has wrong status, or no matching evidence item found.

The strip-and-log count (cases where `resolve_citations()` removed a ghost
reference) is reported as a note, not a failure — stripping is the intended
behaviour.

### S4 — Citation validity

After the pipeline runs, are all surviving citations in `met` evidence items
resolvable against the bundle?

`resolve_citations()` is supposed to strip unresolvable citations before they
reach the output. This dimension verifies it worked: any surviving citation
for a `met` criterion must resolve to a real bundle entry.

**Pass:** zero unresolved citations in the final output.
**Fail:** one or more surviving citations cannot be found in the bundle.

The count of stripped citations (removed by `resolve_citations`) is reported
for visibility — it is expected for case_007 (ghost citation).

### S5 — Confidence calibration

Does the determination drafter's self-reported `confidence` (0-1) actually
track whether the decision was correct?

**Matching strategy:** Brier score — `mean((confidence - outcome)**2)` across
all determination-stage cases, where `outcome` is 1.0 if S1 (determination
accuracy) passed and 0.0 if it failed. Lower is better; 0 is perfect
calibration, 0.25 is what a constant `confidence=0.5` guess produces.

Unlike S1-S4, this is not a per-case pass/fail check — confidence itself has
no "expected" value in the ground truth (only the decision does), so there is
nothing to gate on per case. S5 is reported, not enforced: it does not fail
CI and is not part of `--baseline-check`.

**Sample size:** only 13 of 15 cases reach the determination stage (the other
2 stop at intake/eligibility, before any confidence is produced). N=13 is far
too small for a statistically defensible calibration curve — see Limitations
below.

---

## Test cases

| ID | Title | Stage | Decision | Route | Key behaviour under test |
|----|-------|-------|----------|-------|--------------------------|
| case_001 | Clean approve — all criteria met | determination | approve | auto | Baseline: all three criteria met, all citations survive |
| case_002 | Insufficient evidence — PT not documented | determination | insufficient_evidence | hitl | C2 silent — no PT records in chart |
| case_003 | Insufficient evidence — no imaging | determination | insufficient_evidence | hitl | C1 silent — no ImagingStudy in chart |
| case_004 | Low-confidence HITL routing | determination | approve | hitl | All criteria met but confidence < 0.85 → HITL |
| case_005 | Malformed bundle — Patient missing | intake | — | — | Intake rejects missing Patient resource |
| case_006 | KL Grade III at lower bound | determination | approve | auto | Marginal meeting of imaging threshold |
| case_007 | Ghost citation — strip-and-log fires | determination | approve | auto | LLM cites a resource referenced in a note but absent as a bundle entry; resolve_citations strips it; criterion stays met via surviving citations |
| case_008 | Brain MRI — no matching policy, wrong policy retrieved via RAG | determination | insufficient_evidence | hitl | No CPT 70553 policy in the corpus; none of the mapped criteria are satisfied by brain-MRI evidence (README Known Limitations #5) |
| case_009 | Lapsed coverage — eligibility rejection | eligibility | — | — | Coverage.status=cancelled → pipeline halts before LLM |
| case_010 | TKA — Grade III OA lower-bound criterion check | determination | approve | auto | All 3 criteria met; C1 tests the "Grade III-IV" boundary; solid documentation → confidence ≥ 0.85 |
| case_011 | Empty chart — all criteria insufficient | determination | insufficient_evidence | hitl | Bundle has only Patient+Coverage; all criteria silent |
| case_012 | Bariatric sleeve — near-threshold HITL, thin C2 documentation | determination | approve | hitl | All 3 criteria technically met, but thin C2 documentation and borderline BMI push confidence below 0.85 → HITL on the confidence branch, not criterion insufficiency |
| case_013 | TKA — near-threshold auto, solid documentation | determination | approve | auto | Paired with case_012 (same CPT family, similar clinical picture) — complete C2 documentation keeps confidence ≥ 0.85 → auto |
| case_014 | Psych deferral — required criterion not_met | determination | insufficient_evidence | hitl | DiagnosticReport documents psychiatric clearance NOT granted |
| case_015 | PT refusal — required criterion not_met | determination | insufficient_evidence | hitl | Observation documents patient refused PT; C2 not_met |

### Known eval failures

S1 is 9/13 on the cases that reach the drafter (11/15 overall; the two
intake/eligibility cases pass by construction) in the recordings made
2026-09-17. The previous recordings were also 9/13, with a different failing
set. No fixture or cassette has been adjusted to make these pass.

| Case | Expected | Recorded | Status |
|------|----------|----------|--------|
| case_004 | approve (HITL on confidence) | insufficient_evidence @ 0.55 | Run-to-run variance: approve @ 0.90 in the previous recording. The drafter holds c2 unresolved because the treating provider is named only by role and physician supervision is not established; the fixture sits on that boundary. |
| case_007 | approve | insufficient_evidence @ 0.72 | Reproducible (0.60 previously). S3 passes 3/3; the drafter still holds c2 unresolved because no named treating provider is recorded. S4 shows 0 stripped citations in both recordings: the recorded model did not cite the ghost resource, so these recordings do not exercise strip-and-log (`tests/test_citation_resolution.py` covers the mechanism). |
| case_008 | insufficient_evidence | approve @ 0.50 | Known: empty criteria vacuously approve (README Known Limitations #5). Failed in both recordings. |
| case_012 | approve (HITL on confidence) | insufficient_evidence @ 0.90 | Reproducible: failed in both recordings, and also fails S3 (fixture c2 expected met, recorded not_met). The recorded drafter treats the missing physician/dietitian supervision as failing the supervised-program criterion; the fixture treats it as met with thin documentation. Not adjudicated, and not a scorer bug. |

case_001 failed S1 in the previous recording (insufficient_evidence @ 0.82) and
passes in the current one, like case_004 in reverse. Each cassette is one
sample of a non-deterministic model, so S1 status can flip between recordings.

---

## Scoreboard

> Populate this table after the first `--live` run and `--update-baseline`.

| Dimension | Scoreable cases | Pass | Fail | Pass rate |
|-----------|----------------|------|------|-----------|
| S1 Determination | — | — | — | — |
| S2 Routing | — | — | — | — |
| S3 Criterion evidence | — | — | — | — |
| S4 Citation validity | — | — | — | — |

To regenerate: `python -m evals.run --live` followed by `python -m evals.run --update-baseline`.

---

## Running locally

```bash
# First-time setup: record cassettes (requires ANTHROPIC_API_KEY)
python -m evals.run --live

# After recording: replay mode (no API key required)
python -m evals.run

# Check against committed baseline
python -m evals.run --baseline-check

# Re-record a single case after a system prompt change
python -m evals.run --live case_007

# Deterministic cases only (no cassettes, no API key)
python -m evals.run --no-llm

# Update the baseline after a legitimate score change
python -m evals.run --update-baseline
```

---

## Limitations — read this before citing these numbers

**15 synthetic cases is a demonstration of method, not statistical validation.**

This suite shows that the evaluation *harness* works: that it can catch
regressions, that it scores the right dimensions, and that CI can run it
without an API key. It does not validate the pipeline's clinical correctness,
and no one should cite a pass rate on 15 hand-authored cases as evidence of
production readiness.

Specific limitations:

1. **Sample size**: 15 cases cannot characterise the distribution of real
   prior-auth requests. A pipeline that passes all 15 can still fail on the
   first real case.

2. **Synthetic data only**: Every bundle was authored by the same person who
   wrote the policy. Real charts have noise, ambiguity, conflicting evidence,
   and edge cases that synthetic data systematically under-represents.

3. **Multi-policy retrieval is now exercised, and it found a real bug**: the
   15 cases span 5 CPT codes/policies. A live eval run surfaced a genuine
   RAG retrieval issue — see README's Known Limitations. Not yet fixed.

4. **S3 matching is heuristic**: Criterion evidence is matched by citation
   overlap, not semantic equivalence. A pipeline could pass S3 while
   extracting evidence from the wrong clinical context, as long as the right
   resource ID appears in the citations.

5. **S4 is a post-hoc check**: Citation validity is verified against the
   bundle after the fact. It does not test whether the model's reasoning
   was sound — only that the citations it produced correspond to real
   resources.

6. **No adversarial cases**: There are no cases designed to elicit prompt
   injection, policy bypass, or reasoning shortcuts. The suite tests the
   happy path and a few error paths; it does not test the security surface.

7. **Calibration sample size**: N=13 confidence/correctness pairs is far too
   small to estimate a reliability diagram or expected calibration error
   (ECE) meaningfully — a single case flipping pass/fail moves the Brier
   score by ~1/13 ≈ 0.077. Treat S5's output as a smoke check that the
   confidence field is wired correctly and roughly tracks correctness, not
   as a statistically defensible calibration curve. A binned reliability
   diagram is planned once the golden set is large enough for bins to hold
   more than 0-1 samples each.

8. **The committed baseline is a placeholder**: `evals/baseline.json` holds
   zero floors on every dimension (`git_sha: "placeholder"`), so
   `--baseline-check`, including the CI step that runs it, cannot fail
   whatever the scores are. It guards nothing until it is populated with
   `python -m evals.run --live && python -m evals.run --update-baseline`,
   which would also lock in the current pass counts (including the failures
   above) as the floor.

What this suite is good for: **catching regressions**. If a system prompt
change causes a previously-passing case to fail, the harness will detect it
in CI. That is its intended purpose. It is not a substitute for clinical
validation, red-teaming, or production monitoring.
