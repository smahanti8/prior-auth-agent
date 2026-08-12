"""Criteria Mapper: turn retrieved policy text into discrete, checkable criteria.

For procedures with encoded criteria (D12), the mapper uses the encoded list as
the authoritative criteria set. The LLM reads retrieved policy text and maps each
criterion it identifies to an encoded ID (or null) — that mapping is the divergence
signal. For procedures without encoded criteria, behavior is unchanged.
"""

from datetime import date

from ..llm import structured_call
from ..policy.evaluator import evaluate_criterion
from ..policy.registry import load_by_cpt
from ..state import PriorAuthState

# ── Schema for the un-encoded fallback path (existing behavior) ────────────────

_SCHEMA_UNENCODED = {
    "type": "object",
    "properties": {
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id":       {"type": "string"},
                    "text":     {"type": "string"},
                    "required": {"type": "boolean"},
                },
                "required": ["id", "text", "required"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["criteria"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are a utilization-management analyst. Given payer policy excerpts for a "
    "procedure, extract the discrete medical-necessity criteria as an enumerated "
    "list. Each criterion must be a single, independently checkable condition. "
    "Mark required=true for criteria the policy states must all be met, "
    "required=false for alternatives/optional pathways. Use ids c1, c2, ..."
)

_ENCODED_SUFFIX = (
    "\n\nFor this procedure, the following criterion IDs have been pre-encoded: "
    "{id_list}. For each criterion you identify in the retrieved policy text, "
    "assign it the matching encoded_id from this list, or null if it does not "
    "correspond to any of them. Assign each encoded_id at most once — if two "
    "passages describe the same criterion, assign the ID to the clearer match "
    "and use null for the other."
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _build_encoded_schema(valid_ids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "encoded_id": {"type": ["string", "null"]},
                        "text":       {"type": "string"},
                        "required":   {"type": "boolean"},
                    },
                    "required": ["encoded_id", "text", "required"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["criteria"],
        "additionalProperties": False,
    }


def _build_predicate_hint(spec, result) -> str:
    if result.status == "met":
        paths = ", ".join(result.matched_paths) if result.matched_paths else "bundle"
        return (
            f"Predicate evaluation: MET — {result.reason}. "
            f"Verify and cite: {paths}."
        )
    if result.status == "not_met":
        return (
            f"Predicate evaluation: NOT MET — {result.reason}. "
            f"Do not mark this criterion met."
        )
    return (
        f"Predicate evaluation: INSUFFICIENT — {result.reason}. "
        f"Status will be insufficient unless the bundle contains contrary evidence."
    )


def _compute_divergence(llm_criteria: list[dict], valid_ids: list[str]) -> dict:
    """Deterministic set comparison between LLM ID mapping and the encoded set."""
    llm_mapped: dict[str, list[str]] = {}
    unmatched: list[dict] = []

    for item in llm_criteria:
        eid = item.get("encoded_id")
        if eid is None or eid not in valid_ids:
            unmatched.append({"text": item["text"], "required": item["required"]})
        else:
            llm_mapped.setdefault(eid, []).append(item["text"])

    missing = sorted(set(valid_ids) - set(llm_mapped))
    duplicate_mappings = [
        {"encoded_id": eid, "texts": texts}
        for eid, texts in llm_mapped.items()
        if len(texts) > 1
    ]
    return {"missing": missing, "unmatched": unmatched, "duplicate_mappings": duplicate_mappings}


def _has_divergence(d: dict) -> bool:
    return bool(d["missing"] or d["unmatched"] or d["duplicate_mappings"])


def bundle_to_fragments(bundle: dict) -> dict[str, list[dict]]:
    """Convert a FHIR bundle to the resource_type → [resource] format for predicate eval."""
    fragments: dict[str, list[dict]] = {}
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        rtype = resource.get("resourceType")
        if rtype:
            fragments.setdefault(rtype, []).append(resource)
    return fragments


# ── Node ───────────────────────────────────────────────────────────────────────


def criteria_mapper(state: PriorAuthState) -> PriorAuthState:
    policy_text = "\n\n---\n\n".join(
        f"[{c['source']}]\n{c['text']}" for c in state["policy_chunks"]
    )
    cpt_code = state["cpt_code"]
    today = date.today().isoformat()
    encoded_specs = load_by_cpt(cpt_code, as_of_date=today)

    if not encoded_specs:
        # Un-encoded path: existing behavior unchanged
        result = structured_call(
            system=SYSTEM,
            user_content=f"CPT code: {cpt_code}\n\nPolicy excerpts:\n\n{policy_text}",
            schema=_SCHEMA_UNENCODED,
        )
        return {
            "criteria":           result["criteria"],
            "criteria_versions":  [],
            "criteria_divergence": None,
        }

    # Encoded path
    valid_ids = [s.criterion_id for s in encoded_specs]
    system_encoded = SYSTEM + _ENCODED_SUFFIX.format(id_list=", ".join(valid_ids))

    llm_result = structured_call(
        system=system_encoded,
        user_content=f"CPT code: {cpt_code}\n\nPolicy excerpts:\n\n{policy_text}",
        schema=_build_encoded_schema(valid_ids),
    )

    divergence = _compute_divergence(llm_result["criteria"], valid_ids)

    # Build authoritative criteria from encoded specs (not from LLM paraphrase)
    fragments = bundle_to_fragments(state.get("fhir_bundle", {}))
    criteria = []
    for spec in encoded_specs:
        eval_result = evaluate_criterion(spec, fragments)
        criteria.append({
            "id":              spec.criterion_id,
            "text":            spec.criterion_text,
            "required":        spec.required,
            "encoded_version": spec.version,
            "predicate_hint":  _build_predicate_hint(spec, eval_result),
        })

    criteria_versions = [
        {"criterion_id": s.criterion_id, "version": s.version}
        for s in encoded_specs
    ]

    return {
        "criteria":            criteria,
        "criteria_versions":   criteria_versions,
        "criteria_divergence": divergence if _has_divergence(divergence) else None,
    }
