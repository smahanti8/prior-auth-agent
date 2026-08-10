"""Evaluate a PredicateNode against bundle fragments.

bundle_fragments: dict[resource_type, list[resource_dict]]
  A subset of the FHIR bundle keyed by resource type. Each value is a list
  of resource dicts. This shape is used in conformance test fixtures and maps
  cleanly to a real bundle (group entries by resourceType before calling).

EvalResult.status vocabulary matches Evidence.status in state.py:
  met          — predicate satisfied by the data
  not_met      — data affirmatively contradicts the predicate
  insufficient — chart silent, ambiguous, or value present but uninterpretable

not_met / insufficient rule (DECISIONS.md D12):
  not_met only when data affirmatively contradicts what the criterion requires.
  insufficient when the chart is silent, a required resource is absent, dates
  are missing, or a value is present but its interpretation is beyond the
  evaluator's scope (e.g. clinical score thresholds in C3).
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterator, Literal

from .schema import (
    CriterionSpec,
    FieldDocumentedParams,
    FindingPresentParams,
    MinimumDurationMonthsParams,
    PredicateNode,
    ResourceExistsParams,
)

Status = Literal["met", "not_met", "insufficient"]


@dataclass
class EvalResult:
    status: Status
    reason: str
    matched_paths: list[str] = field(default_factory=list)


def evaluate(predicate: PredicateNode, bundle_fragments: dict[str, list[dict]]) -> EvalResult:
    if predicate.all_of is not None:
        return _eval_all_of(predicate.all_of, bundle_fragments)
    if predicate.any_of is not None:
        return _eval_any_of(predicate.any_of, bundle_fragments)
    if predicate.resource_exists is not None:
        return _eval_resource_exists(predicate.resource_exists, bundle_fragments)
    if predicate.finding_present is not None:
        return _eval_finding_present(predicate.finding_present, bundle_fragments)
    if predicate.field_documented is not None:
        return _eval_field_documented(predicate.field_documented, bundle_fragments)
    if predicate.minimum_duration_months is not None:
        return _eval_minimum_duration_months(predicate.minimum_duration_months, bundle_fragments)
    raise ValueError("PredicateNode has no operator set")


def evaluate_criterion(spec: CriterionSpec, bundle_fragments: dict[str, list[dict]]) -> EvalResult:
    return evaluate(spec.predicate, bundle_fragments)


# ── Composite operators ────────────────────────────────────────────────────────


def _eval_all_of(children: list[PredicateNode], bundle_fragments: dict) -> EvalResult:
    results = [evaluate(c, bundle_fragments) for c in children]
    not_met = [r for r in results if r.status == "not_met"]
    if not_met:
        return EvalResult(
            status="not_met",
            reason=f"all_of failed: {not_met[0].reason}",
            matched_paths=not_met[0].matched_paths,
        )
    insuf = [r for r in results if r.status == "insufficient"]
    if insuf:
        return EvalResult(
            status="insufficient",
            reason=f"all_of incomplete: {insuf[0].reason}",
            matched_paths=insuf[0].matched_paths,
        )
    return EvalResult(
        status="met",
        reason="all_of: all conditions satisfied",
        matched_paths=[p for r in results for p in r.matched_paths],
    )


def _eval_any_of(children: list[PredicateNode], bundle_fragments: dict) -> EvalResult:
    results = [evaluate(c, bundle_fragments) for c in children]
    met = [r for r in results if r.status == "met"]
    if met:
        return EvalResult(
            status="met",
            reason=f"any_of: {met[0].reason}",
            matched_paths=met[0].matched_paths,
        )
    if all(r.status == "not_met" for r in results):
        return EvalResult(
            status="not_met",
            reason=f"any_of: all alternatives not_met — {results[0].reason}",
        )
    return EvalResult(
        status="insufficient",
        reason="any_of: no alternative met; at least one insufficient",
    )


# ── Leaf operators ─────────────────────────────────────────────────────────────


def _eval_resource_exists(params: ResourceExistsParams, bundle_fragments: dict) -> EvalResult:
    resources = bundle_fragments.get(params.resource_type, [])
    if resources:
        return EvalResult(
            status="met",
            reason=f"{params.resource_type} present ({len(resources)} resource(s))",
            matched_paths=[f"{params.resource_type}/{r.get('id', '?')}" for r in resources],
        )
    return EvalResult(
        status="insufficient",
        reason=f"{params.resource_type} not present in bundle",
    )


def _all_text_values(obj: Any) -> Iterator[str]:
    """Yield every string value in a nested dict/list structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _all_text_values(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _all_text_values(item)


def _matches_any_term(text: str, terms: list[str], case_insensitive: bool) -> bool:
    haystack = text.lower() if case_insensitive else text
    return any((t.lower() if case_insensitive else t) in haystack for t in terms)


def _eval_finding_present(params: FindingPresentParams, bundle_fragments: dict) -> EvalResult:
    resources = bundle_fragments.get(params.resource_type, [])
    if not resources:
        return EvalResult(
            status="insufficient",
            reason=f"{params.resource_type} not present in bundle",
        )
    for resource in resources:
        rid = f"{params.resource_type}/{resource.get('id', '?')}"
        all_text = list(_all_text_values(resource))
        # Contradicting terms checked first: affirmative contradiction → not_met
        if params.contradicting_terms:
            for text in all_text:
                if _matches_any_term(text, params.contradicting_terms, params.case_insensitive):
                    return EvalResult(
                        status="not_met",
                        reason=f"{rid}: contradicting finding present in display text",
                    )
        for text in all_text:
            if _matches_any_term(text, params.affirmative_terms, params.case_insensitive):
                return EvalResult(
                    status="met",
                    reason=f"{rid}: affirmative finding present",
                    matched_paths=[rid],
                )
    return EvalResult(
        status="insufficient",
        reason=(
            f"{params.resource_type} present but no affirmative or contradicting "
            "term matched in any resource"
        ),
    )


def _get_path(resource: dict, path: str) -> Any:
    current: Any = resource
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _eval_field_documented(params: FieldDocumentedParams, bundle_fragments: dict) -> EvalResult:
    resources = bundle_fragments.get(params.resource_type, [])
    if not resources:
        return EvalResult(
            status="insufficient",
            reason=f"{params.resource_type} not present in bundle",
        )
    for resource in resources:
        rid = f"{params.resource_type}/{resource.get('id', '?')}"
        value = _get_path(resource, params.field_path)
        if value is not None and value != "" and value != [] and value != {}:
            return EvalResult(
                status="met",
                reason=f"{rid}: field '{params.field_path}' documented",
                matched_paths=[f"{rid}.{params.field_path}"],
            )
    return EvalResult(
        status="insufficient",
        reason=(
            f"{params.resource_type} present but '{params.field_path}' "
            "absent or empty on all resources"
        ),
    )


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def _at_least_n_months(start: date, end: date, n: int) -> bool:
    """True if end >= start + n calendar months."""
    raw = start.month + n
    threshold_year = start.year + (raw - 1) // 12
    threshold_month = (raw - 1) % 12 + 1
    max_day = calendar.monthrange(threshold_year, threshold_month)[1]
    threshold = date(threshold_year, threshold_month, min(start.day, max_day))
    return end >= threshold


def _eval_minimum_duration_months(
    params: MinimumDurationMonthsParams,
    bundle_fragments: dict,
) -> EvalResult:
    resources = bundle_fragments.get(params.resource_type, [])
    if not resources:
        return EvalResult(
            status="insufficient",
            reason=f"{params.resource_type} not present in bundle",
        )
    best: tuple[date, date] | None = None
    for resource in resources:
        start_val = _get_path(resource, params.start_path)
        end_val = _get_path(resource, params.end_path)
        if not start_val or not end_val:
            continue
        try:
            start, end = _parse_date(start_val), _parse_date(end_val)
        except (ValueError, TypeError):
            continue
        if best is None or (end - start) > (best[1] - best[0]):
            best = (start, end)
    if best is None:
        return EvalResult(
            status="insufficient",
            reason=(
                f"{params.resource_type} present but start/end dates "
                "unavailable for duration check"
            ),
        )
    start, end = best
    if _at_least_n_months(start, end, params.min_months):
        return EvalResult(
            status="met",
            reason=f"{params.resource_type}: {start} to {end} >= {params.min_months} calendar months",
            matched_paths=[f"{params.resource_type}[{start}/{end}]"],
        )
    return EvalResult(
        status="not_met",
        reason=f"{params.resource_type}: {start} to {end} < {params.min_months} calendar months",
    )
