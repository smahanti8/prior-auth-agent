import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]

MODEL = "claude-opus-4-8"
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.85"))

CHROMA_DIR = Path(os.getenv("CHROMA_DIR", PROJECT_ROOT / ".chroma"))
POLICY_COLLECTION = "payer_policies"
POLICY_DIR = PROJECT_ROOT / "data" / "policies"

REVIEW_QUEUE_DIR = PROJECT_ROOT / "data" / "review_queue"
PENDING_QUEUE = REVIEW_QUEUE_DIR / "pending.jsonl"
DECISIONS_LOG = REVIEW_QUEUE_DIR / "decisions.jsonl"
