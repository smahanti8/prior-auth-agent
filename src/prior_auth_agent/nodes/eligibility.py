"""Eligibility check.

Stub: replace with a real 270/271 eligibility transaction or your payer's
coverage API. Reads the Coverage resource from the bundle if present.
"""

from ..state import PriorAuthState


def eligibility(state: PriorAuthState) -> PriorAuthState:
    entries = (state.get("fhir_bundle") or {}).get("entry", [])
    coverage = next(
        (
            e["resource"]
            for e in entries
            if e.get("resource", {}).get("resourceType") == "Coverage"
        ),
        None,
    )

    if coverage is None:
        return {
            "eligible": False,
            "eligibility_notes": "No Coverage resource in bundle; cannot verify eligibility.",
        }

    active = coverage.get("status") == "active"
    return {
        "eligible": active,
        "eligibility_notes": (
            f"Coverage status={coverage.get('status')!r}, "
            f"payer={coverage.get('payor', [{}])[0].get('display', 'unknown')}"
        ),
    }
