# Healthcare Prior Auth Agent

An agentic prior-authorization pipeline: it takes a FHIR bundle and a CPT code, validates and checks eligibility, retrieves the applicable payer policy via RAG (ChromaDB), maps policy criteria, and extracts per-criterion clinical evidence with citations. It drafts either an approval or an `insufficient_evidence` result — it never drafts a denial, because a drafted denial anchors the reviewer; a human makes any adverse decision. A hard citation gate rejects any criterion marked *met* that lacks both a policy-side quote and a chart-side FHIR citation ("no citation → no claim"), and non-approved or low-confidence cases route to a human review queue (Streamlit). Every decision is written to a tamper-evident, hash-chained audit log.

## Architecture

```
FHIR Bundle ─┐
             ├─> Intake ─> Eligibility ─> Policy RAG (ChromaDB) ─> Criteria Mapper
CPT Code  ───┘      │          │                                        │
                 reject      reject                                      v
                                            Evidence Extractor (policy quote + chart citations)
                                                              │
                              Citation Gate ──"met" claim missing a quote──> reject (hard)
                                                              │ ok
                              Determination (approve | insufficient_evidence — never denies)
                                                              │
                              Confidence Gate ──not-approve / low-confidence──> HITL Queue (Streamlit)
                                                              │ auto (approvals only)
                                                       Auto Decision
                                                              │
                                         tamper-evident, hash-chained audit log
```

## Layout

```
src/prior_auth_agent/
├── config.py              # env, model id, confidence threshold
├── state.py               # LangGraph state schema
├── graph.py               # graph wiring + entrypoint
├── llm.py                 # shared Anthropic client + structured-output helper (streamed)
├── validation.py          # pydantic citation rule: no citation -> no claim
├── nodes/
│   ├── intake.py          # FHIR bundle + CPT validation
│   ├── eligibility.py     # coverage / eligibility check (stub for payer API)
│   ├── policy_rag.py      # ChromaDB retrieval of applicable policy chunks
│   ├── criteria_mapper.py # policy text -> discrete, checkable criteria
│   ├── evidence_extractor.py  # per-criterion evidence w/ policy quote + FHIR citations
│   ├── citation_gate.py   # hard reject: any met claim missing a policy or chart quote
│   ├── determination.py   # draft approve | insufficient_evidence + rationale (never denies)
│   └── confidence_gate.py # routes approvals to auto decision, everything else to HITL
├── vectorstore/
│   ├── store.py           # ChromaDB persistent client + collection
│   └── ingest.py          # chunk & index policy documents
└── hitl/
    └── review_app.py      # Streamlit review queue UI
```

## Quickstart

```bash
# 1. Install (Python 3.11+)
pip install -e .

# 2. Credentials
cp .env.example .env       # set ANTHROPIC_API_KEY (or use `ant auth login`)

# 3. Index payer policies (drop .md/.txt policy docs into data/policies/)
python -m prior_auth_agent.vectorstore.ingest

# 4. Run the agent on the sample case
python -m prior_auth_agent.graph data/samples/sample_bundle.json 29881

# 5. Review non-approved cases
streamlit run src/prior_auth_agent/hitl/review_app.py
```

The deterministic parts (intake, eligibility, the citation gate, the audit log) are covered by the test suite and need **no** API key: `PYTHONPATH=src python -m pytest tests/ -q`.

## Notes

- **Model**: `claude-opus-4-8` with adaptive thinking; structured outputs (`output_config.format`) guarantee schema-valid JSON at every LLM node. Calls are streamed, as the SDK requires above its non-streaming time ceiling.
- **No AI-drafted denials**: the determination schema can only emit `approve` or `insufficient_evidence`; a denial is made by a human in the review queue, never framed by the model.
- **Citation gate**: a criterion marked *met* must carry both a policy-side quote and a chart-side FHIR citation, or it is rejected outright — a hard gate, separate from the confidence gate's soft routing.
- **Confidence gate**: non-approvals, cases below `CONFIDENCE_THRESHOLD` (default 0.85), or any required criterion lacking evidence go to `data/review_queue/pending.jsonl` for human review.
- **PHI**: this scaffold does no de-identification; do not point it at real patient data without your compliance controls in place.

## Known Limitations

1. **The LLM nodes have no automated test coverage.** The test suite stops at the API boundary by design (it runs without a key), so criteria mapping, evidence extraction, and determination are exercised only by manual runs — there is no recorded-response or eval harness yet.
2. **Eligibility is a stub.** It reads `Coverage.status` from the bundle and nothing more; it does not perform a real 270/271 eligibility transaction or call a payer coverage API.
3. **The audit log is tamper-evident, not tamper-proof.** The hash chain detects edits, deletions, and reordering, but an attacker who can rewrite the whole file can rebuild the chain. Anchoring the head hash externally is not yet implemented.

**Known issue (tracked):** the policy chunker in `ingest.py` over-produces chunks on small documents (a ~1 KB policy indexes to ~200 chunks); a fix is pending.
