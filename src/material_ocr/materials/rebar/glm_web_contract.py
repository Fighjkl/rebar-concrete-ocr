"""Adapt reviewed GLM matching metadata to the existing Vue/Java contract.

No OCR values or association decisions are changed. Raw grouped candidates are
retained alongside the flat, located candidates understood by the workbench.
"""
from __future__ import annotations

from copy import deepcopy
import json
from .review_policy import apply_review_policy


_GROUP_LABELS = {
    "nameplates": "铭牌候选", "deliveries": "送货单候选",
    "certificate_pages": "质量证明书候选",
    "review_only_certificates": "质量证明书编号候选",
    "certificate_number_conflicts": "质保书编号冲突候选",
    "heat_lot_conflicts": "炉批号冲突候选",
}


def _items(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _unique(values):
    seen, result = set(), []
    for value in values:
        key = json.dumps(value, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _page(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and str(number) == str(value) else None


def _locations(value):
    """Retain measured evidence; add only page-level fallbacks when available."""
    if not isinstance(value, dict):
        number = _page(value)
        return [dict(page_no=number, precision="page")] if number else []
    locations = []
    for key in ("locations", "evidence", "evidence_views"):
        for location in _items(value.get(key)):
            if isinstance(location, dict):
                location = deepcopy(location)
                number = _page(location.get("page_no", location.get("page")))
                if number:
                    location["page_no"] = number
                    locations.append(location)
    if not locations:
        for role in ("manufacturer", "supplier", "nameplate", "certificate"):
            if isinstance(value.get(role), dict):
                locations.extend(_locations(value[role]))
    if not locations:
        number = _page(value.get("page_no", value.get("page")))
        if number:
            locations.append(dict(page_no=number, precision="page"))
    return _unique(locations)


def _diagnostics(value, *, field="", locations=()):
    result = []
    for item in _items(value):
        if isinstance(item, dict):
            issue = deepcopy(item)
            issue.setdefault("code", "glm_field_requires_review" if field else "glm_match_requires_review")
            issue.setdefault("reason", str(issue.get("code") or "待人工核验"))
            if field:
                issue.setdefault("field", field)
            issue["locations"] = _locations(issue) or deepcopy(list(locations))
        else:
            issue = dict(code="glm_field_requires_review" if field else "glm_match_requires_review",
                         reason=str(item), locations=deepcopy(list(locations)))
            if field:
                issue["field"] = field
        result.append(issue)
    return _unique(result)


def _candidate_reason(candidate, group, field=""):
    reason = candidate.get("reason") or candidate.get("review_reason")
    if reason:
        return str(reason)
    details = []
    for key, label in (("value", "候选值"), ("grade", "牌号"),
                       ("diameter_mm", "规格"), ("delivery_no", "送货单号")):
        if candidate.get(key) not in (None, ""):
            details.append(label + "「" + str(candidate[key]) + "」")
    if candidate.get("heat_lots"):
        details.append("炉批号「" + "、".join(map(str, candidate["heat_lots"])) + "」")
    for key, label in (("supplier", "供应单位"), ("manufacturer", "生产单位")):
        company = candidate.get(key)
        if isinstance(company, dict) and company.get("value"):
            details.append(label + "「" + str(company["value"]) + "」")
    label = (field + "候选") if field else _GROUP_LABELS.get(group, "关联候选")
    return label + ("：" + "；".join(details) if details else "；请核对来源原图")


def _candidate(value, group, field=""):
    if isinstance(value, dict):
        item = deepcopy(value)
    elif group == "certificate_pages":
        item = dict(page_no=value)
    else:
        item = dict(value=deepcopy(value))
    item["candidate_group"] = group
    if field:
        item["field"] = field
    item["reason"] = _candidate_reason(item, group, field)
    item["locations"] = _locations(item)
    return item


def adapt_glm_ledger(ledger, pdf_name, pdf_digest, ocr_model="local-glm"):
    """Return a deep copy compatible with the current twelve-column workbench."""
    result = deepcopy(ledger)
    result.update(schema_version="automatic-ledger-v1", pdf_name=pdf_name,
                  pdf_sha256=pdf_digest, model=ocr_model, reviewed=False)
    result.setdefault("matching_requires_human", False)
    result.setdefault("review_stage", "before_excel_export")
    if "diagnostics" in result:
        result["diagnostics"] = _diagnostics(result["diagnostics"])
    apply_review_policy(result)
    for record in result.get("records", []):
        raw_candidates = record.get("glm_match_candidates", record.get("match_candidates", []))
        candidates = []
        if isinstance(raw_candidates, dict):
            record["glm_match_candidates"] = deepcopy(raw_candidates)
            for group, values in raw_candidates.items():
                candidates.extend(_candidate(item, group) for item in _items(values))
        else:
            candidates.extend(_candidate(item, item.get("candidate_group", "association"))
                              for item in _items(raw_candidates) if isinstance(item, dict))
        row_locations = []
        for source in record.get("sources", {}).values():
            row_locations.extend(_locations(source))
        row_locations = _unique(row_locations) or _locations(record)
        record["diagnostics"] = _diagnostics(record.get("diagnostics"), locations=row_locations)
        for column, field in record.get("fields", {}).items():
            evidence = _items(field.get("evidence"))
            field["diagnostics"] = _diagnostics(field.get("diagnostics"),
                field=column, locations=_locations(field))
            # Vue uses this flag for both red cells and the review filter.
            # A read value with a contradictory association remains readable,
            # but must not become a green, fully confirmed field.
            field["found_in_ocr"] = bool(field.get("value") and evidence
                and field.get("found_in_ocr") is not False
                and field.get("status") in (None, "", "observed")
                and not field["diagnostics"])
            field.setdefault("polygon", next((item.get("polygon_page") for item in evidence
                if isinstance(item, dict) and item.get("polygon_page")), None))
            candidates.extend(_candidate(item, "field", column)
                              for item in _items(field.get("candidates")))
        record["match_candidates"] = _unique(candidates)
    return result
