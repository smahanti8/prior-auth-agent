"""Intake/Validation: structural checks on the FHIR bundle and CPT code."""

import re
import uuid

from ..state import PriorAuthState

CPT_PATTERN = re.compile(r"^\d{5}$|^\d{4}[A-Z]$")  # 5 digits, or Category II/III


def intake(state: PriorAuthState) -> PriorAuthState:
    errors: list[str] = []
    bundle = state.get("fhir_bundle") or {}

    if bundle.get("resourceType") != "Bundle":
        errors.append("fhir_bundle.resourceType must be 'Bundle'")
    entries = bundle.get("entry") or []
    if not entries:
        errors.append("FHIR bundle has no entries")

    resource_types = {
        e.get("resource", {}).get("resourceType") for e in entries
    }
    if "Patient" not in resource_types:
        errors.append("Bundle is missing a Patient resource")

    cpt = state.get("cpt_code", "")
    if not CPT_PATTERN.match(cpt):
        errors.append(f"CPT code {cpt!r} is not a valid format")

    return {
        "case_id": state.get("case_id") or f"pa-{uuid.uuid4().hex[:8]}",
        "valid": not errors,
        "validation_errors": errors,
    }
