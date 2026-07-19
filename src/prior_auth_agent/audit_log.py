"""Tamper-evident append-only JSONL logs (hash chain).

Every entry carries two extra fields:

  prev_hash   entry_hash of the previous line (GENESIS for the first entry)
  entry_hash  sha256(prev_hash + canonical JSON of the entry content)

"Content" is the entry minus the two hash fields, serialized canonically
(sorted keys, no whitespace), so verification is independent of dict order.
Editing, deleting, or reordering any line invalidates every hash after it.

This only makes the log tamper-EVIDENT, not tamper-proof: an attacker who can
rewrite the whole file can rebuild the chain. Preventing that requires
anchoring the head hash somewhere they can't write (a signed timestamp, a
separate service). Detection of casual edits is still worth having in an
audit trail that backs clinical determinations.
"""

import hashlib
import json
from pathlib import Path

GENESIS = "0" * 64
_HASH_FIELDS = ("prev_hash", "entry_hash")


class ChainBrokenError(Exception):
    """The hash chain does not verify."""

    def __init__(self, path: Path, line_no: int, reason: str):
        self.path = path
        self.line_no = line_no
        super().__init__(f"{path.name}:{line_no}: {reason}")


def _canonical(record: dict) -> str:
    content = {k: v for k, v in record.items() if k not in _HASH_FIELDS}
    return json.dumps(content, sort_keys=True, separators=(",", ":"))


def _entry_hash(prev_hash: str, record: dict) -> str:
    return hashlib.sha256((prev_hash + _canonical(record)).encode()).hexdigest()


def _last_hash(path: Path) -> str:
    if not path.exists():
        return GENESIS
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    if not lines:
        return GENESIS
    return json.loads(lines[-1])["entry_hash"]


def append_chained(path: Path, record: dict) -> dict:
    """Append `record` to the log, chained to the previous entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    prev_hash = _last_hash(path)
    entry = record | {
        "prev_hash": prev_hash,
        "entry_hash": _entry_hash(prev_hash, record),
    }
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def verify_chain(path: Path) -> list[dict]:
    """Verify the whole chain; return the entries or raise ChainBrokenError."""
    if not path.exists():
        return []
    entries: list[dict] = []
    prev_hash = GENESIS
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("prev_hash") != prev_hash:
            raise ChainBrokenError(
                path, line_no,
                f"prev_hash {entry.get('prev_hash')!r} does not match "
                f"predecessor's entry_hash {prev_hash!r}",
            )
        expected = _entry_hash(prev_hash, entry)
        if entry.get("entry_hash") != expected:
            raise ChainBrokenError(
                path, line_no,
                "entry content does not match its entry_hash (tampered?)",
            )
        prev_hash = entry["entry_hash"]
        entries.append(entry)
    return entries


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        p = Path(arg)
        n = len(verify_chain(p))
        print(f"{p}: OK ({n} entries)")
