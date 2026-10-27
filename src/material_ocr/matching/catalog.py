"""Evidence-preserving matching for steel rebar forms.

The matcher returns candidates and review states rather than silently selecting
one page.  All identifier comparisons retain zero/letter distinctions.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from material_ocr.ocr.attachments import (DOCUMENT_ALIASES, compact_text, extract_attachment_fields,
                              merge_rules, normalize_identifier, normalize_similar_value,
                              normalize_text, parse_quantity_t, validate_attachment_fields,
                              classify_page_text, build_regions)


RELATIONS = (
    "inspection_to_quality_certificate",
    "inspection_to_delivery_note",
    "nameplate_to_quality_certificate",
    "approval_to_inspection_record",
)


def _field_key(field: str, value: Mapping[str, Any]) -> str:
    if field in {'quantity_t', 'weight_t'} and value.get('value_t') is not None:
        return str(Decimal(str(value['value_t'])).normalize())
    if field == 'diameter_mm':
        diameter = _diameter(value.get('value', ''))
        if diameter is not None:
            return str(diameter.normalize())
    return normalize_identifier(value.get('value', ''))


def _field_values(region: Mapping[str, Any], field: str) -> list[dict]:
    fields = region.get("extracted_fields")
    if not fields:
        fields = extract_attachment_fields(region.get("text", region.get("raw_text", "")))
    values = fields.get(field, []) if isinstance(fields, Mapping) else []
    if isinstance(values, (str, int, float)):
        values = [{"value": str(values)}]
    output = [dict(item) if isinstance(item, Mapping) else {"value": str(item)} for item in values]
    # Strict relation keys must not use OCR labels or mixed table sentences.
    if field == "certificate_no":
        output = [x for x in output if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{2,}", compact_text(_value(x))) and compact_text(_value(x)).upper() not in {"CERTIFICATE", "LICENSE NO", "LICENSENO"}]
    elif field == "heat_lot_no":
        output = [x for x in output if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{2,}", compact_text(_value(x)))
                  and compact_text(_value(x)).upper() not in {"BATCHNO", "HEATNO"}
                  and not re.fullmatch(r"HRB(?:[0-9]{3,4})(?:E|B)?", compact_text(_value(x)), re.I)]
    valid, _ = validate_attachment_fields({field: output}, region.get("document_type"))
    return valid.get(field, [])


def _value(item: Mapping[str, Any]) -> str:
    return normalize_text(item.get("value", item.get("raw_value", ""))).strip()


def _identifier_value(item: Mapping[str, Any]) -> str:
    return normalize_identifier(item.get("normalized_value", _value(item)))


def _number(value: Any, *, default_unit: str = "t") -> Decimal | None:
    if value is None:
        return None
    parsed = parse_quantity_t(value, default_unit=default_unit)
    if parsed is not None:
        return parsed
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _diameter(value: Any) -> Decimal | None:
    m = re.search(r"(?:Φ|φ|Ø|直径)?\s*([0-9]+(?:\.[0-9]+)?)", compact_text(value))
    if not m:
        return None
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return None


def _date(value: Any) -> str:
    text = normalize_text(value)
    match = re.search(r"(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})", text)
    if not match:
        match = re.search(r"(20\d{2})(\d{2})(\d{2})", re.sub(r"\D", "", text))
    return "%04d-%02d-%02d" % tuple(map(int, match.groups())) if match else compact_text(text)


def _contains(a: str, b: str) -> bool:
    a, b = compact_text(a), compact_text(b)
    return bool(a and b and (a == b or a in b or b in a))


def _region_type(region: Mapping[str, Any], page: Mapping[str, Any] | None = None) -> str:
    value = region.get("document_type") or (page or {}).get("document_type") or "unknown"
    return str(value)


def _iter_regions(index: Mapping[str, Any], rules: Mapping[str, Any] | None = None) -> Iterable[dict]:
    for page in index.get("pages", []):
        # Focused OCR is evidence attached to an entity/row, never a free
        # standing match candidate.  Gather it before normalising regions so
        # it can be fused only into the same entity and physical row.
        focus = [x for x in page.get("regions", []) if x.get("layout_role") == "field_focus"]
        observations = []
        for x in focus:
            for o in x.get("field_observations", []):
                oo = dict(o); oo.setdefault("entity_key", x.get("entity_key", "full")); observations.append(oo)
        for region in page.get("regions", []):
            item = dict(region)
            item.setdefault("page_no", page.get("page_no"))
            item.setdefault("document_type", page.get("document_type", "unknown"))
            item.setdefault("rotation_ccw", page.get("rotation_ccw", 0))
            item.setdefault("source_image", page.get("source_image"))
            item.setdefault("pdf_sha256", index.get("pdf_sha256"))
            item.setdefault("preprocessing", page.get("preprocessing") or {})
            item.setdefault("raw_text", item.get("text", ""))
            item.setdefault("text", item.get("raw_text", ""))
            if item.get("document_type") in (None, "unknown"):
                inferred = classify_page_text(item["text"], rules=rules)
                if inferred != "unknown":
                    item["document_type"] = inferred
            recovered = extract_attachment_fields(item["text"], rules=rules)
            existing = item.get("extracted_fields") if isinstance(item.get("extracted_fields"), Mapping) else {}
            merged = {key: list(values) for key, values in existing.items()}
            for field, values in recovered.items():
                seen = {str(x.get("value")) for x in merged.get(field, []) if isinstance(x, Mapping)}
                merged.setdefault(field, [])
                merged[field].extend(x for x in values if str(x.get("value")) not in seen)
            # Header fields (certificate number) are page/entity scoped. Row
            # measurements must have the exact canonical row id; a missing row
            # id is deliberately not propagated into table rows.
            entity_key = item.get("entity_key", "full")
            focused_review = []
            for crop in focus:
                if crop.get('entity_key', 'full') != entity_key or not crop.get('gpu_ocr'):
                    continue
                crop_rows = {o.get('row_id') for o in crop.get('field_observations', [])}
                crop_rows.update(f"p{item['page_no']:03d}_{entity_key}_row{w['physical_row_index']}"
                                 for w in crop.get('row_windows', []))
                header = crop.get('priority_field') == 'certificate_no' and None in crop_rows
                if header or item.get('row_id') in crop_rows or (not crop_rows and item.get('row_id') is None):
                    focused_review.extend(crop.get('review_reasons', []))
            if focused_review:
                item['focused_ocr_review_reasons'] = sorted(set(focused_review))
            for obs in observations:
                if obs.get("field") not in {"certificate_no", "heat_lot_no", "quantity_t", "weight_t", "diameter_mm", "grade", "manufacturer"}:
                    continue
                if obs.get("entity_key", entity_key) != entity_key:
                    continue
                orow, irow = obs.get("row_id"), item.get("row_id")
                header_certificate = obs["field"] == "certificate_no" and orow is None
                if not header_certificate and orow != irow and (orow is not None or irow is not None):
                    continue
                candidate = dict(obs)
                candidate.setdefault("value", candidate.get("raw_value"))
                merged.setdefault(obs["field"], []).append(candidate)
            valid, rejected = validate_attachment_fields(merged, item.get("document_type"))
            # Repeated CN/EN labels and repeated OCR crops represent evidence
            # for one value, not additional field choices. Keep all provenance.
            for field, values in valid.items():
                unique = {}
                for value in values:
                    key = _field_key(field, value)
                    if key not in unique:
                        unique[key] = dict(value)
                    unique[key].setdefault("evidence_refs", []).append(value.get("evidence"))
                valid[field] = list(unique.values())
            item["extracted_fields"] = valid
            item["rejected_fields"] = {**item.get("rejected_fields", {}), **rejected}
            if not item.get('layout_role') and sum(bool(b.get('polygon_page')) for b in item.get('blocks', [])) >= 2:
                refreshed = build_regions(item['blocks'], page_no=item['page_no'],
                                          source_image=item.get('source_image'), rotation_ccw=item['rotation_ccw'],
                                          rules=rules, region_id=item.get('region_id'), document_type=item['document_type'])
                if len(refreshed) > 1:
                    for child in refreshed:
                        yield {**item, **child}
                    continue
            yield item


def build_keyword_catalog(index: Mapping[str, Any], rules: Mapping[str, Any] | None = None) -> dict:
    """Persist searchable, evidence-backed keywords for three attachment roles."""
    fields = {
        "nameplate": ("heat_lot_no", "grade", "diameter_mm", "weight_t"),
        "quality_certificate": ("certificate_no", "heat_lot_no", "grade", "diameter_mm", "weight_t"),
        "delivery_note": ("quantity_t", "diameter_mm", "grade", "supplier", "weight_t"),
    }
    items = []
    forms = {}
    required = {'nameplate': ('heat_lot_no',), 'quality_certificate': ('certificate_no', 'heat_lot_no'),
                'delivery_note': ('quantity_t',)}
    for region in _iter_regions(index, rules or index.get("rules")):
        role = region.get("document_type")
        if role not in fields or region.get("layout_role") in {"field_focus", "table_container", "page_evidence"}:
            continue
        form_key = (role, region.get('page_no'), region.get('entity_key', 'full'), region.get('row_id'))
        form = forms.setdefault(form_key, {'role': role, 'page_no': region.get('page_no'),
                                         'entity_key': region.get('entity_key', 'full'),
                                         'row_id': region.get('row_id'), 'region_ids': [], 'available_fields': []})
        form['region_ids'].append(region.get('region_id'))
        for field in fields[role]:
            for value in _field_values(region, field):
                if field not in form['available_fields']:
                    form['available_fields'].append(field)
                raw = _value(value); norm = _field_key(field, value)
                items.append({"role": role, "field": field, "raw_value": raw,
                              "ocr_raw_value": value.get("raw_value", raw),
                              "normalized_value": norm, "value_t": value.get("value_t"),
                              "unit": value.get("unit"), "page_no": region.get("page_no"),
                              "region_id": region.get("region_id"), "entity_key": region.get("entity_key", "full"),
                              "row_id": region.get("row_id"), "accepted": True,
                              "value_evidence": value.get("evidence"),
                              "value_evidence_refs": value.get("evidence_refs", []),
                              "evidence": _evidence(region, field=field, value=raw)})
    # Deduplicate repeated crops while preserving their evidence references.
    grouped = {}
    for item in items:
        key = tuple(item.get(k) for k in ("role", "field", "normalized_value", "page_no", "entity_key", "row_id"))
        if key not in grouped: grouped[key] = item
        else:
            grouped[key].setdefault("evidence_refs", []).append(item["evidence"])
            grouped[key]['value_evidence_refs'].extend(item['value_evidence_refs'])
    scopes = {}
    for item in grouped.values():
        key = tuple(item.get(k) for k in ("role", "field", "page_no", "entity_key", "row_id"))
        scopes.setdefault(key, set()).add(item.get("value_t") or item["normalized_value"])
    for item in grouped.values():
        key = tuple(item.get(k) for k in ("role", "field", "page_no", "entity_key", "row_id"))
        item['status'] = 'conflict' if len(scopes[key]) > 1 else 'format_accepted'
    for key, form in forms.items():
        form['missing_required_fields'] = [f for f in required[form['role']] if f not in form['available_fields']]
        form['conflicting_fields'] = sorted({item['field'] for item in grouped.values()
                                            if (item['role'], item['page_no'], item['entity_key'], item['row_id']) == key
                                            and item['status'] == 'conflict'})
        form['status'] = 'needs_review' if form['missing_required_fields'] or form['conflicting_fields'] else 'keys_available'
    rejected = [{"page_no": p.get("page_no"), "region_id": r.get("region_id"),
                 "entity_key": r.get("entity_key"), "observation": o}
                for p in index.get("pages", []) for r in p.get("regions", [])
                for o in r.get("field_observations", []) if o.get("validation_status") == "rejected"]
    return {"schema_version": "keyword-catalog-v1", "pdf_sha256": index.get("pdf_sha256"),
            "rejected_observations": rejected,
            "forms": list(forms.values()),
            "items": list(grouped.values()), "item_count": len(grouped)}


def _evidence(region: Mapping[str, Any], *, field: str | None = None,
              value: str | None = None) -> dict:
    result = {
        "pdf_sha256": region.get("pdf_sha256"),
        "page_no": region.get("page_no"),
        "region_id": region.get("region_id"),
        "polygon_page": region.get("polygon_page"),
        "raw_text": region.get("raw_text", region.get("text", "")),
        "ocr_score": region.get("ocr_score"),
        "rotation_ccw": region.get("rotation_ccw", 0),
        "source_image": region.get("source_image"),
        "extraction_methods": sorted({b.get("extraction_method") for b in region.get("blocks", [])
                                       if b.get("extraction_method")}),
        "blocks": [{"text": b.get("text"), "polygon_page": b.get("polygon_page"),
                    "score": b.get("score"), "extraction_method": b.get("extraction_method"),
                    "tile_path": b.get("tile_path")} for b in region.get("blocks", [])],
    }
    if field is not None:
        result["field"] = field
    if value is not None:
        result["raw_value"] = value
    return result


def _matching_fields(record: Mapping[str, Any], region: Mapping[str, Any], relation: str,
                     rules: Mapping[str, Any] | None = None) -> tuple[list[str], list[str], int]:
    """Return matched field names, conflicts and a deterministic score."""
    matched: list[str] = []
    conflicts: list[str] = []
    score = 0
    relation_cfg = merge_rules(rules).get("relations", {}).get(relation, {})
    weights = relation_cfg.get("weights", {})
    primary_fields = set(relation_cfg.get("primary_fields", []))
    def add(field: str, default: int) -> None:
        nonlocal score
        score += int(weights.get(field, default))

    record_cert = record.get("certificate_no")
    certs = _field_values(region, "certificate_no")
    if record_cert and certs:
        if any(_identifier_value(x) == normalize_identifier(record_cert) for x in certs):
            matched.append("certificate_no"); add("certificate_no", 100)
        else:
            conflicts.append("certificate_no_conflict")
    elif record_cert:
        # OCR may lose the label while retaining the complete identifier.
        # Require token boundaries and preserve O/0 and I/1 distinctions.
        if re.search(r"(?<![A-Za-z0-9_./-])" + re.escape(normalize_identifier(record_cert)) +
                     r"(?![A-Za-z0-9_./-])", normalize_text(region.get("text", region.get("raw_text", ""))).upper()):
            matched.append("certificate_no"); add("certificate_no", 100)

    record_weight = _number(record.get("weight_t"), default_unit="t")
    weight_items = _field_values(region, "quantity_t") if relation == "inspection_to_delivery_note" else _field_values(region, "weight_t")
    if record_weight is not None and weight_items:
        quantities = [_number(x.get("value_t"), default_unit="t") if x.get("value_t") is not None else parse_quantity_t(_value(x), default_unit=x.get("unit")) for x in weight_items]
        quantities = [x for x in quantities if x is not None]
        if any(x == record_weight for x in quantities):
            quantity_field = "quantity_t" if relation == "inspection_to_delivery_note" else "weight_t"
            matched.append(quantity_field); add(quantity_field, 45)
        elif quantities:
            conflicts.append("weight_or_quantity_conflict")

    diameter = _diameter(record.get("diameter_mm"))
    diameter_items = _field_values(region, "diameter_mm")
    if diameter is not None and diameter_items:
        numbers = [_diameter(_value(x)) for x in diameter_items]
        numbers = [x for x in numbers if x is not None]
        if any(x == diameter for x in numbers):
            matched.append("diameter_mm"); add("diameter_mm", 20)
        elif numbers:
            conflicts.append("diameter_conflict")

    manufacturer = record.get("manufacturer")
    mfrs = _field_values(region, "manufacturer")
    if manufacturer and mfrs:
        record_mfr, _ = normalize_similar_value(manufacturer, "manufacturer", rules)
        if any(_contains(record_mfr, normalize_similar_value(_value(x), "manufacturer", rules)[0]) for x in mfrs):
            matched.append("manufacturer"); add("manufacturer", 20)
        else:
            conflicts.append("manufacturer_conflict")

    prep = region.get('preprocessing') or {}
    if prep.get('orientation_review') or prep.get('review_reasons'):
        conflicts.append('image_quality_requires_review')
    if region.get('focused_ocr_review_reasons'):
        conflicts.append('focused_ocr_requires_review')
    if _region_type(region) == 'unknown':
        conflicts.append('document_role_unverified')
    if primary_fields and not primary_fields.intersection(matched):
        if relation == 'nameplate_to_quality_certificate' and record.get('heat_lot_no') in _heat_values(region):
            matched.append('heat_lot_no'); add('heat_lot_no', 100)
        else:
            conflicts.append("primary_key_missing")
    return matched, conflicts, score


def _candidate(region: Mapping[str, Any], matched: list[str], conflicts: list[str], score: int,
               relation: str) -> dict:
    for field in ("certificate_no", "heat_lot_no", "quantity_t", "diameter_mm", "grade", "manufacturer"):
        if len(_field_values(region, field)) > 1:
            conflicts = [*conflicts, f"{field}_multiple_values"]
    evidence = [_evidence(region, field=field) for field in matched] or [_evidence(region)]
    return {
        "relation": relation,
        "page_no": region.get("page_no"),
        "region_id": region.get("region_id"),
        "entity_key": region.get("entity_key"),
        "row_id": region.get("row_id"),
        "document_type": region.get("document_type", "unknown"),
        "score": score,
        "matched_fields": matched,
        "conflicts": conflicts,
        "status": "candidate",
        "evidence": evidence,
        "raw_text": region.get("raw_text", region.get("text", "")),
        "ocr_score": region.get("ocr_score"),
        "rotation_ccw": region.get("rotation_ccw", 0),
        "source_image": region.get("source_image"),
    }


def _rank(candidates: list[dict], relation: str,
          rules: Mapping[str, Any] | None = None) -> list[dict]:
    # Canonical layout IDs consolidate repeated full/entity OCR of the same
    # physical row; genuinely different rows/forms remain separate candidates.
    unique = {}
    for candidate in candidates:
        row = candidate.get("row_id")
        key = (candidate.get("page_no"), candidate.get("entity_key"), row) if row else (candidate.get("region_id"),)
        if key not in unique:
            unique[key] = candidate
        else:
            previous = unique[key]
            previous["conflicts"] = sorted(set(previous["conflicts"] + candidate["conflicts"]))
            previous["evidence"].extend(candidate["evidence"])
    candidates = list(unique.values())
    if not candidates:
        return []
    candidates.sort(key=lambda x: (-x["score"], x.get("page_no") or 0, x.get("region_id") or ""))
    top = candidates[0]["score"]
    relation_cfg = merge_rules(rules).get("relations", {}).get(relation, {})
    primary_fields = set(relation_cfg.get("primary_fields", []))
    exact_key = (relation in {"inspection_to_quality_certificate", "nameplate_to_quality_certificate"}
                 and ("certificate_no" in candidates[0]["matched_fields"] or
                      "heat_lot_no" in candidates[0]["matched_fields"]))
    if relation == "nameplate_to_quality_certificate":
        exact_key = "heat_lot_no" in candidates[0]["matched_fields"]
    min_score = int(relation_cfg.get("min_match_score", 45))
    viable = [c for c in candidates if not c['conflicts'] and
              (not primary_fields or primary_fields.intersection(c['matched_fields']))]
    if top <= 0:
        state = "unmatched"
    elif len(viable) > 1 or len([c for c in candidates if c["score"] == top]) > 1:
        state = "ambiguous"
    elif (not primary_fields or primary_fields.intersection(candidates[0].get("matched_fields", []))) and (exact_key or top >= min_score):
        state = "matched"
    else:
        state = "needs_review"
    for candidate in candidates:
        # Every conflict blocks promotion, even with an exact primary key.
        if candidate.get("conflicts"):
            candidate["status"] = "needs_review" if candidate["score"] == top else "candidate"
        else:
            candidate["status"] = state if candidate["score"] == top else "candidate"
    return candidates


def _match_record_relation(record: Mapping[str, Any], regions: list[dict], relation: str,
                           allowed_types: set[str], rules: Mapping[str, Any] | None = None) -> list[dict]:
    candidates = []
    for region in regions:
        if region.get("match_eligible") is False:
            continue
        if _region_type(region) not in allowed_types and _region_type(region) != "unknown":
            continue
        matched, conflicts, score = _matching_fields(record, region, relation, rules)
        # For a certificate/quantity relation, only emit a region with actual
        # evidence.  This avoids returning every page in a 100-page PDF.
        if score > 0:
            candidates.append(_candidate(region, matched, conflicts, score, relation))
    return _rank(candidates, relation, rules)


def _heat_values(region: Mapping[str, Any]) -> list[str]:
    return [_identifier_value(item) for item in _field_values(region, "heat_lot_no") if _value(item)]


def _match_nameplates(regions: list[dict]) -> list[dict]:
    plates = [r for r in regions if r.get("match_eligible") is not False and _region_type(r) == "nameplate" and _heat_values(r)]
    certs = [r for r in regions if r.get("match_eligible") is not False and _region_type(r) == "quality_certificate" and _heat_values(r)]
    results = []
    for plate in plates:
        for cert in certs:
            shared = sorted(set(_heat_values(plate)) & set(_heat_values(cert)))
            if not shared:
                continue
            _, cross_conflicts, _ = _matching_fields(
                {"diameter_mm": (_best_field(plate, ("diameter_mm",)) or {}).get("value"),
                 "manufacturer": (_best_field(plate, ("manufacturer",)) or {}).get("value"),
                 "heat_lot_no": shared[0]},
                cert, "nameplate_to_quality_certificate")
            for region in (plate, cert):
                for field in ('heat_lot_no', 'diameter_mm', 'grade', 'manufacturer'):
                    if len(_field_values(region, field)) > 1:
                        cross_conflicts.append(f'{field}_multiple_values')
            candidate = {
                "relation": "nameplate_to_quality_certificate",
                "page_no": plate.get("page_no"),
                "region_id": plate.get("region_id"),
                "certificate_page_no": cert.get("page_no"),
                "certificate_region_id": cert.get("region_id"),
                "score": 100,
                "matched_fields": ["heat_lot_no"],
                "matched_values": shared,
                "conflicts": cross_conflicts,
                "status": "candidate",
                "evidence": [_evidence(plate, field="heat_lot_no"), _evidence(cert, field="heat_lot_no")],
            }
            results.append(candidate)
    if not results:
        return []
    # Same heat lot may legitimately appear on multiple delivery pages.  Mark
    # all equal top matches ambiguous instead of hiding shared attachments.
    by_plate: dict[str, list[dict]] = {}
    for item in results:
        by_plate.setdefault(str(item["region_id"]), []).append(item)
    for values in by_plate.values():
        state = "matched" if len(values) == 1 else "ambiguous"
        for value in values:
            value["status"] = "needs_review" if value.get("conflicts") else state
    return sorted(results, key=lambda x: (x.get("page_no") or 0, x.get("certificate_page_no") or 0))


def _match_approvals(regions: list[dict]) -> list[dict]:
    # Date is explicitly a weak/non-unique key.  Require both document types
    # to be known before emitting a relation; an ``unknown`` page remains in
    # the index for manual search but must not create a false auto-fill value.
    approvals = [r for r in regions if _region_type(r) == "approval_form" and _field_values(r, "inspection_date")]
    inspections = [r for r in regions if _region_type(r) == "inspection_record" and _field_values(r, "inspection_date")]
    results = []
    for approval in approvals:
        for inspection in inspections:
            a_dates = {_date(_value(x)) for x in _field_values(approval, "inspection_date")}
            i_dates = {_date(_value(x)) for x in _field_values(inspection, "inspection_date")}
            shared = sorted(x for x in a_dates & i_dates if x)
            if not shared:
                continue
            results.append({
                "relation": "approval_to_inspection_record",
                "page_no": approval.get("page_no"),
                "region_id": approval.get("region_id"),
                "inspection_page_no": inspection.get("page_no"),
                "inspection_region_id": inspection.get("region_id"),
                "score": 10,
                "matched_fields": ["inspection_date"],
                "matched_values": shared,
                "conflicts": ["inspection_date_is_not_unique"],
                "status": "ambiguous",
                "evidence": [_evidence(approval, field="inspection_date"), _evidence(inspection, field="inspection_date")],
            })
    return results


def _best_field(region: Mapping[str, Any], fields: tuple[str, ...]) -> dict | None:
    for field in fields:
        values = _field_values(region, field)
        if len({_value(v) for v in values}) > 1:
            return None
        if values:
            item = values[0]
            return {"value": _value(item), "raw_value": _value(item),
                    "source": {**_evidence(region, field=field, value=_value(item)),
                               **item.get("evidence", {})}, "candidate": item}
    return None


def _record_evidence(record: Mapping[str, Any]) -> dict:
    return {"record_id": record.get("record_id"), "page_no": record.get("page_no"),
            "source_result": record.get("source_result"), "source_cells": record.get("source_cells")}


def _field(value: str | None, status: str, *, source: dict | None = None,
           relation: str | None = None, conflicts: list[str] | None = None) -> dict:
    return {"value": value, "status": status, "source": source,
            "relation": relation, "conflicts": conflicts or []}


def _safe_fill(record, quality, delivery, plate, approval):
    """Validate each field independently of the attachment's relation status."""
    def selected(region, field, relation, status='matched'):
        if not region:
            return _field(None, 'missing', relation=relation)
        values = _field_values(region, field)
        best = _best_field(region, (field,))
        if region.get('rejected_fields', {}).get(field):
            return _field(None, 'needs_review', relation=relation, conflicts=['invalid_field_candidates'])
        if not best:
            denied = region.get('rejected_fields', {}).get(field)
            reason = 'multiple_field_values' if values else 'invalid_field_value' if denied else 'missing_field'
            return _field(None, 'needs_review' if values or denied else 'missing', relation=relation, conflicts=[reason])
        value = best['value']
        if field == 'quantity_t':
            value = best['candidate'].get('value_t')
        return _field(value, status, source=best['source'], relation=relation)

    def entry(field):
        return _field(record.get(field), 'from_entry_record' if record.get(field) else 'missing',
                      source=_record_evidence(record), relation='entry_record')

    result = {
        'material_name': entry('material_name') if record.get('material_name') else selected(approval, 'material_name', 'approval_to_inspection_record', 'needs_review'),
        'production_unit': selected(quality, 'manufacturer', 'inspection_to_quality_certificate') if quality else entry('manufacturer'),
        'supplier': selected(delivery, 'supplier', 'inspection_to_delivery_note'),
        'supply_date': selected(delivery or quality, 'supply_date', 'inspection_to_delivery_note' if delivery else 'inspection_to_quality_certificate'),
        'usage_location': selected(approval, 'usage_location', 'approval_to_inspection_record', 'needs_review'),
        'heat_lot_no': selected(plate or quality, 'heat_lot_no', 'nameplate_to_quality_certificate', 'matched' if plate else 'needs_review'),
        'arrival_quantity': selected(delivery, 'quantity_t', 'inspection_to_delivery_note'),
        'certificate_no': entry('certificate_no') if record.get('certificate_no') else selected(quality, 'certificate_no', 'inspection_to_quality_certificate'),
    }
    result['arrival_quantity']['unit'] = 't'
    grade = selected(quality, 'grade', 'inspection_to_quality_certificate')
    diameter = selected(quality, 'diameter_mm', 'inspection_to_quality_certificate')
    if grade['status'] == diameter['status'] == 'matched':
        value = f"{compact_text(grade['value']).upper()} Φ{_diameter(diameter['value'])}mm"
        result['specification'] = _field(value, 'matched', source={'grade':grade['source'], 'diameter_mm':diameter['source']}, relation='inspection_to_quality_certificate')
    else:
        result['specification'] = _field(f"{record['diameter_mm']}mm" if record.get('diameter_mm') else None,
                                         'needs_review', source=_record_evidence(record),
                                         conflicts=['grade_and_diameter_required'], relation='inspection_to_quality_certificate')
    return result


def search_attachments(index, query, rules=None):
    """Keyword recall for a frontend; hits never constitute a confirmed join."""
    terms = [compact_text(x).upper() for x in re.split(r"[\s,，;；]+", query) if compact_text(x)]
    if not terms:
        return []
    hits = []
    for region in _iter_regions(index, rules or index.get('rules')):
        keywords = [str(x.get('value_t') or x.get('value') or '')
                    for values in region.get('extracted_fields', {}).values() for x in values]
        text = compact_text(region.get('text', '') + ' ' + ' '.join(keywords)).upper()
        found = [term for term in terms if term in text]
        if found:
            hits.append({'page_no':region['page_no'], 'region_id':region['region_id'],
                         'document_type':region['document_type'], 'keywords':found,
                         'status':'recall_only', 'score':len(found), 'evidence':_evidence(region)})
    return sorted(hits, key=lambda h: (-h['score'], h['page_no'], h['region_id']))


def _association_group(record: Mapping[str, Any], relations: Mapping[str, Any],
                       auto_fill: Mapping[str, Any], status: str) -> dict:
    """Package one entry record with its five-form candidate set.

    The group is an indexable unit for later keyword search and filling.  It
    intentionally keeps candidate arrays instead of collapsing ambiguous
    attachments into one page.
    """
    quality = relations.get("inspection_to_quality_certificate", [])
    delivery = relations.get("inspection_to_delivery_note", [])
    approval = relations.get("approval_to_inspection_record", [])
    plates = relations.get("nameplate_to_quality_certificate", [])
    def pages(values: list[dict]) -> list[int]:
        output = []
        for value in values:
            for key in ("page_no",):
                if value.get(key) is not None and value[key] not in output:
                    output.append(value[key])
        return sorted(output)
    def refs(values):
        return [{"page_no": v.get("page_no"), "region_id": v.get("region_id"),
                 "status": v.get("status"), "matched_fields": v.get("matched_fields", []),
                 "certificate_region_id": v.get("certificate_region_id"),
                 "inspection_region_id": v.get("inspection_region_id")} for v in values]
    edge_keys = {'inspection_to_quality_certificate': 'certificate_no',
                 'inspection_to_delivery_note': 'quantity_t',
                 'nameplate_to_quality_certificate': 'heat_lot_no',
                 'approval_to_inspection_record': 'inspection_date'}
    return {
        "group_id": str(record.get("record_id")),
        "record_id": record.get("record_id"),
        "join_keys": {
            "certificate_no": record.get("certificate_no"),
            "arrival_quantity_t": record.get("weight_t"),
            "inspection_date": record.get("inspection_date"),
            "heat_lot_no": sorted({heat for plate in plates for heat in plate.get('matched_values', [])}),
        },
        "forms": {
            "inspection_record": {"page_no": record.get("page_no"), "record_id": record.get("record_id")},
            "quality_certificate": {"candidates": quality, "pages": pages(quality), "refs": refs(quality)},
            "delivery_note": {"candidates": delivery, "pages": pages(delivery), "refs": refs(delivery)},
            "approval_form": {"candidates": approval, "pages": pages(approval), "refs": refs(approval),
                              "extract_fields": ["usage_location", "material_name"]},
            "nameplate": {"candidates": plates, "pages": pages(plates), "refs": refs(plates)},
        },
        "auto_fill": dict(auto_fill),
        "status": status,
        "missing_roles": [role for role, values in (("quality_certificate", quality), ("delivery_note", delivery),
                                                    ("nameplate", plates), ("approval_form", approval)) if not values],
        "relation_edges": [{"relation": key, "join_field": edge_keys[key],
                            "candidate_count": len(value), "candidate_refs": refs(value)} for key, value in relations.items()],
        "search_text": " ".join(str(x or "") for x in (
            record.get("record_id"), record.get("certificate_no"), record.get("material_name"),
            record.get("manufacturer"), record.get("diameter_mm"), record.get("weight_t"))),
    }


def match_forms(records: Mapping[str, Any], index: Mapping[str, Any],
                rules: Mapping[str, Any] | None = None) -> dict:
    """Match all first-stage records and generate structured auto-fill fields."""
    if records.get('pdf_sha256') and index.get('pdf_sha256') and records['pdf_sha256'] != index['pdf_sha256']:
        raise ValueError('记录与附件 PDF SHA256 不一致，拒绝跨文件自动关联')
    effective_rules = merge_rules(rules or index.get("rules"))
    regions = list(_iter_regions(index, effective_rules))
    canonical = {}
    for r in regions:
        key = (r.get('page_no'), r.get('entity_key'), r.get('row_id'), r.get('document_type'))
        if not r.get('entity_key') or r.get('layout_role') in {'field_focus', 'table_container'}:
            key = (r.get('region_id'),)
        if key not in canonical:
            canonical[key] = r
        else:
            previous = canonical[key]
            for field, values in r.get('extracted_fields', {}).items():
                existing = previous['extracted_fields'].setdefault(field, [])
                seen = {_field_key(field, v) for v in existing}
                existing.extend(v for v in values if _field_key(field, v) not in seen)
    regions = list(canonical.values())
    source_records = records.get("records", []) if isinstance(records, Mapping) else []
    nameplate_relations = _match_nameplates(regions)
    approval_relations = _match_approvals(regions)
    entries = []
    for record in source_records:
        quality_cfg = effective_rules.get("relations", {}).get("inspection_to_quality_certificate", {})
        delivery_cfg = effective_rules.get("relations", {}).get("inspection_to_delivery_note", {})
        quality_types = set(quality_cfg.get("allowed_types", ["quality_certificate"]))
        delivery_types = set(delivery_cfg.get("allowed_types", ["delivery_note"]))
        quality = _match_record_relation(record, regions, "inspection_to_quality_certificate", quality_types, effective_rules)
        delivery = _match_record_relation(record, regions, "inspection_to_delivery_note", delivery_types, effective_rules)
        scoped_approvals = [x for x in approval_relations if x.get("inspection_page_no") == record.get("page_no")]
        scoped_plates = [x for x in nameplate_relations
                         if x.get("certificate_region_id") in {c['region_id'] for c in quality}]
        relations = {
            "inspection_to_quality_certificate": quality,
            "inspection_to_delivery_note": delivery,
            "nameplate_to_quality_certificate": scoped_plates,
            "approval_to_inspection_record": scoped_approvals,
        }
        quality_top = quality[0] if quality and quality[0].get("status") == "matched" else None
        delivery_top = delivery[0] if delivery and delivery[0].get("status") == "matched" else None
        by_id = {str(r.get("region_id")): r for r in regions}
        quality_region = by_id.get(str(quality_top.get("region_id"))) if quality_top else None
        delivery_region = by_id.get(str(delivery_top.get("region_id"))) if delivery_top else None
        plate_matches = [x for x in scoped_plates if quality_top and
                         x.get("certificate_region_id") == quality_top.get("region_id")]
        plate_region = by_id.get(str(plate_matches[0]['region_id'])) if len(plate_matches) == 1 and plate_matches[0]['status'] == 'matched' else None
        approval_region = by_id.get(str(scoped_approvals[0]['region_id'])) if len(scoped_approvals) == 1 else None
        conflicts = []
        for candidate_list in (quality, delivery):
            if candidate_list:
                conflicts.extend(candidate_list[0].get('conflicts', []))
                if candidate_list[0]['status'] == 'ambiguous':
                    conflicts.append('multiple_attachment_candidates')
        if len(scoped_approvals) > 1:
            conflicts.append('multiple_approval_candidates')
        if len(plate_matches) > 1:
            conflicts.append('multiple_nameplate_candidates')
        auto_fill = _safe_fill(record, quality_region, delivery_region, plate_region, approval_region)
        has_review_candidate = any(
            candidate.get("status") in {"needs_review", "ambiguous", "matched"}
            for candidate_list in (quality, delivery)
            for candidate in candidate_list
        )
        status = "matched" if quality_top and delivery_top else ("needs_review" if has_review_candidate else "unmatched")
        if conflicts or any(x["status"] in {"needs_review", "ambiguous", "missing"} for x in auto_fill.values()):
            status = "needs_review"
        review_reasons = sorted(set(conflicts + [f"{key}:{value['status']}" for key,value in auto_fill.items()
                                                 if value['status'] != 'matched']))
        if not record.get('ready_for_search', records.get('ready_for_search', False)):
            review_reasons.append('first_stage_requires_human_release')
        if any(p.get('ocr_status') == 'failed' for p in index.get('pages', [])):
            review_reasons.append('attachment_ocr_failed')
        if any(candidate.get('rejected_fields') for candidate in (quality_region, delivery_region, plate_region) if candidate):
            review_reasons.append('rejected_ocr_field_candidates')
        entry = {"record_id": record.get("record_id"), "record": record,
                        "status": status, "conflicts": sorted(set(conflicts)),
                        "relations": relations, "auto_fill": auto_fill,
                        "workflow": {"status": "pending_human_review" if review_reasons else "ready_to_fill",
                                     "review_reasons": sorted(set(review_reasons)),
                                     "write_allowed": not review_reasons},
                        "fill_payload": {k:v['value'] for k,v in auto_fill.items()} if not review_reasons else {}}
        entry["association_group"] = _association_group(record, relations, auto_fill, status)
        entry["association_group"]["workflow"] = entry["workflow"]
        entries.append(entry)
    counts = {"matched": sum(x["status"] == "matched" for x in entries),
              "needs_review": sum(x["status"] == "needs_review" for x in entries),
              "unmatched": sum(x["status"] == "unmatched" for x in entries)}
    result = {
        "schema_version": "phase2-result-v1",
        "source_records_schema_version": records.get("schema_version") if isinstance(records, Mapping) else None,
        "attachment_index_schema_version": index.get("schema_version"),
        "pdf_sha256": index.get("pdf_sha256") or records.get("pdf_sha256"),
        "ready_for_search": False,
        "entries": entries,
        "review_pages": [{'page_no':p.get('page_no'), 'status':'pending_human_review',
                          'reason':'attachment_ocr_failed', 'error_type':p.get('error_type')}
                         for p in index.get('pages', []) if p.get('ocr_status') == 'failed'],
        "first_stage_review_pages": records.get('review_pages', []),
        "review_queue": [{"record_id":e['record_id'], "status":"pending",
                          "reasons":e['workflow']['review_reasons'],
                          "source":_record_evidence(e['record'])} for e in entries if not e['workflow']['write_allowed']],
        "association_groups": [entry["association_group"] for entry in entries],
        "global_relations": {"nameplate_to_quality_certificate": nameplate_relations,
                             "approval_to_inspection_record": approval_relations},
        "summary": {"record_count": len(entries), "status_counts": counts,
                    "attachment_page_count": len(index.get("pages", [])),
                    "region_count": len(regions),
                    "nameplate_certificate_relation_count": len(nameplate_relations),
                    "approval_inspection_relation_count": len(approval_relations),
                    "field_candidate_counts": {
                        field: sum(len(r.get("extracted_fields", {}).get(field, [])) for r in regions)
                        for field in ("certificate_no", "heat_lot_no", "quantity_t", "weight_t", "diameter_mm", "manufacturer", "supply_date")
                    },
                    "field_rejected_counts": {
                        field: sum(len(r.get("rejected_fields", {}).get(field, [])) for r in regions)
                        for field in ("certificate_no", "heat_lot_no", "quantity_t", "weight_t", "diameter_mm", "manufacturer", "supply_date")
                    }},
        "notes": ["candidate_results_require_human_review", "shared_delivery_pages_are_not_deduplicated",
                  "date_is_weak_and_non_unique", "O_and_0_are_not_substituted"],
    }
    result["keyword_catalog"] = build_keyword_catalog(index, effective_rules)
    return result


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records")
    parser.add_argument("index")
    parser.add_argument("out")
    args = parser.parse_args()
    records = json.loads(Path(args.records).read_text(encoding="utf-8"))
    index = json.loads(Path(args.index).read_text(encoding="utf-8"))
    Path(args.out).write_text(json.dumps(match_forms(records, index), ensure_ascii=False, indent=2), encoding="utf-8")
