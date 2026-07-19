"""HITL Review Queue — Streamlit UI.

Run: streamlit run src/prior_auth_agent/hitl/review_app.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # src/ on path

from prior_auth_agent.audit_log import append_chained, verify_chain  # noqa: E402
from prior_auth_agent.config import DECISIONS_LOG, PENDING_QUEUE  # noqa: E402

STATUS_ICONS = {"met": "✅", "not_met": "❌", "insufficient": "⚠️"}


def load_pending() -> list[dict]:
    """Fold the append-only queue: a case is pending unless a later
    'resolved' event exists for its case_id. The queue is never rewritten —
    rewriting would invalidate the hash chain, which is the point of it."""
    events = verify_chain(PENDING_QUEUE)
    latest: dict[str, dict] = {}
    for e in events:
        latest[e["case_id"]] = e
    return [c for c in latest.values() if c.get("status") == "pending_review"]


def save_decision(case: dict, decision: str, reviewer_note: str) -> None:
    record = {k: v for k, v in case.items() if k not in ("prev_hash", "entry_hash")} | {
        "decided_by": "human",
        "final_decision": decision,
        "reviewer_note": reviewer_note,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    append_chained(DECISIONS_LOG, record)

    # mark resolved with an append-only event, chained like everything else
    append_chained(
        PENDING_QUEUE,
        {
            "case_id": case["case_id"],
            "status": "resolved",
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        },
    )


st.set_page_config(page_title="Prior Auth Review Queue", layout="wide")
st.title("Prior Auth — Human Review Queue")

pending = load_pending()
if not pending:
    st.success("No cases pending review. 🎉")
    st.stop()

case_ids = [c["case_id"] for c in pending]
selected = st.sidebar.radio(f"Pending cases ({len(pending)})", case_ids)
case = next(c for c in pending if c["case_id"] == selected)

det = case["determination"]
col1, col2, col3 = st.columns(3)
col1.metric("CPT Code", case["cpt_code"])
col2.metric("Draft Decision", det["decision"].upper())
col3.metric("Model Confidence", f"{det['confidence']:.0%}")

st.caption(case.get("eligibility_notes", ""))

st.subheader("Draft rationale")
st.write(det["rationale"])

st.subheader("Criteria & evidence")
evidence_by_id = {e["criterion_id"]: e for e in case.get("evidence", [])}
for crit in case.get("criteria", []):
    ev = evidence_by_id.get(crit["id"])
    status = ev["status"] if ev else "insufficient"
    label = f"{STATUS_ICONS.get(status, '❓')} {crit['id']} — {crit['text']}"
    with st.expander(label, expanded=(status != "met")):
        st.markdown(f"**Required:** {'yes' if crit['required'] else 'no'}")
        if ev:
            st.markdown(f"**Finding:** {ev['summary']}")
            st.markdown(
                "**Citations:** " + (", ".join(f"`{c}`" for c in ev["citations"]) or "none")
            )

st.divider()
st.subheader("Your determination")
note = st.text_area("Reviewer note (required for overrides)")
c1, c2, c3 = st.columns(3)
if c1.button("✅ Approve", use_container_width=True):
    save_decision(case, "approve", note)
    st.rerun()
if c2.button("❌ Deny", use_container_width=True):
    save_decision(case, "deny", note)
    st.rerun()
if c3.button("⏸ Pend / request info", use_container_width=True):
    save_decision(case, "pend", note)
    st.rerun()
