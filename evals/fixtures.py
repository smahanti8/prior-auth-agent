"""Cassette-based replay for golden eval cases.

Cassettes live at evals/recorded/<case_id>.json. Each cassette stores the
RAG results and three LLM responses recorded from a prior live run. The runner
patches the appropriate module-level bindings before calling graph.invoke(),
so CI replays identically without an API key or a ChromaDB instance.

Staleness is detected by comparing the SHA-256 of each node's SYSTEM string
(stored at record time) against the current source. If a system prompt changes
without re-recording, check_staleness() raises StalenessError immediately —
the cassette is a silent lie and the run must not proceed.

Token counts (input_tokens, output_tokens, model) are stored in each node
entry so that replay_structured_call() can call telemetry.record_llm() with
the original counts. This allows cost_report.py to produce accurate cost data
from cassette replay without burning API budget.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import MagicMock

RECORDED_DIR = Path(__file__).resolve().parent / "recorded"


class StalenessError(RuntimeError):
    """Raised when a cassette's stored prompt hash no longer matches source."""


def _fingerprint(schema: dict) -> str:
    """Short hash of a JSON schema dict — routes structured_call to the right cassette entry."""
    payload = json.dumps(schema, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _hash_string(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _current_system_hashes() -> dict[str, str]:
    """Compute system-prompt hashes from current source. Imported lazily to
    avoid pulling in the full prior_auth_agent stack at import time."""
    from prior_auth_agent.nodes.criteria_mapper import SYSTEM as CM_SYS
    from prior_auth_agent.nodes.evidence_extractor import SYSTEM as EE_SYS
    from prior_auth_agent.nodes.determination import SYSTEM as DET_SYS
    return {
        "criteria_mapper":    _hash_string(CM_SYS),
        "evidence_extractor": _hash_string(EE_SYS),
        "determination":      _hash_string(DET_SYS),
    }


class Cassette:
    """Replay recorded RAG and LLM responses for one golden eval case."""

    def __init__(self, case_id: str, data: dict) -> None:
        self.case_id = case_id
        self._data = data
        # Build dispatch map: schema_fingerprint -> node entry
        self._by_fp: dict[str, dict] = {}
        for node_name, entry in data.get("nodes", {}).items():
            if node_name == "policy_rag":
                continue
            fp = entry.get("schema_fingerprint")
            if fp:
                self._by_fp[fp] = entry

    @classmethod
    def load(cls, case_id: str) -> "Cassette":
        path = RECORDED_DIR / f"{case_id}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"No cassette for {case_id}. "
                f"Record it with: python -m evals.run --live {case_id}"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(case_id, data)

    @classmethod
    def exists(cls, case_id: str) -> bool:
        return (RECORDED_DIR / f"{case_id}.json").exists()

    def check_staleness(self) -> None:
        """Raise StalenessError if any node's system prompt changed since recording."""
        current = _current_system_hashes()
        stale = []
        for node_name, current_hash in current.items():
            entry = self._data.get("nodes", {}).get(node_name, {})
            stored = entry.get("system_hash")
            if stored and stored != current_hash:
                stale.append(node_name)
        if stale:
            raise StalenessError(
                f"Cassette {self.case_id} is STALE — system prompt changed in: "
                f"{', '.join(stale)}.\n"
                f"Re-record: python -m evals.run --live {self.case_id}"
            )

    def replay_structured_call(
        self,
        system: str,
        user_content: str,
        schema: dict[str, Any],
        max_tokens: int = 16000,
    ) -> dict[str, Any]:
        fp = _fingerprint(schema)
        entry = self._by_fp.get(fp)
        if entry is None:
            fps = list(self._by_fp.keys())
            raise RuntimeError(
                f"Cassette {self.case_id}: no recorded response for schema "
                f"fingerprint {fp!r}. Known fingerprints: {fps}. "
                f"Re-record: python -m evals.run --live {self.case_id}"
            )
        # Per-call staleness check
        current_hash = _hash_string(system)
        stored_hash = entry.get("system_hash")
        if stored_hash and current_hash != stored_hash:
            raise StalenessError(
                f"Cassette {self.case_id}/{entry['node_name']}: system prompt changed "
                f"since recording.\n"
                f"Re-record: python -m evals.run --live {self.case_id}"
            )
        # Record telemetry from stored token counts so cost reports work in replay mode
        from prior_auth_agent import telemetry as _tel
        import time
        t0 = time.perf_counter()
        result = dict(entry["response"])
        replay_ms = (time.perf_counter() - t0) * 1000  # near-zero: just dict copy
        if entry.get("input_tokens") is not None:
            _tel.record_llm(
                input_tokens=entry["input_tokens"],
                output_tokens=entry["output_tokens"],
                model=entry.get("model", self.model),
                latency_ms=replay_ms,
            )
        return result

    def make_collection_fn(self) -> Callable:
        """Return a function that replaces get_collection() for replay."""
        chunks = self._data.get("nodes", {}).get("policy_rag", {}).get("chunks", [])
        mock_col = MagicMock()
        mock_col.query.return_value = {
            "documents": [[c["text"] for c in chunks]],
            "metadatas": [[{"source": c.get("source", "unknown")} for c in chunks]],
            "distances": [[c.get("distance", 0.0) for c in chunks]],
        }
        def get_collection():
            return mock_col
        return get_collection

    @property
    def recorded_at(self) -> str:
        return self._data.get("recorded_at", "unknown")

    @property
    def model(self) -> str:
        return self._data.get("model", "unknown")
