# Decision log

The decisions in this pipeline worth arguing about, one entry each: the
context that forced a choice, the choice, why, and the strongest argument
against it. If a counter-argument ever wins, the entry gets superseded here —
not silently rewritten.

---

## D1. Denials always route to human review, regardless of confidence

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
The missing enforcement layer is a deterministic post-check that every cited
`ResourceType/id` exists in the bundle, downgrading to `insufficient` when it
does not — acknowledged as the roadmap's clearest gap. Strictness also pends
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
