# AI Prior-Authorization System — Compliance Audit Packet

**Generated:** 2026-08-08T11:39:00+00:00
**Git commit:** `7f577f7`
**Model:** `claude-opus-4-8`
**Eval run:** `evals/results/run_20260802_172528.json` (2026-08-02T17:25:28.270472+00:00)
**Cost report:** not available

This packet is machine-generated from live system data. It does not require
manual maintenance. To regenerate after any system change:

    python -m prior_auth_agent.audit_packet

The packet describes the system at the Git SHA above. A different commit, a
different model, or a new eval run produces a different packet. Do not use
a packet whose SHA does not match the deployed system.

---
## 1. System Identity

| Component | Value |
|-----------|-------|
| LLM model | `claude-opus-4-8` |
| Confidence threshold | 0.85 |
| Criteria Mapper prompt | `700f2dd524ca1549`  (SHA-256[:16] of SYSTEM string, commit `7f577f7`) |
| Evidence Extractor prompt | `479d79ee670e7b3f`  (SHA-256[:16] of SYSTEM string, commit `7f577f7`) |
| Determination prompt | `40990d39c2435f5c`  (SHA-256[:16] of SYSTEM string, commit `7f577f7`) |

*Prompt hashes are computed from each node's `SYSTEM` constant at generation time. A changed prompt produces a different hash and this table changes on next regeneration.*

---

## 2. Governance Invariants

| Rule | Description | Decision(s) | Enforcing test | Status |
|------|-------------|-------------|----------------|--------|
| No AI denial | The AI cannot frame, recommend, or auto-finalize a denial. Permitted outputs are `approve` and `insufficient_evidence` only. A human makes any denial. | D9 | `tests/test_no_denial.py` | **PASS** (4/4) |
| Deterministic autonomy gate | Auto-finalization is pure Python: confidence ≥ 0.85 AND decision = approve AND no required criterion is insufficient. No LLM call in the gate. | D3 | `tests/test_no_denial.py` | **PASS** (4/4) |
| Bilateral citation gate (hard reject) | A criterion marked `met` must carry both a policy-side quote and a chart-side FHIR citation. Missing either triggers hard rejection before determination — not a soft HITL route, a hard discard. | D2 / D10 | `tests/test_citation_gate.py` | **PASS** (8/8) |
| Citation existence check | FHIR citations are resolved against the submitted bundle post-LLM. Unresolvable citations are stripped; a criterion with no surviving citations is downgraded to `insufficient`. | D2 / D10 | `tests/test_citation_resolution.py` | **PASS** (6/6) |
| Tamper-evident audit log | Every audit record carries SHA-256 `prev_hash` and `entry_hash`. Editing, deleting, or reordering any record breaks chain verification. Limitation documented: whole-file rewrite can rebuild the chain. | D7 | `tests/test_audit_log.py` | **PASS** (7/7) |
| Append-only pending queue | Reviewer resolutions are appended as events; the queue is never rewritten in place. Chain is verified on every read — a tampered queue fails loudly rather than serving doctored cases. | D8 | `tests/test_audit_log.py` | **PASS** (7/7) |

*A rule without a passing test is not a rule — it is a comment. Status is computed by running each test file at generation time.*

---

## 3. Golden-Set Evaluation Scoreboard

**Eval run:** `evals/results/run_20260802_172528.json`  
**Run timestamp:** 2026-08-02T17:25:28.270472+00:00  
**Eval git SHA:** `5f71d70`  
**Cases:** 15 ran of 15 total  
**Ground truth:** human-authored from policy documents — LLM never used to generate expected outcomes.

| Dimension | What it measures | Scoreable | Result |
|-----------|-----------------|-----------|--------|
| S1 Determination accuracy | Pipeline decision matches expected (approve / insufficient_evidence / reject) | 15 | 11/15 |
| S2 Routing accuracy | auto vs HITL routing matches expected for determination-stage cases | 13 | 9/13 |
| S3 Criterion-level evidence accuracy | Each criterion matched to correct pipeline evidence with correct status and citations | 12 | 11/12 |
| S4 Citation validity | Surviving citations in `met` evidence resolve against the submitted bundle | 13 | **13/13** |

**S2 HITL routing — precision and recall:**

| Metric | Value |
|--------|-------|
| Cases expected HITL | 8 |
| True positives (correctly sent to HITL) | 7 |
| False positives (sent to HITL unnecessarily) | 3 |
| False negatives (missed — sent to auto) | 1 |
| Precision | 70% |
| Recall | 88% |

**Key behavioral cases:**

| Case | Behavior | S1 | S2 | S3 | S4 |
|------|----------|----|----|----|----|
| `case_007` | Strip-and-log: ghost citation absent from bundle stripped by existence check; criterion stays `met` on surviving citations | **FAIL** | **FAIL** | PASS | PASS |
| `case_009` | Eligibility gate: `Coverage.status=cancelled` halts pipeline before any LLM call | PASS | skip | skip | skip |
| `case_011` | All criteria insufficient: bundle has only Patient+Coverage, zero clinical resources | PASS | PASS | PASS | PASS |
| `case_014` | `not_met`: DiagnosticReport explicitly denies psychiatric clearance for surgery | PASS | PASS | PASS | PASS |
| `case_015` | `not_met`: Observation documents patient refused PT — C2 not satisfied | PASS | PASS | PASS | PASS |

---

## 4. Citation-Gate Coverage

| Metric | Count |
|--------|-------|
| Total criteria evaluated across determination-stage cases | 37 |
| Met criteria with bilateral citations (passed gate) | 29 |
| Citations stripped by existence check | 0 |
| Criteria downgraded to `insufficient` (no citations survived) | 0 |
| Cases hard-rejected at citation gate (never reached determination) | 0 |

*Derived from `scores.criterion_evidence.details[].strip_log_fired` and `scores.citation_validity.stripped_count` in the eval results JSON. `case_007` is the designed strip-and-log case; any other stripped citations represent unintended pipeline behavior.*

---

## 5. Traceability: Policy Criteria → Tests

**Policy document:** `data/policies/rotator_cuff_repair_29827.md`  
**CPT code:** 29827

| ID | Criterion | Required | Met in | Insufficient in | Not-met in | Covering test patterns | Status |
|----|-----------|----------|--------|----------------|------------|------------------------|--------|
| C1 | MRI-confirmed full-thickness rotator cuff tear | Yes | `case_007` | — | — | `test_met_expected_citations_resolve_in_bundle[case_007]` | **PASS** (288/288) |
| C2 | Failure of ≥3 months conservative management including PT | Yes | `case_007` | — | — | `test_met_expected_citations_resolve_in_bundle[case_007]`<br>`test_case_007_ghost_citation_absent_from_bundle` | **PASS** (288/288) |
| C3 | Documented functional impairment affecting ADLs | Yes | `case_007` | — | — | `test_met_expected_citations_resolve_in_bundle[case_007]` | **PASS** (288/288) |

*The criterion→test mapping in this table is declared in the generator config (~10 lines). It is the only section requiring human update when criteria or test names change. CPT 29827 currently has exactly one golden case (`case_007`), which exercises `met` for all three criteria — there is no `insufficient` or `not_met` example for this policy in the golden set yet, hence the empty columns above.*

---

## 6. Cost per Determination

> **[LIVE RUN REQUIRED]** No cost report found.  
> Run: `python -m evals.run --live && python -m evals.cost_report`

---

## 7. Limitations

### From the evaluation methodology (EVALS.md)

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

What this suite is good for: **catching regressions**. If a system prompt
change causes a previously-passing case to fail, the harness will detect it
in CI. That is its intended purpose. It is not a substitute for clinical
validation, red-teaming, or production monitoring.

### Operational limitations (from README)

- **LLM node coverage comes from a 15case golden eval suite, not exhaustive unit tests.** Cassettebased replay (`evals/run.py`) lets criteria mapping, evidence extraction, and determination run in CI without an API key — see [EVALS.md](EVALS.md) for what that suite does and doesn't validate.
- **Eligibility is a stub.** It reads `Coverage.status` from the bundle and nothing more; it does not perform a real 270/271 eligibility transaction or call a payer coverage API.
- **The audit log is tamperevident, not tamperproof.** The hash chain detects edits, deletions, and reordering, but an attacker who can rewrite the whole file can rebuild the chain. Anchoring the head hash externally is not yet implemented.
- **RAG retrieval can select the wrong policy document.** The query's only reliably distinguishing feature between similar policies is the numeric CPT code; the local embedding model (allMiniLML6v2) doesn't always weight that distinctly enough against nearidentical boilerplate shared across policy documents. Confirmed via a live eval run: 3 of 5 test CPTs retrieved a different policy's chunks as the top match. Not yet fixed.
- **Empty criteria can vacuously autoapprove.** When retrieval returns zero applicable policy chunks (e.g., no policy exists for the requested CPT), the determination step can reason "no required criteria to evaluate" as trivially satisfied and approve, rather than treating the absence of any policy knowledge as insufficient. Confirmed via `case_008`'s live eval run. Not yet fixed. **Fixed 2026-08-02:** the policy chunker in `ingest.py` previously crawled forward one character at a time whenever a paragraph break fell within the overlap window — up to ~200 near-duplicate chunks from a ~1KB document. Not just wasteful: once more than one policy existed in the same ChromaDB collection, the resulting dense cluster could dominate retrieval for every query, regardless of the actual CPT. See `tests/test_ingest.py`.

---

## 8. Generation Provenance

| Field | Value |
|-------|-------|
| Generated at | 2026-08-08T11:39:00+00:00 |
| Git SHA | `7f577f7` |
| Generator | `python -m prior_auth_agent.audit_packet` |
| Eval results | `evals/results/run_20260802_172528.json` |
| Cost report | not available |
| Test run at generation | **PASS** (288/288) |

*Regenerate after any model change, prompt change, or new eval run. Do not use a packet whose SHA does not match the deployed system.*
