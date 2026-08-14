import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]

MODEL = "claude-opus-4-8"
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.85"))

# ── LLM backend ────────────────────────────────────────────────────────────────
# LLM_BACKEND=anthropic  — direct Anthropic API (default, not BAA-eligible for PHI)
# LLM_BACKEND=bedrock    — AWS Bedrock (run inside a BAA-covered AWS account)
#
# The Bedrock path uses the task IAM role for credentials (no ANTHROPIC_API_KEY).
# See README "What changes inside a PHI boundary" for the compliance rationale.
LLM_BACKEND: str = os.getenv("LLM_BACKEND", "anthropic")
BEDROCK_REGION: str = os.getenv("BEDROCK_REGION", "us-east-1")
BEDROCK_MODEL_ID: str = os.getenv(
    "BEDROCK_MODEL_ID",
    "us.anthropic.claude-opus-4-8-20251101-v1:0",
)

CHROMA_DIR = Path(os.getenv("CHROMA_DIR", PROJECT_ROOT / ".chroma"))
POLICY_COLLECTION = "payer_policies"
POLICY_DIR = PROJECT_ROOT / "data" / "policies"

REVIEW_QUEUE_DIR = PROJECT_ROOT / "data" / "review_queue"
PENDING_QUEUE = REVIEW_QUEUE_DIR / "pending.jsonl"
DECISIONS_LOG = REVIEW_QUEUE_DIR / "decisions.jsonl"
AUDIT_LOG_PATH = REVIEW_QUEUE_DIR / "determinations.jsonl"
