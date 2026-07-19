# Healthcare Prior Auth Agent

An agentic prior-authorization pipeline: it takes a FHIR bundle and a CPT code, validates and checks eligibility, retrieves the applicable payer policy via RAG (ChromaDB), maps policy criteria, extracts per-criterion clinical evidence with citations, drafts a determination, and routes low-confidence cases to a human review queue (Streamlit).

## Architecture

```
FHIR Bundle ─┐
             ├─> Intake/Validation ─> Eligibility ─> Policy RAG (ChromaDB)
CPT Code  ───┘                                          │
                                        Criteria Mapper <┘
                                              │
                                   Evidence Extractor (per-criterion citations)
                                              │
                                     Determination Drafter
                                              │
                                      Confidence Gate ──low──> HITL Review Queue (Streamlit)
                                              │high                    │
                                        Auto Decision <───────────────┘
```

## Layout

```
src/prior_auth_agent/
├── config.py              # env, model id, confidence threshold
├── state.py               # LangGraph state schema
├── graph.py               # graph wiring + entrypoint
├── llm.py                 # shared Anthropic client + structured-output helper
├── nodes/
│   ├── intake.py          # FHIR bundle + CPT validation
│   ├── eligibility.py     # coverage / eligibility check (stub for payer API)
│   ├── policy_rag.py      # ChromaDB retrieval of applicable policy chunks
│   ├── criteria_mapper.py # policy text -> discrete, checkable criteria
│   ├── evidence_extractor.py  # per-criterion evidence w/ FHIR citations
│   ├── determination.py   # draft approve/deny/pend + rationale
│   └── confidence_gate.py # routes to auto decision vs HITL queue
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

# 5. Review low-confidence cases
streamlit run src/prior_auth_agent/hitl/review_app.py
```

## Notes

- **Model**: `claude-opus-4-8` with adaptive thinking; structured outputs (`output_config.format`) guarantee schema-valid JSON at every LLM node.
- **Eligibility** is a stub — wire it to your payer's 270/271 or coverage API.
- **Confidence gate**: cases below `CONFIDENCE_THRESHOLD` (default 0.85), or with any criterion lacking evidence, go to `data/review_queue/pending.jsonl` for human review.
- **PHI**: this scaffold does no de-identification; do not point it at real patient data without your compliance controls in place.
