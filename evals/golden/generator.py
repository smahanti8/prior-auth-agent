"""FHIR bundle generator for golden eval fixtures.

Reads a CaseSpec and produces a valid FHIR R4 Bundle. The translation is
purely mechanical — no clinical inference, no added resources. The resource
ids in the output match the ids in the spec exactly, which is what makes
citations resolvable or not in resolve_citations().

Usage:
    python -m evals.golden.generator          # generate all cases
    python -m evals.golden.generator case_001 # generate one case by id prefix
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .schema import (
    BundleResourcesSpec,
    CaseSpec,
    ConditionSpec,
    DiagnosticReportSpec,
    ImagingStudySpec,
    ObservationSpec,
    PatientSpec,
    ProcedureSpec,
)

CASES_DIR = Path(__file__).resolve().parent / "cases"
BUNDLES_DIR = Path(__file__).resolve().parent / "bundles"


# ── per-resource builders ──────────────────────────────────────────────────────


def _make_patient(spec: PatientSpec) -> dict:
    gender = {"M": "male", "F": "female", "O": "other"}[spec.sex]
    return {
        "resourceType": "Patient",
        "id": spec.id,
        "gender": gender,
        "extension": [
            {"url": "http://hl7.org/fhir/StructureDefinition/patient-age",
             "valueInteger": spec.age}
        ],
    }


def _make_condition(spec: ConditionSpec) -> dict:
    r: dict = {
        "resourceType": "Condition",
        "id": spec.id,
        "clinicalStatus": {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                        "code": spec.clinical_status}]
        },
        "code": {"text": spec.display},
    }
    if spec.icd10:
        r["code"]["coding"] = [
            {"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": spec.icd10,
             "display": spec.display}
        ]
    if spec.onset_date:
        r["onsetDateTime"] = spec.onset_date
    if spec.abatement_date:
        r["abatementDateTime"] = spec.abatement_date
    if spec.note:
        r["note"] = [{"text": spec.note}]
    return r


def _make_observation(spec: ObservationSpec) -> dict:
    r: dict = {
        "resourceType": "Observation",
        "id": spec.id,
        "status": "final",
        "code": {"text": spec.display},
    }
    if spec.loinc:
        r["code"]["coding"] = [
            {"system": "http://loinc.org", "code": spec.loinc, "display": spec.display}
        ]
    if spec.value is not None and spec.unit:
        r["valueQuantity"] = {"value": spec.value, "unit": spec.unit}
    if spec.date:
        r["effectiveDateTime"] = spec.date
    if spec.note:
        r["note"] = [{"text": spec.note}]
    return r


def _make_imaging_study(spec: ImagingStudySpec) -> dict:
    r: dict = {
        "resourceType": "ImagingStudy",
        "id": spec.id,
        "status": "available",
        "description": spec.description,
        "modality": [{"code": spec.modality}],
        "series": [
            {"uid": f"urn:oid:{spec.id}",
             "bodySite": {"display": spec.body_site}}
        ],
    }
    if spec.finding:
        r["note"] = [{"text": spec.finding}]
    if spec.date:
        r["started"] = spec.date
    return r


def _make_procedure(spec: ProcedureSpec) -> dict:
    r: dict = {
        "resourceType": "Procedure",
        "id": spec.id,
        "status": spec.status,
        "code": {"text": spec.display},
    }
    if spec.start_date and spec.end_date:
        r["performedPeriod"] = {"start": spec.start_date, "end": spec.end_date}
    if spec.note:
        r["note"] = [{"text": spec.note}]
    return r


def _make_diagnostic_report(spec: DiagnosticReportSpec) -> dict:
    r: dict = {
        "resourceType": "DiagnosticReport",
        "id": spec.id,
        "status": "final",
        "code": {"text": spec.display},
        "conclusion": spec.conclusion,
    }
    if spec.date:
        r["effectiveDateTime"] = spec.date
    return r


# ── bundle assembly ────────────────────────────────────────────────────────────


def generate_bundle(spec: CaseSpec) -> dict:
    """Translate a CaseSpec into a FHIR R4 Bundle.

    Only resources declared in spec.bundle_resources are included. The
    generator adds nothing and infers nothing. Resource ids are preserved
    exactly so citations in ground_truth.criteria can be verified.
    """
    res: BundleResourcesSpec = spec.bundle_resources
    entries: list[dict] = []

    if not res.omit_patient and res.patient is not None:
        entries.append({"resource": _make_patient(res.patient)})

    for c in res.conditions:
        entries.append({"resource": _make_condition(c)})
    for o in res.observations:
        entries.append({"resource": _make_observation(o)})
    for i in res.imaging_studies:
        entries.append({"resource": _make_imaging_study(i)})
    for p in res.procedures:
        entries.append({"resource": _make_procedure(p)})
    for d in res.diagnostic_reports:
        entries.append({"resource": _make_diagnostic_report(d)})

    return {
        "resourceType": "Bundle",
        "id": spec.id,
        "type": "collection",
        "entry": entries,
    }


def generate_all(
    cases_dir: Path = CASES_DIR,
    bundles_dir: Path = BUNDLES_DIR,
    filter_prefix: str = "",
) -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    generated = 0
    for yaml_path in sorted(cases_dir.glob("*.yaml")):
        if filter_prefix and not yaml_path.stem.startswith(filter_prefix):
            continue
        spec = CaseSpec.load(yaml_path)
        bundle = generate_bundle(spec)
        out_path = bundles_dir / f"{yaml_path.stem}.json"
        out_path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
        print(f"  generated {out_path.name}  ({len(bundle['entry'])} entries)")
        generated += 1
    print(f"\n{generated} bundle(s) written to {bundles_dir}")


if __name__ == "__main__":
    prefix = sys.argv[1] if len(sys.argv) > 1 else ""
    generate_all(filter_prefix=prefix)
