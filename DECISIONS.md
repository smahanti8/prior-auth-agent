# Decision Log

The decisions in this pipeline worth arguing about, one entry each: the
context that forced a choice, the choice, why, and the strongest argument
against it. If a counter-argument ever wins, the entry gets superseded here —
not silently rewritten.

---

## D1. Denials always route to human review, regardless of confidence

> **Superseded by [D9](#d9-the-ai-never-frames-a-denial-supersedes-d1).**
> Routing a drafted denial to a human was correct but insufficient: the draft
> still *contained* a denial recommendation, which anchors the reviewer. D9
> removes the denial recommendation from the drafter entirely.

**Context.** The confidence gate decides which cases the machine finalizes.
A threshold alone would let a sufficiently confident denial be auto-finalized
— and the model draft for a denial can be very confident when a required
criterion is clearly not met.

**Decision.** The gate routes to human review whenever the draft decision is
`deny` or `pend`, independent of the confidence score. The machine may
finalize approvals; it may never finalize a denial. The determination prompt
states this openly, so the model has no incentive to inflate confidence to
avoid review.

**Rationale.** Error costs are not symmetric: a wrong auto-approval grants
care; a wrong auto-denial withholds it. It also forecloses the worst
failure mode of confidence-gated autonomy — a model that becomes confidently
wrong in exactly the direction that escapes oversight. This asymmetry mirrors
where regulation of AI-assisted utilization review is heading.

**Counter-argument.** Throughput. Many denials are clear-cut, and a human on
every one is expensive at scale — real payers auto-deny today. But that
practice is precisely what is under regulatory and public fire, which is the
point of the entry.

---

## D2. Every clinical claim carries a FHIR citation, or the criterion is `insufficient`

> **Extended by [D10](#d10-a-met-claim-requires-both-a-policy-quote-and-a-chart-citation-extends-d2).**
> D10 makes a policy quote and a chart citation a hard requirement on met
> claims; a deterministic existence check in the evidence extractor now
> resolves each citation against the submitted bundle and closes the gap
> described below.

**Context.** The evidence extractor searches the patient's FHIR bundle for
support of each policy criterion. The unforgivable failure mode in this
domain is an invented clinical fact.

**Decision.** Every evidence claim must cite FHIR references
(`ResourceType/id`) to resources actually present in the bundle. If the chart
is silent on a criterion, the status is `insufficient` with empty citations —
never an inference. `insufficient` is a first-class outcome: on any
*required* criterion it forces human review through the gate, regardless of
confidence.

**Rationale.** "The documentation doesn't say" is a legitimate clinical
answer, and it routes to the right place — a person. Prompt rules alone are
policy, not enforcement, so the gate independently backstops them: an honest
insufficiency can never be auto-finalized into a decision.

**Counter-argument.** A prompt cannot guarantee a cited reference is real.
D10 enforces that a met claim *carries* both a policy quote and a chart
citation; a deterministic post-check in the evidence extractor now resolves
each citation per-citation against the bundle, strips any that fail to
resolve, and downgrades the criterion to `insufficient` when none survive.
Two subtleties remain honest: first, resolution proves existence, not clinical
relevance — a citation can resolve to a real resource in the bundle that does
not actually support the criterion (the human gate backstops this); second, a
criterion can now pass `met` with fewer citations than the model originally
offered, on the surviving ones only, so a partially-fabricated claim may still
reach determination if at least one citation holds. Strictness also pends
cases where a clinician would trivially infer the fact.

---

## D3. The autonomy decision is made by deterministic code, not the model

**Context.** Something must decide whether the machine or a human finalizes a
case. The obvious agentic move is to let the model decide — or to gate purely
on its self-reported confidence, which is circular.

**Decision.** The gate is pure Python: route to human if confidence is below
the threshold (default 0.85, env-tunable), OR the decision is deny/pend, OR
any required criterion came back insufficient. No LLM call.

**Rationale.** The riskiest routing decision in the system should be made by
its most auditable component. The whole routing policy is three boolean
conditions anyone — an auditor, a clinician, a regulator — can read and use
to predict exactly which cases a human sees. Confidence is only one of three
OR'd conditions; the other two are structural and do not trust the model's
self-report at all.

**Counter-argument.** A fixed 0.85 is uncalibrated — plausibly too strict or
too lax. A calibrated gate tuned from logged human-override rates (the data
already accumulates in `decisions.jsonl`) could outperform it. That is a
tuning path, not a reason to put an LLM in the gate.

---

## D4. Everything deterministic runs before anything probabilistic

**Context.** Bundles arrive malformed, CPT codes arrive mistyped, coverage
lapses. Each LLM node costs money and introduces paraphrase.

**Decision.** Intake (bundle structure, Patient presence, CPT format) and
eligibility (Coverage status) run first and short-circuit to `reject` before
any retrieval or LLM call. Rejection reasons are the exact validation
messages.

**Rationale.** A malformed bundle should never burn tokens, and a rejection
should carry the precise failure — a regex message, not a model's paraphrase
of one. Cost ordering and safety ordering happen to agree.

**Counter-argument.** The validation is shallow — structural checks, not FHIR
profile validation, and eligibility is a stub for a real 270/271
transaction. A production front door needs a real validator; the stub
boundary is documented rather than papered over.

---

## D5. Policy criteria come from retrieval, not model memory

**Context.** Payer policies differ by plan and change on their own schedule.
A model's parametric memory of "typical criteria for CPT 29881" is exactly
what must not decide a real case.

**Decision.** Policy documents are chunked into a local vector store and the
top matches — with source filenames and distances attached — are the only
policy text the criteria mapper sees. Every criterion traces to retrieved
text from a named file.

**Rationale.** Grounding is a discipline, not a search feature: the pipeline
reasons over the actual policy in force, and provenance rides along so a
reviewer can trace a criterion back to its source document.

**Counter-argument.** Retrieval has no hard CPT↔policy filter today; the
wrong policy's chunks could surface. Metadata filtering at ingest and a
retrieval eval set are the fix; until then the human gate is the backstop,
since criteria from an irrelevant policy are recognizably wrong on review.

---

## D6. Every LLM boundary is schema-constrained; refusals raise

**Context.** Three nodes call an LLM and their outputs feed typed state.
Free-text responses mean parse-and-pray: JSON wrapped in markdown, missing
fields, invented enum values.

**Decision.** All three nodes use a shared helper that constrains the
response to a JSON schema at the API level, so node code receives
schema-valid JSON or an exception. A refusal stop-reason raises rather than
returning something half-usable.

**Rationale.** This deletes the entire syntax-failure class, so error
handling concentrates on the failures that matter — refusals and wrong
content, not wrong shape. The state schema, the JSON schema, and the API
constraint are three views of the same typed contract.

**Counter-argument.** Schema validity can breed false confidence: a
conforming evidence row can still cite the wrong resource or misread the
chart. Schema validity is table stakes, not a safety argument — the safety
argument is D1–D3.

---

## D7. Decision logs are hash-chained JSONL — tamper-evident, not tamper-proof

**Context.** Decisions and the review queue live in JSONL files that anyone
with file access can edit. A decision log that can be silently edited is not
an audit trail, and these records back clinical determinations.

**Decision.** Every entry carries `prev_hash` and
`entry_hash = sha256(prev_hash + canonical content)` (sorted keys, hash
fields excluded). Editing, deleting, or reordering any line breaks
verification of everything after it; `verify_chain()` raises with file,
line, and reason. The limitation is stated in the module docstring: an
attacker who rewrites the whole file can rebuild the chain — anchoring the
head hash externally is the documented next step, not a hand-wave.

**Rationale.** The chain is the cheapest primitive that changes the
attacker's job from "edit one line" to "rebuild the entire file", and its
guarantee boundary can be stated precisely. Claiming more than the mechanism
delivers would be worse than the gap.

**Counter-argument.** A database with audit features is the conventional
answer — but a DBA can edit rows too, so the chain would still be wanted on
top. The real weaknesses are operational: no file locking (concurrent
writers could fork the chain) and O(n) verification per read. Both are
acceptable at this scale and named rather than hidden.

---

## D8. The pending queue is never rewritten; resolution is an appended event

**Context.** The first version of reviewer resolution rewrote
`pending.jsonl` in place — filter out the resolved case, write the file
back. That is the natural instinct, and it is exactly wrong once the queue
is hash-chained: a legitimate rewrite is indistinguishable from tampering.

**Decision.** The queue is an append-only event log. Resolution appends a
chained `resolved` event; `load_pending()` folds the log (latest event per
case wins) and runs `verify_chain()` on every load, so a tampered queue
fails loudly in the review UI instead of quietly serving doctored cases.

**Rationale.** State lives in the fold, the file only ever grows, and
integrity verification becomes a free side effect of reading — event
sourcing in miniature. Verification on every load only stays false-alarm-free
because rewrites are structurally impossible, which is why the two halves of
this decision need each other.

**Counter-argument.** Unbounded growth and O(n) reads; and a raised
exception is a blunt reviewer experience — an explicit integrity-failure
screen with file, line, and reason would keep the loudness with better
operator ergonomics. Compaction would require re-anchoring the chain and is
deliberately deferred until external anchoring (D7) exists.

---

## D9. The AI never frames a denial (supersedes D1)

**Context.** Under D1 the drafter could recommend `deny`, and the gate routed
every denial to a human. The routing was correct, but it treated the problem
as *who finalizes* the denial while ignoring *who frames* it. A reviewer who
opens a case already labelled "DENY — required criterion c2 not met" is
anchored: the cognitively cheap action is to ratify the model's denial, and
human-in-the-loop review quietly degrades into rubber-stamping even though,
on paper, a human made the call.

**Decision.** The drafter can no longer produce a denial at all. It emits
either `approve`, or `insufficient_evidence` — which names exactly which
required criteria lack support and the specific documentation that would
satisfy each. No code path yields a denial recommendation: the response
schema's `decision` enum excludes it, the `Determination` type excludes it,
and structured output constrains the model to that enum. A human forms and
owns any denial. `tests/test_no_denial.py` asserts the invariant at all three
layers and exhaustively drives every schema-permitted decision through the
gate and finalizers to confirm none yields a denial.

**Rationale.** A drafted denial anchors the reviewer toward denying, which
hollows out human-in-the-loop review even when the routing is technically
correct — the exact failure D1 left open. Reframing every non-approval as
"here is the evidence still needed" keeps the reviewer's judgment central and
hands the requester an actionable path to approval instead of a verdict to
appeal.

**Counter-argument.** Reviewers lose a useful signal: the model's denial
rationale was a concise account of *why* a case is weak, and reconstructing
that from a list of gaps takes work — throughput may drop, and a reviewer
under load might approve a genuinely deficient case because the draft no
longer argues against it. The bet is that removing the anchor does more for
decision quality than the lost signal does, and that the named gaps carry
most of the same information without the framing.

---

## D10. A met claim requires both a policy quote and a chart citation (extends D2)

> **Extends [D2](#d2-every-clinical-claim-carries-a-fhir-citation-or-the-criterion-is-insufficient).**
> D2 established that claims must be citation-backed or fall to `insufficient`
> and route to a human. D10 hardens that into a hard gate for one specific
> failure: a claim asserted `met` without its citations. The existence gap
> named in this entry's counter-argument is now closed — see D2.

**Context.** D2 routes honest gaps (`insufficient`) to a human. It does not
stop the opposite failure: the model marking a criterion `met` while omitting
the evidence for it. Structured output guarantees the *shape* of an evidence
row, not that a `met` row actually carries support. An uncited `met` is not
uncertainty a reviewer should weigh — it is an unsupported assertion.

**Decision.** A criterion with `status == "met"` must carry BOTH a non-empty
`policy_quote` (the requirement) and at least one chart-side FHIR citation
(the evidence). A pydantic validator (`validation.py: EvidenceClaim`) *raises*
on violation; the `citation_gate` node runs it after evidence extraction and
routes any violation to a hard `citation_reject` terminal — the case never
reaches the determination. This is a hard gate, not D2's soft routing: an
uncited `met` is thrown out, not sent to a human. `tests/test_citation_gate.py`
pins both sides — a met claim with both quotes passes; missing either raises;
non-`met` statuses need no citation.

**Rationale.** Routing an unsupported claim to a human relaxes the rule back
into a suggestion and trains reviewers to launder the model's gaps ("the AI
said met, I'll approve it"). The two failure classes deserve different
handling: uncertainty (D2's `insufficient`) is a judgment call for a person;
an unsupported assertion is invalid and is rejected. Keeping enforcement in a
validator that raises makes it deterministic and testable without an LLM call.

**Counter-argument.** The gate checks that citations are *present*, not that
they *resolve*. That existence gap is now closed: a deterministic post-check
in the evidence extractor resolves each citation per-citation against the
bundle, strips any that fail, and downgrades to `insufficient` when none
survive (see [D2](#d2-every-clinical-claim-carries-a-fhir-citation-or-the-criterion-is-insufficient)).
What remains: rejecting outright (rather than pending) discards a case a
human might have salvaged by supplying the missing quote.

---

## D11. The evidence-tier pattern stays local; not extracted into a shared package

**Context.** The three-tier evidence pattern — hard-reject on no evidence,
soft-downgrade-with-flag on partial, clean-pass on full — is genuinely
present in both this repo and `surgical-fhir-pipeline`. A shared
`evidence-gate` package was scoped to extract it.

**Decision.** Not extracted. The primitive stays local to this repo.

**Rationale.** Only this repo computes tier assignment — `validation.py`
resolves citations at request time, strips non-resolving ones, and
downgrades `met` to `insufficient`. `surgical-fhir-pipeline` asserts status
as static data in a hardcoded table, with a test guarding against silent
promotion; its promotion path is explicitly deferred roadmap work there.
Extracting now would either ship dead code to `surgical-fhir-pipeline` or
smuggle new feature work in as a refactor, violating behavior preservation.
Applying the "both consumers must genuinely use it" rule strictly leaves
~10 lines — a tier enum and a boolean reduction — not enough to justify a
separately versioned package with coordinated CI across two repos that
share no runtime interop.

**Counter-argument.** The pattern is real, and a future Snowstorm
terminology-server validation loop would make `surgical-fhir-pipeline` a
genuine second consumer with executable promotion. Revisit then. There is
also a modest cost to deferring: the shape is currently documented in prose
rather than enforced by a shared type.
