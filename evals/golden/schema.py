"""Pydantic models for golden eval fixture case specs.

Each case spec is a YAML file authored by a human. This module provides
the schema that validates the YAML on load. The generator reads these
models to produce FHIR bundles; the runner reads them to assert on pipeline
output.

Authoring rules:
- ground_truth.criteria[].rationale is mandatory — it is the audit trail.
- ground_truth.outcome.rationale is mandatory.
- Do not modify expected_status or expected_citations without updating rationale.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field


# ── bundle resource specs ──────────────────────────────────────────────────────


class PatientSpec(BaseModel):
    id: str
    age: int
    sex: Literal["M", "F", "O"]


class ConditionSpec(BaseModel):
    id: str
    display: str
    clinical_status: str = "active"
    icd10: Optional[str] = None
    onset_date: Optional[str] = None
    abatement_date: Optional[str] = None
    note: Optional[str] = None


class ObservationSpec(BaseModel):
    id: str
    display: str
    loinc: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    date: Optional[str] = None
    note: Optional[str] = None


class ImagingStudySpec(BaseModel):
    id: str
    modality: str
    body_site: str
    description: str = ""
    finding: str = ""
    date: Optional[str] = None


class ProcedureSpec(BaseModel):
    id: str
    display: str
    status: str = "completed"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    note: Optional[str] = None


class DiagnosticReportSpec(BaseModel):
    id: str
    display: str
    conclusion: str = ""
    date: Optional[str] = None


class CoverageSpec(BaseModel):
    id: str
    status: str = "active"
    payer_display: str = "Acme Health Plan"


class BundleResourcesSpec(BaseModel):
    patient: Optional[PatientSpec] = None
    omit_patient: bool = False  # set True to intentionally produce a bundle without Patient
    coverage: Optional[CoverageSpec] = None
    conditions: list[ConditionSpec] = Field(default_factory=list)
    observations: list[ObservationSpec] = Field(default_factory=list)
    imaging_studies: list[ImagingStudySpec] = Field(default_factory=list)
    procedures: list[ProcedureSpec] = Field(default_factory=list)
    diagnostic_reports: list[DiagnosticReportSpec] = Field(default_factory=list)


# ── ground truth ───────────────────────────────────────────────────────────────


class CriterionGroundTruth(BaseModel):
    id: str
    text: str
    required: bool
    expected_status: Literal["met", "insufficient", "not_met"]
    expected_citations: list[str] = Field(default_factory=list)
    rationale: str


class OutcomeSpec(BaseModel):
    stage: Literal["determination", "intake", "eligibility"]
    decision: Optional[Literal["approve", "insufficient_evidence"]] = None
    route: Optional[Literal["auto", "hitl"]] = None
    valid: Optional[bool] = None
    eligible: Optional[bool] = None
    validation_error_contains: Optional[str] = None
    rationale: str


class GroundTruth(BaseModel):
    criteria: list[CriterionGroundTruth] = Field(default_factory=list)
    outcome: OutcomeSpec


# ── top-level case spec ────────────────────────────────────────────────────────


class CaseSpec(BaseModel):
    id: str
    title: str
    cpt: str
    date: str
    bundle_resources: BundleResourcesSpec
    ground_truth: GroundTruth

    @classmethod
    def load(cls, path: Path) -> "CaseSpec":
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)

    @classmethod
    def load_all(cls, cases_dir: Path) -> list["CaseSpec"]:
        specs = []
        for yaml_path in sorted(cases_dir.glob("*.yaml")):
            specs.append(cls.load(yaml_path))
        return specs
