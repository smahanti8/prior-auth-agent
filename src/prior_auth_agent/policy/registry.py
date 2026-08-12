"""Load versioned criterion YAML files and perform version-date lookups."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from .schema import CriterionSpec

_CRITERIA_DIR = Path(__file__).resolve().parents[3] / "data" / "policy" / "criteria"


def load_all(criteria_dir: Path = _CRITERIA_DIR) -> list[CriterionSpec]:
    """Load and validate every *.yaml file in criteria_dir."""
    specs = []
    for path in sorted(criteria_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        specs.append(CriterionSpec.model_validate(data))
    return specs


def load_active(
    criterion_id: str,
    as_of_date: str,
    criteria_dir: Path = _CRITERIA_DIR,
) -> Optional[CriterionSpec]:
    """Return the most-recent version of criterion_id active as of as_of_date.

    as_of_date is an ISO date string, e.g. "2026-07-29". The active version is
    the one with the highest policy_effective_date that is <= as_of_date. Both
    versions coexist in the directory; past determinations can be explained by
    loading the version that was active when the determination was made.
    """
    candidates = [
        spec for spec in load_all(criteria_dir)
        if spec.criterion_id == criterion_id
        and spec.policy_effective_date <= as_of_date
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.policy_effective_date)


def load_by_cpt(
    cpt_code: str,
    as_of_date: str,
    criteria_dir: Path = _CRITERIA_DIR,
) -> list[CriterionSpec]:
    """Return all active criteria for cpt_code as of as_of_date, one per criterion_id.

    Returns the most-recent version of each criterion_id that lists cpt_code in
    its cpt_codes field and has policy_effective_date <= as_of_date. Results are
    sorted by criterion_id for deterministic ordering. Returns an empty list if
    no criteria are encoded for this CPT code.
    """
    by_id: dict[str, CriterionSpec] = {}
    for spec in load_all(criteria_dir):
        if cpt_code not in spec.cpt_codes:
            continue
        if spec.policy_effective_date > as_of_date:
            continue
        existing = by_id.get(spec.criterion_id)
        if existing is None or spec.policy_effective_date > existing.policy_effective_date:
            by_id[spec.criterion_id] = spec
    return sorted(by_id.values(), key=lambda s: s.criterion_id)
