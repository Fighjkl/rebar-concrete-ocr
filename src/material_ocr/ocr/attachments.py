"""Build a traceable OCR index for every page in a scanned PDF.

The first stage deliberately routes only its supported entry-table pages.  This
module scans *all* pages, including ``other`` and ``review`` pages, because the
second stage needs certificates, delivery notes, nameplates and approval forms.
OCR is optional at import time; the deterministic schema helpers are usable in
CPU tests without PaddleOCR or a PDF renderer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from material_ocr.ocr.layout import assess_image, split_entities, table_rows, orientation_score, priority_field_templates
import unicodedata
from decimal import Decimal, InvalidOperation
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping
from material_ocr.storage_artifacts import register_derivative


SCHEMA_VERSION = "phase2-attachment-index-v1"

DEFAULT_RULES = {
    "document_aliases": {},
    "field_aliases": {},
    "relations": {},
    "fragmented_ocr": {"enabled": True, "tile_rows": 3, "tile_cols": 2, "upscale": 1.6, "retry_mean_score": 0.75},
    "region_templates": {},
    "similar_words": {},
    "ocr_runtime": {"profile": "complete", "recognition_batch_size": None},
}

FIELD_ALIASES = {
    "certificate_no": ("质量证明书编号", "质保证书编号", "质保书编号", "证书编号", "质编号", "合格证号", "合格证编号", "CERTIFICATE NO", "CERTIFICATE", "QUALITY CERTIFICATE NO"),
    "heat_lot_no": ("炉批号", "炉号", "炉批", "批号", "批次号", "炉批", "BATCH NO", "BATCHNO", "HEAT NO", "HEATNO", "轧制号"),
    "weight_t": ("总重量", "重量", "净重", "毛重", "总理重", "理重", "WEIGHT", "TOTAL WEIGHT", "PIECES/Wt", "支数/重量"),
    "quantity_t": ("进场数量", "验收数量", "供货数量", "到货数量", "数量", "重量（吨）", "重量(吨)", "签收数量", "WEIGHT (T)", "QUANTITY"),
    "diameter_mm": ("钢筋规格", "规格", "直径", "公称直径", "规格/材质", "SIZE", "DIMENSION"),
    "grade": ("钢筋牌号", "牌号", "级别", "品种规格", "GRADE", "STEEL GRADE"),
    "manufacturer": ("生产厂家", "生产单位", "生产厂", "制造厂", "厂家", "产地", "MANUFACTURER", "FACTORY", "PRODUCER"),
    "supplier": ("供应单位", "供货单位", "送货单位", "提货单位", "供应商"),
    "supply_date": ("发货日期", "供货日期", "送货日期", "发货时间", "DATE OF ISSUE", "DATEOFISSUE", "DATE OF DELIVERY", "DATEOFDELIV"),
    "inspection_date": ("检验日期", "验收日期", "进场日期"),
    "material_name": ("材料名称", "材料名", "品名", "货物名称"),
    "usage_location": ("使用部位", "使用位置", "施工部位", "部位"),
    "project_name": ("工程名称", "项目名称"),
}

DOCUMENT_ALIASES = {
    "quality_certificate": ("质量证明书", "质保证书", "质保书", "材质证明", "产品合格证", "QUALITY CERTIFICATE", "CERTIFICATE", "CERTIFICATE NO", "DATE OF ISSUE", "BATCH NO", "HEAT NO"),
    "delivery_note": ("送货单", "发货单", "供货单", "送货通知"),
    "nameplate": ("材料名牌", "材料铭牌", "铭牌", "炉批号", "炉号", "BATCH NO", "BATCHNO", "HEAT NO", "SIZE", "PIECES/Wt"),
    "inspection_record": ("材料构配件进场检验记录", "进场检验记录", "检验记录"),
    "approval_form": ("工程材料、构配件、设备报审表", "工程材料构配件设备报审表", "材料报审表", "报审表"),
}


def merge_rules(rules: Mapping[str, Any] | None = None) -> dict:
    """Merge user rules with safe defaults without mutating module constants."""
    import copy
    result = copy.deepcopy(DEFAULT_RULES)
    result["document_aliases"] = copy.deepcopy(DOCUMENT_ALIASES)
    result["field_aliases"] = copy.deepcopy(FIELD_ALIASES)
    if rules:
        for section in ("document_aliases", "field_aliases", "relations", "fragmented_ocr", "region_templates", "similar_words", "ocr_runtime"):
            if isinstance(rules.get(section), Mapping):
                if section in ("relations", "fragmented_ocr", "region_templates", "similar_words", "ocr_runtime"):
                    result[section].update(copy.deepcopy(rules[section]))
                else:
                    result[section].update({k: list(v) for k, v in rules[section].items()})
    return result


def load_rules(path: str | Path | None = None) -> dict:
    if path is None:
        return merge_rules()
    return merge_rules(json.loads(Path(path).read_text(encoding="utf-8")))


def normalize_text(value: Any) -> str:
    """NFKC-normalise text while retaining all characters and their meaning."""
    return unicodedata.normalize("NFKC", str(value or ""))


def compact_text(value: Any) -> str:
    return re.sub(r"\s+", "", normalize_text(value))


def compact_label(value: Any) -> str:
    """Normalize punctuation/spacing for OCR label comparisons."""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", normalize_text(value)).upper()


def normalize_identifier(value: Any) -> str:
    """Normalise spacing/case only; deliberately never maps O↔0 or I↔1."""
    return re.sub(r"\s+", "", normalize_text(value)).upper()


def normalize_similar_value(value: Any, field: str, rules: Mapping[str, Any] | None = None) -> tuple[str, str | None]:
    """Return a compact comparison value and optional configured canonical value."""
    compact = compact_text(value)
    mapping = merge_rules(rules).get("similar_words", {}).get(field, {})
    if isinstance(mapping, list):
        mapping = {item: item for item in mapping}
    for canonical, variants in mapping.items():
        values = variants if isinstance(variants, list) else [variants]
        if compact == compact_text(canonical) or any(compact == compact_text(v) for v in values):
            return compact_text(canonical), str(canonical)
    return compact, None


def parse_quantity_t(value: Any, default_unit: str | None = None) -> Decimal | None:
    """Parse a mass/quantity into tonnes using Decimal.

    ``default_unit`` is used only when OCR text has no explicit unit.  A bare
    number is otherwise left unknown so a count cannot silently become a mass.
    """
    text = compact_text(value)
    if not text:
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(吨|t|T|kg|KG|千克|公斤)?", text)
    if not match:
        return None
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation:
        return None
    unit = match.group(2) or default_unit
    if unit in ("kg", "KG", "千克", "公斤"):
        amount /= Decimal(1000)
    elif unit in ("吨", "t", "T"):
        pass
    else:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def classify_page_text(text: str, hint: str | None = None, rules: Mapping[str, Any] | None = None) -> str:
    """Classify a page conservatively from OCR text and optional first-stage hint."""
    configured = merge_rules(rules)["document_aliases"]
    if hint in configured:
        return hint
    compact = compact_text(text).upper()
    if '送货单' in compact and ('签收数量' in compact or '规格/材质' in compact):
        return 'delivery_note'
    # Carbon-copy delivery notes often have no readable title: the OCR only
    # retains the four copy labels (stub/customer/pick-up/settlement).
    if "存根" in compact and ("提货" in compact or "结算" in compact):
        return "delivery_note"
    # English-only quality certificates are common in manufacturer forms.
    # Require several certificate-specific anchors so a delivery note carrying
    # a generic `CERTIFICATE` word is not misclassified.
    cert_hits = sum(token in compact for token in ("QUALITYCERTIFICATE", "LICENSENO", "DATEOFISSUE", "BATCHNO", "HEATNO", "DELIVERYSTATE"))
    if cert_hits >= 2:
        return "quality_certificate"
    plate_hits = sum(token in compact for token in ("BATCHNO", "HEATNO", "PIECES/WT", "SIZE", "GRADE", "STANDARD"))
    if plate_hits >= 3 and "QUALITYCERTIFICATE" not in compact:
        return "nameplate"
    for page_type, aliases in configured.items():
        if any(normalize_text(alias).upper() in compact for alias in aliases):
            return page_type
    return "unknown"


def _value_after_label(text: str, label: str, all_labels: Iterable[str]) -> str:
    """Extract one labelled value, stopping at the next known label/newline."""
    source = normalize_text(text)
    start = source.find(label)
    if start < 0:
        return ""
    tail = source[start + len(label):]
    tail = re.sub(r"^[\s:：=#号编号]+", "", tail)
    stops = [r"[\r\n]", r"[;；]", r"[,，]"]
    for other in all_labels:
        if other == label:
            continue
        # A value may itself start with a label-like word (for example
        # “供应商B” after “送货单位”).  It is the value when it begins at
        # offset zero; only later occurrences can delimit the next field.
        if tail.startswith(other):
            continue
        stops.append(re.escape(other))
    m = re.search("(?:" + "|".join(stops) + ")", tail)
    if m:
        tail = tail[:m.start()]
    return tail.strip(" \t:：=#-_/|（）()[]【】")


def _mass_unit(label: str) -> str | None:
    text = compact_text(label).lower()
    if re.search(r"kg|千克|公斤", text):
        return "kg"
    if re.search(r"吨|(?:\(|（)t(?:\)|）)|weightt$", text):
        return "t"
    return None


def _field_candidate(field: str, value: str, evidence: Mapping[str, Any] | None) -> dict:
    item = {
        "field": field,
        "value": value,
        "raw_value": value,
        "normalized_value": normalize_identifier(value) if field in {"certificate_no", "heat_lot_no"} else compact_text(value),
    }
    if field in {"weight_t", "quantity_t"}:
        # The table/header parser may provide the unit; preserve it so a
        # bare value is never confused with piece count or unit weight.
        unit = (evidence or {}).get("column_unit")
        parsed = parse_quantity_t(value, default_unit=unit)
        item["value_t"] = format(parsed, "f") if parsed is not None else None
        item["unit"] = unit
    if evidence:
        item["evidence"] = dict(evidence)
    return item


def _clean_spatial_value(field: str, value: str) -> str:
    """Extract a compact value from a neighbouring OCR token.

    Spatial OCR frequently returns a label and its value as one token.  Only
    format-constrained fields are cleaned here; free-text fields retain the
    original evidence for review.
    """
    text = normalize_text(value).strip()
    compact = compact_text(text)
    if field == "certificate_no":
        matches = re.findall(r"(?<![A-Z0-9])(?:[A-Z]{1,4}[-/]?\d{6,}|\d{6,})(?![A-Z0-9])", compact.upper())
        return max(matches, key=len) if matches else ""
    if field == "heat_lot_no":
        matches = re.findall(r"(?<![A-Z0-9])(?:[A-Z]{1,5}[-/]?\d{6,})(?![A-Z0-9])", compact.upper())
        return max(matches, key=len) if matches else ""
    if field in {"weight_t", "quantity_t"}:
        match = re.search(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*(?:吨|t|T|kg|KG|千克)?", text)
        return match.group(0) if match else ""
    if field in {"supply_date", "inspection_date"}:
        match = re.search(r"20\d{2}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}(?:\s*日)?", text)
        return match.group(0) if match else ""
    if field == "grade":
        match = re.search(r"(?:HRB|HRBF|HPB|RRB|CRB)\s*\d{3,4}(?:E|B)?", text, re.I)
        return match.group(0) if match else ""
    return text


def extract_attachment_fields(text: str, evidence: Mapping[str, Any] | None = None,
                              rules: Mapping[str, Any] | None = None) -> dict[str, list[dict]]:
    """Extract labelled field candidates without discarding the source text.

    OCR layouts vary considerably, therefore this function emits a list per
    field.  A later matching decision may choose one candidate or mark the
    field ambiguous; parsing itself never invents a value.
    """
    source = normalize_text(text)
    configured = merge_rules(rules)["field_aliases"]
    labels = [label for aliases in configured.values() for label in aliases]
    fields: dict[str, list[dict]] = {field: [] for field in configured}
    # A scan frequently emits one Chinese label as several OCR blocks, e.g.
    # ``规`` + ``格``.  Compact text is a recovery candidate only; the raw
    # source remains the primary evidence and the extraction method is marked.
    variants = [(source, "labelled")]
    compact_source = compact_text(source)
    if compact_source != source:
        variants.append((compact_source, "fragmented_compact"))
    for field, aliases in configured.items():
        # Longest alias first prevents “数量” from consuming “进场数量”.
        for variant, method in variants:
            for label in sorted(aliases, key=len, reverse=True):
                value = _value_after_label(variant, label, labels)
                if not value:
                    continue
                # A label may occur once in a combined line; keep the first
                # occurrence per exact value and preserve a separate alias hit.
                if not any(x["value"] == value for x in fields[field]):
                    label_unit = _mass_unit(label)
                    item = _field_candidate(field, value, {**(evidence or {}), "label": label,
                                           "column_unit": label_unit})
                    if method != "labelled":
                        item["extraction_method"] = method
                    fields[field].append(item)
                break
            if fields[field]:
                break
    # Nameplate/delivery scans often split a label into separate OCR blocks
    # (``规`` + ``格``) or omit the label entirely.  Add explicitly marked
    # heuristic candidates for common steel evidence; they remain candidates
    # and are never treated as a confirmed value by themselves.
    compact = compact_text(source)
    if not fields.get("diameter_mm"):
        for match in re.finditer(r"[ΦφØ][^0-9]{0,100}([0-9]{1,2}(?:\.[0-9]+)?)", compact):
            fields.setdefault("diameter_mm", []).append(
                dict(_field_candidate("diameter_mm", match.group(1), evidence), extraction_method="heuristic_symbol"))
            break
    if not fields.get("manufacturer") and not fields.get("supplier"):
        companies = re.findall(r"([\u4e00-\u9fff]{2,30}(?:钢铁|特钢|集团有限公司|有限公司))", source)
        if companies:
            fields.setdefault("manufacturer", []).append(
                dict(_field_candidate("manufacturer", companies[0], evidence), extraction_method="heuristic_company"))
    # Approval forms often express these values in prose rather than labels:
    # ``于 2026年7月29日进场的拟用于工程地下室部位的钢筋``.
    if not fields.get("inspection_date"):
        match = re.search(r"于\s*(20\d{2}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?)", source)
        if match:
            fields["inspection_date"] = [_field_candidate("inspection_date", match.group(1), evidence)]
    if not fields.get("usage_location") or all(str(item.get("value", "")).strip().startswith("的") for item in fields.get("usage_location", [])):
        match = re.search(r"用于工程\s*(.+?)\s*部位", source, re.S)
        if match:
            value = match.group(1).strip(" \t\r\n:：")
            value = re.sub(r"(?:的|部位)$", "", value).strip()
            if value:
                fields["usage_location"] = [_field_candidate("usage_location", value, evidence)]
    if not fields.get("material_name"):
        match = re.search(r"部位的\s*([\u4e00-\u9fffA-Za-z0-9（）()]{2,30})", source)
        if match:
            fields["material_name"] = [_field_candidate("material_name", match.group(1), evidence)]
    # Bare identifiers are useful only when the OCR text has a clear label.
    return {key: value for key, value in fields.items() if value}


def _line_grouped_text(blocks: Iterable[Mapping[str, Any]]) -> str:
    """Join OCR blocks by spatial lines before applying field labels."""
    rows: list[tuple[float, list[tuple[float, str]]]] = []
    for block in blocks:
        text = normalize_text(block.get("text", "")).strip()
        poly = block.get("polygon_ocr", block.get("polygon_page"))
        if not text or not poly:
            continue
        try:
            xs = [float(p[0]) for p in poly]; ys = [float(p[1]) for p in poly]
            x, y, h = min(xs), sum(ys) / len(ys), max(ys) - min(ys)
        except (TypeError, ValueError, IndexError):
            continue
        for idx, (row_y, items) in enumerate(rows):
            if abs(y - row_y) <= max(12.0, h * 0.7):
                items.append((x, text)); rows[idx] = ((row_y + y) / 2.0, items); break
        else:
            rows.append((y, [(x, text)]))
    rows.sort(key=lambda item: item[0])
    return "\n".join("".join(text for _, text in sorted(items)) for _, items in rows)


def _spatial_label_fields(blocks: Iterable[Mapping[str, Any]],
                          rules: Mapping[str, Any] | None = None) -> dict[str, list[dict]]:
    """Recover values printed next to a label when OCR split the table.

    PaddleOCR returns one block per label/value in many rotated certificates.
    We retain the upright polygon as ``polygon_ocr`` and look for the nearest
    non-label block to the right or below the label.  This is deliberately a
    candidate generator; normal format validation still decides acceptance.
    """
    configured = merge_rules(rules)["field_aliases"]
    aliases = [(field, normalize_text(alias).upper()) for field, vals in configured.items() for alias in vals]
    items = []
    for block in blocks:
        text = normalize_text(block.get("text", "")).strip()
        poly = block.get("polygon_ocr", block.get("polygon_page"))
        if not text or not poly:
            continue
        try:
            xs = [float(p[0]) for p in poly]; ys = [float(p[1]) for p in poly]
            items.append({"text": text, "compact": compact_text(text).upper(),
                          "x0": min(xs), "x1": max(xs), "y0": min(ys), "y1": max(ys),
                          "yc": sum(ys) / len(ys), "xc": sum(xs) / len(xs), "block": block})
        except (TypeError, ValueError, IndexError):
            continue
    output: dict[str, list[dict]] = {}
    label_tokens = {compact_label(alias) for _, alias in aliases}
    for label in items:
        matches = [(field, alias) for field, alias in aliases if alias and compact_label(alias) in compact_label(label["text"])]
        if not matches:
            continue
        field = max(matches, key=lambda x: len(x[1]))[0]
        candidates = []
        for value in items:
            if value is label or any(tok and tok in compact_label(value["text"]) for tok in label_tokens):
                continue
            # Same-line right neighbour, or a block immediately below the
            # label.  Distances are normalised by the label height.
            height = max(1.0, label["y1"] - label["y0"])
            same_line = abs(value["yc"] - label["yc"]) <= height * 1.8
            same_column = abs(value["xc"] - label["xc"]) <= height * 5
            right = value["x0"] >= label["x1"] - height * 0.25 and same_line
            left = value["x1"] <= label["x0"] + height * 0.25 and same_line
            below = value["y0"] >= label["y1"] - height * 0.25 and same_column
            above = value["y1"] <= label["y0"] + height * 0.25 and same_column
            if right or left or below or above:
                distance = min(
                    max(0.0, value["x0"] - label["x1"]) if right else 1e9,
                    max(0.0, label["x0"] - value["x1"]) if left else 1e9,
                    max(0.0, value["y0"] - label["y1"]) if below else 1e9,
                    max(0.0, label["y0"] - value["y1"]) if above else 1e9,
                )
                compact_value = compact_text(value["text"])
                priority = 0 if re.search(r"(?:\d{4,}|[A-Z]{1,4}[-/]?\d{4,})", compact_value, re.I) else 1
                distance += priority * height * 2
                candidates.append((distance, value))
        if not candidates:
            continue
        value_block = min(candidates, key=lambda x: x[0])[1]["block"]
        value = value_block["text"]
        value = _clean_spatial_value(field, value)
        if not value:
            continue
        # Do not emit a label or a long table sentence as a field value.
        if len(compact_text(value)) > 80:
            continue
        item = _field_candidate(field, value, {"label": label["text"], "spatial": True, "column_unit": _mass_unit(label["text"]),
                                            "polygon_page": value_block.get("polygon_page"),
                                            "label_polygon_page": label["block"].get("polygon_page")})
        item["extraction_method"] = "spatial_label_neighbor"
        output.setdefault(field, []).append(item)
    return output


def _looks_like_company(value: str) -> bool:
    compact = compact_text(value)
    return (any(token in compact for token in ("有限公司", "钢铁", "特钢", "集团", "钢厂"))
            or bool(re.search(r'\b(?:CO\.?|LTD\.?|INC\.?|CORPORATION|FACTORY|GROUP)\b', value, re.I)))


def validate_attachment_fields(fields: Mapping[str, Any], document_type: str | None = None) -> tuple[dict, dict]:
    """Separate format-plausible candidates from obvious OCR/table-header noise."""
    accepted: dict[str, list[dict]] = {}
    rejected: dict[str, list[dict]] = {}
    for field, values in fields.items():
        if not isinstance(values, list):
            values = [values]
        for raw in values:
            item = dict(raw) if isinstance(raw, Mapping) else {"value": str(raw)}
            value = normalize_text(item.get("value", item.get("raw_value", ""))).strip()
            compact = compact_text(value)
            ok = bool(compact)
            reason = None
            if field == "certificate_no":
                ok = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{2,}", compact)) and not any(
                    x in compact.upper() for x in ("检验项目", "合格", "进场", "数量", "规格", "LICENSE", "CERTIFICATE")) and bool(re.search(r"\d", compact)) and not re.fullmatch(r"\d+\.\d+", compact)
            elif field == "heat_lot_no":
                # Steel grades are not heat/lot identifiers.  This explicit
                # guard blocks a frequent OCR false positive (HRB500E).
                ok = (bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{2,}", compact))
                      and not re.fullmatch(r"\d{1,3}", compact)
                      and not re.fullmatch(r"HRB(?:[0-9]{3,4})(?:E|B)?", compact, re.I)
                      and compact.upper() not in {"BATCHNO", "HEATNO", "炉批号"}
                      and bool(re.search(r"\d", compact))
                      and not re.fullmatch(r"\d+\.\d+", compact)
                      and not re.fullmatch(r"(?:HPB|HRBF|RRB|CRB|HRB)\d+[A-Z]*", compact, re.I))
            elif field in {"weight_t", "quantity_t"}:
                parsed = parse_quantity_t(value, default_unit=item.get("unit"))
                label = str(item.get("evidence", {}).get("label", ""))
                ok = parsed is not None and parsed > 0 and not re.search(r"单件|单重|单支|件数|UNIT WEIGHT", label, re.I)
                if ok:
                    item["value_t"] = format(parsed, "f")
                    item["quantity_role"] = "arrival_mass" if field == "quantity_t" else "total_mass"
            elif field == "diameter_mm":
                number = re.fullmatch(r"(?:Φ|φ|Ø|直径)?\s*(\d{1,2}(?:\.\d+)?)(?:mm|毫米)?", compact, re.I)
                ok = bool(number) and 5 <= float(number.group(1)) <= 50
            elif field == "grade":
                ok = bool(re.fullmatch(r"(?:HRB|HRBF|HPB|RRB|CRB)\d{3,4}(?:E|B)?", compact, re.I))
            elif field == "manufacturer":
                ok = _looks_like_company(value)
            elif field == 'supplier':
                ok = _looks_like_company(value) or (compact.startswith('供应商') and len(compact) > 3)
            elif field in {"inspection_date", "supply_date"}:
                match = re.fullmatch(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?", compact)
                ok = bool(match)
                if match:
                    try:
                        date(*map(int, match.groups()))
                    except ValueError:
                        ok = False
            elif field == "usage_location":
                ok = compact not in {"的", "部位", "位置", "使用部位"} and len(compact) >= 2
            if not ok:
                reason = "format_rejected"
                item["validation_status"] = "rejected"
                item["validation_reason"] = reason
                rejected.setdefault(field, []).append(item)
            else:
                item["validation_status"] = "accepted"
                accepted.setdefault(field, []).append(item)
    return accepted, rejected


def _polygon(block: Mapping[str, Any]) -> Any:
    return block.get("polygon_page", block.get("poly", block.get("polygon")))


def _rotate_for_ocr(image, rotation_ccw: int):
    """Rotate a page to its upright orientation before OCR."""
    import numpy as np
    angle = int(rotation_ccw or 0) % 360
    if angle not in (0, 90, 180, 270):
        angle = 0
    return np.ascontiguousarray(np.rot90(image, angle // 90))


def _map_point_to_original(point: Any, shape: tuple[int, ...], rotation_ccw: int) -> list[int]:
    """Map an upright OCR point back to the original rendered-page axes."""
    x, y = float(point[0]), float(point[1])
    h, w = int(shape[0]), int(shape[1])
    angle = int(rotation_ccw or 0) % 360
    if angle == 90:
        return [int(round(w - 1 - y)), int(round(x))]
    if angle == 180:
        return [int(round(w - 1 - x)), int(round(h - 1 - y))]
    if angle == 270:
        return [int(round(y)), int(round(h - 1 - x))]
    return [int(round(x)), int(round(y))]


def _map_blocks_to_original(blocks: list[dict], shape: tuple[int, ...], rotation_ccw: int) -> list[dict]:
    if not rotation_ccw:
        return blocks
    for block in blocks:
        poly = block.get("polygon_page")
        if poly:
            try:
                block.setdefault("polygon_ocr", [list(point) for point in poly])
                block["polygon_page"] = [_map_point_to_original(point, shape, rotation_ccw) for point in poly]
            except (TypeError, ValueError, IndexError):
                pass
    return blocks


def build_regions(blocks: Iterable[Mapping[str, Any]], *, page_no: int,
                  source_image: str | None = None, rotation_ccw: int = 0,
                  rules: Mapping[str, Any] | None = None,
                  region_id: str | None = None,
                  document_type: str | None = None,
                  _layout: bool = True, entity_key: str = "full") -> list[dict]:
    """Convert OCR blocks into one region while retaining block evidence."""
    normalised = []
    texts = []
    scores = []
    for block in blocks:
        text = normalize_text(block.get("text", block.get("rec_text", "")))
        if not text:
            continue
        score = block.get("score", block.get("rec_score", block.get("confidence")))
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        poly = _polygon(block)
        item = {"text": text, "score": score, "polygon_page": poly}
        # Keep the coordinates in the OCR/upright image as a second evidence
        # channel.  ``polygon_page`` may subsequently be mapped back to the
        # original PDF axes for consumers that need page coordinates.
        if block.get("polygon_ocr") is not None:
            item["polygon_ocr"] = block.get("polygon_ocr")
        for key in ("extraction_method", "tile_path", "template_name", "template_path", "priority_field"):
            if block.get(key) is not None:
                item[key] = block[key]
        normalised.append(item)
        texts.append(text)
        if score is not None:
            scores.append(score)
    region_id = region_id or f"p{int(page_no):03d}_full"
    if _layout:
        entities = split_entities(normalised)
        if entities:
            result = []
            for i, (role, entity_blocks) in enumerate(entities):
                result.extend(build_regions(entity_blocks, page_no=page_no, source_image=source_image,
                                            rotation_ccw=rotation_ccw, rules=rules,
                                            region_id=f"{region_id}_entity{i+1}", document_type=role, entity_key=f"entity{i+1}"))
            return result
    evidence = {
        "page_no": int(page_no), "region_id": region_id,
        "polygon_page": None, "source_image": source_image,
        "rotation_ccw": rotation_ccw,
    }
    region = {
        "region_id": region_id, "page_no": int(page_no), "entity_key":entity_key,
        "raw_text": "\n".join(texts), "text": "\n".join(texts),
        "blocks": normalised, "polygon_page": None,
        "source_image": source_image, "rotation_ccw": rotation_ccw,
        "ocr_score": min(scores) if scores else None,
        "has_fragmented_ocr": any(b.get("extraction_method") == "fragmented_region_ocr" for b in normalised),
    }
    region["document_type"] = document_type or classify_page_text(region["text"], rules=rules)
    spatial_text = _line_grouped_text(normalised)
    extracted = extract_attachment_fields(region["text"], evidence, rules=rules)
    if spatial_text and spatial_text != region["text"]:
        spatial = extract_attachment_fields(spatial_text, evidence, rules=rules)
        for field, values in spatial.items():
            existing = {str(item.get("value")) for item in extracted.get(field, [])}
            for value in values:
                if str(value.get("value")) not in existing:
                    value["extraction_method"] = "spatial_line_group"
                extracted.setdefault(field, []).append(value)
    spatial_fields = _spatial_label_fields(normalised, rules=rules)
    for field, values in spatial_fields.items():
        existing = {str(item.get("value")) for item in extracted.get(field, [])}
        for value in values:
            if str(value.get("value")) not in existing:
                extracted.setdefault(field, []).append(value)
    # Bind labelled/line-group candidates to the actual value block, whenever
    # exactly one block contains it. Do not invent a polygon for merged text.
    for values in extracted.values():
        for value in values:
            candidates = [b for b in normalised if compact_text(value['value']) in compact_text(b['text'])]
            if len(candidates) == 1:
                value.setdefault('evidence', {}).update(evidence)
                value['evidence']['polygon_page'] = candidates[0].get('polygon_page')
    valid, rejected = validate_attachment_fields(extracted, region["document_type"])
    polygon_points = [p for block in normalised for p in (block.get('polygon_page') or [])]
    if polygon_points:
        xs, ys = zip(*polygon_points)
        region['polygon_page'] = [[min(xs),min(ys)],[max(xs),min(ys)],[max(xs),max(ys)],[min(xs),max(ys)]]
    region["extracted_fields"] = valid
    region["rejected_fields"] = rejected
    if _layout:
        rows = table_rows(normalised, merge_rules(rules)["field_aliases"]) if region["document_type"] != "nameplate" else []
        if rows:
            region["match_eligible"] = False
            region["layout_role"] = "table_container"
            result = [region]
            for i, row in enumerate(rows):
                row_region_id = f"{region_id}_row{i+1}"
                row_id = f"p{page_no:03d}_{entity_key}_row{i+1}"
                row_fields = {}
                row_blocks = []
                for (header, bounds, field), block in row['cells']:
                    if field in {"piece_count", "unit_weight"}:
                        continue
                    if field == 'weight_t' and region['document_type'] == 'delivery_note':
                        field = 'quantity_t'
                    value = block['text'].strip()
                    ev = {**evidence, "region_id": row_region_id, "row_id": row_id, "physical_row_index":i+1,
                          "label": header['text'], "column_unit": _mass_unit(header['text']),
                          "polygon_page": block.get('polygon_page'),
                          "label_polygon_page": header.get('polygon_page')}
                    item = _field_candidate(field, value, ev)
                    if field == 'diameter_mm':
                        # Combined specification columns such as
                        # 14*9HRB500E carry grade, diameter and length.
                        grade_match = re.search(r'(?:HRB|HRBF|HPB|RRB|CRB)\s*\d{3,4}(?:E|B)?', value, re.I)
                        diameter_match = re.match(r'\s*[ΦφØ]?\s*(\d{1,2}(?:\.\d+)?)(?:\s*[*×/]|\s*(?:HRB|HRBF|HPB|RRB|CRB))', value, re.I)
                        if grade_match and diameter_match:
                            row_fields.setdefault('grade', []).append(_field_candidate('grade', compact_text(grade_match.group()), ev))
                            item = _field_candidate(field, diameter_match.group(1), ev)
                    item["row_id"] = row_id
                    item["extraction_method"] = "table_column_row"
                    row_fields.setdefault(field, []).append(item)
                    row_blocks.append(block)
                # Header identities may apply to every row, row measurements never do.
                for field in ('certificate_no', 'manufacturer', 'supplier', 'supply_date'):
                    header_values = [x for x in valid.get(field, [])
                                     if x.get('evidence', {}).get('polygon_page') and
                                     max(p[1] for p in x['evidence']['polygon_page']) < min(h[1][1] for h,b in row['cells'])]
                    if len({x.get('value') for x in header_values}) == 1 and field not in row_fields:
                        row_fields[field] = header_values
                accepted, denied = validate_attachment_fields(row_fields, region['document_type'])
                row_points=[p for b in row_blocks for p in (b.get('polygon_page') or [])]
                row_polygon=None
                if row_points:
                    xs,ys=zip(*row_points)
                    row_polygon=[[min(xs),min(ys)],[max(xs),min(ys)],[max(xs),max(ys)],[min(xs),max(ys)]]
                result.append({**region, 'polygon_page':row_polygon, 'region_id':row_region_id, 'row_id':row_id, 'physical_row_index':i+1,
                               'parent_region_id':region_id, 'match_eligible':True,
                               'layout_role':'table_row', 'blocks':row_blocks,
                               'text':'\n'.join(b['text'] for b in row_blocks),
                               'raw_text':'\n'.join(b['text'] for b in row_blocks),
                               'extracted_fields':accepted, 'rejected_fields':denied})
            return result
    return [region]


def build_index_from_pages(pages: Iterable[Mapping[str, Any]], *, pdf_sha256: str | None = None,
                           pdf_name: str | None = None,
                           rules: Mapping[str, Any] | None = None) -> dict:
    """Build the JSON index from page/block dictionaries (offline/test API)."""
    effective_rules = merge_rules(rules)
    output_pages = []
    for page in pages:
        page_no = int(page.get("page_no", len(output_pages) + 1))
        blocks = page.get("blocks")
        if blocks is None:
            blocks = page.get("ocr_blocks", [])
        source_image = page.get("source_image") or page.get("image_path")
        rotation = int(page.get("rotation_ccw", 0) or 0)
        regions = page.get("regions")
        if regions is None:
            regions = build_regions(blocks, page_no=page_no, source_image=source_image,
                                    rotation_ccw=rotation, rules=effective_rules)
        else:
            regions = [dict(region) for region in regions]
            for region in regions:
                region.setdefault("page_no", page_no)
                region.setdefault("rotation_ccw", rotation)
                region.setdefault("source_image", source_image)
                region.setdefault("raw_text", region.get("text", ""))
                region.setdefault("text", region.get("raw_text", ""))
                region.setdefault("blocks", [])
                region.setdefault("ocr_score", None)
                region.setdefault("document_type", classify_page_text(region["text"], page.get("document_type"), effective_rules))
                region.setdefault("extracted_fields", extract_attachment_fields(region["text"], rules=effective_rules))
        page_type = page.get("document_type") or page.get("classification") or classify_page_text(
            "\n".join(r.get("text", "") for r in regions), rules=effective_rules)
        output_pages.append({
            "page_no": page_no,
            "document_type": page_type,
            "classification": page.get("classification", page_type),
            "rotation_ccw": rotation,
            "source_image": source_image,
            "regions": regions,
            "preprocessing": page.get("preprocessing"),
            "ocr_status": page.get("ocr_status", "completed"),
            "error_type": page.get("error_type"),
            "performance": page.get('performance'),
            "pixel_size": page.get('pixel_size'), "dpi": page.get('dpi'),
            "upright_image": page.get('upright_image'),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "pdf_name": pdf_name,
        "pdf_sha256": pdf_sha256,
        "page_count": len(output_pages),
        "pages": output_pages,
        "rules": effective_rules,
        "notes": ["all_pages_including_other_review_and_error", "raw_text_and_coordinates_preserved",
                   "identifier_normalization_does_not_replace_O_or_0"],
    }


def digest(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _render_page(pdf, page_index: int, dpi: int):
    import cv2
    import numpy as np
    page = pdf[page_index]
    try:
        bitmap = page.render(scale=dpi / 72)
        try:
            pil = bitmap.to_pil()
            try:
                return cv2.cvtColor(np.asarray(pil.convert("RGB")), cv2.COLOR_RGB2BGR)
            finally:
                pil.close()
        finally:
            bitmap.close()
    finally:
        page.close()


def _paddle_ocr(device: str, runtime=None):
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise RuntimeError("缺少 PaddleOCR，请在 ocr 环境执行 pip install -r requirements.txt") from exc
    runtime = runtime or {}
    options = {}
    batch_size = runtime.get('recognition_batch_size')
    if batch_size is not None:
        if int(batch_size) < 1:
            raise ValueError('recognition_batch_size must be positive')
        options['text_recognition_batch_size'] = int(batch_size)
    return PaddleOCR(lang="ch", device=device, use_doc_orientation_classify=False,
                     use_doc_unwarping=False, use_textline_orientation=False, **options)


class TimedOCREngine:
    """Measure consumed inference, including lazy predict generators."""
    def __init__(self, engine):
        self.engine = engine
        self.calls = 0
        self.seconds = 0.0

    def predict(self, *args, **kwargs):
        start = time.perf_counter()
        self.calls += 1
        try:
            yield from self.engine.predict(*args, **kwargs)
        finally:
            self.seconds += time.perf_counter() - start


def efficient_region_templates(templates, runtime):
    """Skip broad repeat passes only when each physical entity has key crops.

    Keep every planned field crop and every row window. If a key label was
    not detected, keep the broad pass to recover it. No plate is dropped just
    because another plate shares its entity key.
    """
    if runtime.get('profile') != 'fast_gpu':
        return templates
    required = {'delivery_note': {'quantity_t', 'diameter_mm'},
                'quality_certificate': {'certificate_no', 'heat_lot_no', 'diameter_mm'},
                'nameplate': {'heat_lot_no', 'diameter_mm'}}
    focused = {}
    for t in templates:
        key = (t.get('entity_key', 'full'), t.get('entity_role') or t.get('document_type'))
        if t.get('priority_field'):
            focused.setdefault(key, set()).add(t['priority_field'])
    return [t for t in templates if t.get('priority_field') or not required.get(t.get('entity_role') or t.get('document_type'))
            or not required[t.get('entity_role') or t.get('document_type')].issubset(
                focused.get((t.get('entity_key', 'full'), t.get('entity_role') or t.get('document_type')), set()))]


def _ocr_blocks(engine, image, image_path: Path) -> list[dict]:
    result = engine.predict(str(image_path))
    blocks = []
    for item in result:
        data = getattr(item, "json", None)
        if callable(data):
            data = data()
        if data is None and hasattr(item, "to_json"):
            data = item.to_json()
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, Mapping) and "res" in data:
            data = data["res"]
        if not isinstance(data, Mapping):
            continue
        texts = data.get("rec_texts", data.get("texts", [])) or []
        scores = data.get("rec_scores", data.get("scores", [])) or []
        polys = data.get("dt_polys", data.get("polys", [])) or []
        for i, text in enumerate(texts):
            poly = polys[i] if i < len(polys) else None
            blocks.append({"text": text, "score": scores[i] if i < len(scores) else None,
                           "polygon_page": poly, "polygon_ocr": poly})
    return blocks


def focused_fields_complete(template, specs, rules=None):
    """Only skip a broad pass after every required crop/row yields valid evidence."""
    role = template.get('entity_role') or template.get('document_type')
    required = {'delivery_note': {'quantity_t', 'diameter_mm'},
                'quality_certificate': {'certificate_no', 'heat_lot_no', 'diameter_mm'},
                'nameplate': {'heat_lot_no', 'diameter_mm'}}.get(role, set())
    key = template.get('entity_key', 'full')
    covered = set()
    for spec in specs:
        field = spec.get('priority_field')
        if field not in required or spec.get('entity_key', 'full') != key or spec['document_type'] != role:
            continue
        regions = _build_spec_regions(spec, page_no=0, source_image='', rotation_ccw=0, rules=rules)
        valid = [o for r in regions for o in r.get('field_observations', [])
                 if o.get('validation_status') == 'accepted']
        expected_rows = {w['physical_row_index'] for w in spec.get('row_windows', [])} or {None}
        if expected_rows.issubset({o.get('physical_row_index') for o in valid}):
            covered.add(field)
    return bool(required) and required.issubset(covered)


def _verify_attachment_orientation(engine, original, rotation_hint: int,
                                   out_dir: Path, stem: str, *, source_image=None) -> tuple:
    """Evaluate all four directions on a thumbnail before applying templates.

    An ambiguous direction is preserved as a review condition. No deskew is
    applied without a validated coordinate transform.
    """
    import cv2
    scale = min(1.0, 1400 / max(original.shape[:2]))
    preview = cv2.resize(original, None, fx=scale, fy=scale)
    attempts = []
    for angle in (0, 90, 180, 270):
        image = _rotate_for_ocr(preview, angle)
        path = out_dir / f"{stem}_direction_{angle}.png"
        cv2.imwrite(str(path), image)
        if source_image:
            register_derivative(path, source_image,
                [{'resize': {'scale': scale}}, {'rotate': angle}], kind='direction_preview')
        blocks = _ocr_blocks(engine, image, path)
        attempts.append({"rotation_ccw": angle, "score": orientation_score(blocks),
                         "block_count": len(blocks), "preview_path": str(path)})
    ranked = sorted(attempts, key=lambda a: (-a['score'], a['rotation_ccw'] != rotation_hint))
    best, second = ranked[:2]
    confident = best['score'] >= 3 and best['score'] > second['score'] * 1.15
    rotation = best['rotation_ccw'] if confident else rotation_hint % 360
    image = _rotate_for_ocr(original, rotation)
    return image, rotation, {"attempts": attempts, "rotation_hint": rotation_hint,
                             "selected_rotation_ccw": rotation,
                             "orientation_review": not confident}


def _fragmented_ocr_blocks(engine, image, image_path: Path, out_dir: Path,
                           base_blocks: list[dict], rules: Mapping[str, Any] | None = None) -> list[dict]:
    """Run a conservative tiled OCR retry when the full-page text is fragmented."""
    import cv2
    cfg = merge_rules(rules).get("fragmented_ocr", {})
    if not cfg.get("enabled", True) or not base_blocks:
        return []
    scores = [float(b["score"]) for b in base_blocks if b.get("score") is not None]
    # Good full-page OCR should not pay the cost of six extra recognitions.
    # The threshold is deliberately conservative; a low average score is a
    # routing signal only, never evidence that a recovered value is correct.
    if scores and sum(scores) / len(scores) >= float(cfg.get("retry_mean_score", 0.75)):
        return []
    rows, cols = int(cfg.get("tile_rows", 3)), int(cfg.get("tile_cols", 2))
    scale = float(cfg.get("upscale", 1.6))
    height, width = image.shape[:2]
    recovered = []
    for row in range(rows):
        for col in range(cols):
            x0, x1 = width * col // cols, width * (col + 1) // cols
            y0, y1 = height * row // rows, height * (row + 1) // rows
            crop = image[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            if scale != 1:
                crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            tile_path = out_dir / f"{image_path.stem}_tile_{row}_{col}.png"
            cv2.imwrite(str(tile_path), crop)
            register_derivative(tile_path, image_path,
                [{'crop': [x0, y0, x1, y1]},
                 {'resize': {'scale': scale, 'interpolation': 'INTER_CUBIC'}}], kind='fragmented_tile')
            for block in _ocr_blocks(engine, crop, tile_path):
                poly = block.get("polygon_page")
                if poly:
                    try:
                        mapped = [[int(p[0] / scale + x0), int(p[1] / scale + y0)] for p in poly]
                        block["polygon_page"] = mapped
                        block["polygon_ocr"] = mapped
                    except (TypeError, ValueError, IndexError):
                        pass
                block["extraction_method"] = "fragmented_region_ocr"
                block["tile_path"] = str(tile_path)
                recovered.append(block)
    return recovered


def _template_ocr_regions(engine, image, image_path: Path, out_dir: Path,
                           document_type: str, rules: Mapping[str, Any] | None = None,
                           page_blocks: list | None = None) -> list[dict]:
    """OCR configured form zones and return independent evidence regions."""
    import cv2
    templates = [dict(x) for x in merge_rules(rules).get("region_templates", {}).get(document_type, [])]
    height, width = image.shape[:2]
    entities = split_entities(page_blocks or [])
    if entities:
        # Crop each observed form in upright coordinates; static page
        # templates must not span a delivery note and its pasted nameplate.
        templates = []
        from material_ocr.ocr.layout import box
        for i, (role, entity_blocks) in enumerate(entities):
            bounds = [box(b) for b in entity_blocks if box(b)]
            templates.append({'name':f'entity_{i+1}_{role}', 'document_type':role, 'entity_role':role, 'entity_key':f'entity{i+1}',
                              'x0':max(0,min(b[0] for b in bounds)-10)/width,
                              'y0':max(0,min(b[1] for b in bounds)-10)/height,
                              'x1':min(width,max(b[2] for b in bounds)+10)/width,
                              'y1':min(height,max(b[3] for b in bounds)+10)/height,
                              'upscale':2.0})
        # High-value field crops are generated from observed labels/headers,
        # never from a fixed page fraction.
        for i, (role, entity_blocks) in enumerate(entities):
            focused = priority_field_templates(entity_blocks, role, merge_rules(rules)['field_aliases'], width, height)
            for focus in focused:
                focus['name'] = f"entity_{i+1}_{role}_{focus['name']}"
                focus['entity_role'] = role
                focus['entity_key'] = f'entity{i+1}'
            templates.extend(focused)
    else:
        templates.extend(priority_field_templates(page_blocks or [], document_type, merge_rules(rules)['field_aliases'], width, height))
    # Configured broad templates inherit the page role for key-completeness checks.
    for template in templates:
        template.setdefault('document_type', document_type)
    runtime = merge_rules(rules)['ocr_runtime']
    planned = efficient_region_templates(templates, runtime)
    omitted = [t for t in templates if t not in planned]
    # OCR focused fields first; label presence alone does not prove a usable value.
    templates = planned + omitted
    output = []
    for template in templates:
        if template in omitted and focused_fields_complete(template, output, rules):
            continue
        name = str(template.get("name", "region"))
        x0, y0 = max(0.0, float(template.get("x0", 0))), max(0.0, float(template.get("y0", 0)))
        x1, y1 = min(1.0, float(template.get("x1", 1))), min(1.0, float(template.get("y1", 1)))
        px0, py0, px1, py1 = int(width*x0), int(height*y0), int(width*x1), int(height*y1)
        crop = image[py0:py1, px0:px1]
        if crop.size == 0:
            continue
        scale = float(template.get("upscale", 2.0))
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        crop_path = out_dir / f"{image_path.stem}_{name}.png"
        cv2.imwrite(str(crop_path), crop)
        register_derivative(crop_path, image_path,
            [{'crop': [px0, py0, px1, py1]},
             {'resize': {'scale': scale, 'interpolation': 'INTER_CUBIC'}}], kind='field_or_region_crop')
        blocks = _ocr_blocks(engine, crop, crop_path)
        for block in blocks:
            poly = block.get("polygon_page")
            if poly:
                try:
                    mapped = [[int(p[0]/scale + px0), int(p[1]/scale + py0)] for p in poly]
                    block["polygon_page"] = mapped
                    block["polygon_ocr"] = mapped
                except (TypeError, ValueError, IndexError):
                    pass
            block["extraction_method"] = "priority_field_ocr" if template.get("priority_field") else "entity_region_ocr" if template.get("entity_role") else "template_region_ocr"
            if template.get("priority_field"):
                block["priority_field"] = template["priority_field"]
            block["template_name"] = name
            block["template_path"] = str(crop_path)
        crop_type = classify_page_text("\n".join(b["text"] for b in blocks), rules=rules)
        output.append({"name": name, "document_type": template.get('document_type') or (crop_type if crop_type != "unknown" else document_type),
                       "priority_field": template.get('priority_field'), "entity_role": template.get('entity_role'),
                       "crop_polygon_page": [[px0,py0],[px1,py0],[px1,py1],[px0,py1]],
                       "crop_polygon_ocr": [[px0,py0],[px1,py0],[px1,py1],[px0,py1]],
                       "label_text":template.get('label_text'),
                       "label_polygon_ocr":template.get('label_polygon_ocr'),
                       "entity_key":template.get('entity_key','full'),
                       "row_windows":template.get('row_windows', []), "blocks": blocks,
                       "region_id_suffix": name})
    return output


def _map_region_specs_to_original(specs: list[dict], original_shape: tuple[int, ...],
                                  rotation_ccw: int) -> list[dict]:
    for spec in specs:
        for key in ('crop','label'):
            polygon = spec.get(f'{key}_polygon_ocr')
            if polygon:
                spec[f'{key}_polygon_page'] = [_map_point_to_original(p, original_shape, rotation_ccw) for p in polygon]
        for window in spec.get('row_windows', []):
            window['polygon_page'] = [_map_point_to_original(p, original_shape, rotation_ccw) for p in window['polygon_ocr']]
        _map_blocks_to_original(spec.get("blocks", []), original_shape, rotation_ccw)
    return specs


def _build_spec_regions(spec, *, page_no, source_image, rotation_ccw, rules=None):
    """Publish focused OCR as row-bound observations, never standalone joins."""
    region_id = f"p{page_no:03d}_{spec['region_id_suffix']}"
    regions = build_regions(spec['blocks'], page_no=page_no, source_image=source_image,
                            rotation_ccw=rotation_ccw, rules=rules, region_id=region_id,
                            document_type=spec['document_type'], _layout=not bool(spec.get('priority_field')),
                            entity_key=spec.get('entity_key','full'))
    for region in regions:
        region.update({key:spec[key] for key in ('priority_field','entity_role','entity_key',
                                               'crop_polygon_page','crop_polygon_ocr') if spec.get(key) is not None})
    field = spec.get('priority_field')
    if not field:
        return regions
    region = regions[0]
    region['match_eligible'] = False
    region['layout_role'] = 'field_focus'
    region['polygon_page'] = spec.get('crop_polygon_page')
    observations = []
    windows = spec.get('row_windows') or [None]
    for window in windows:
        selected = spec['blocks']
        if window:
            from material_ocr.ocr.layout import box
            bounds = box({'polygon_ocr':window['polygon_ocr']})
            selected = [b for b in selected if box(b) and
                        bounds[1] <= (box(b)[1]+box(b)[3])/2 <= bounds[3]]
        for block in selected:
            value = normalize_text(block['text']).strip()
            if not window:
                fields = extract_attachment_fields(value, rules=rules)
                if fields.get(field):
                    values = [x['value'] for x in fields[field]]
                else:
                    values = [_clean_spatial_value(field, value)]
            elif field == 'diameter_mm':
                match = re.fullmatch(r'[ΦφØ]?\s*(\d{1,2}(?:\.\d+)?)(?:[/×*]\d+(?:\.\d+)?)?(?:(?:HRB|HPB|HRBF|RRB|CRB)\d{3,4}E?)?', compact_text(value), re.I)
                values = [match.group(1)] if match else [value]
            else:
                values = [value]
            if field == 'diameter_mm':
                cleaned=[]
                for raw in values:
                    dimension=re.fullmatch(r'[ΦφØ]?\s*(\d{1,2}(?:\.\d+)?)(?:[/×*]\d+(?:\.\d+)?)?(?:(?:HRB|HPB|HRBF|RRB|CRB)\d{3,4}E?)?',compact_text(raw),re.I)
                    cleaned.append(dimension.group(1) if dimension else raw)
                values=cleaned
            row_index = window['physical_row_index'] if window else None
            row_id = f"p{page_no:03d}_{spec.get('entity_key','full')}_row{row_index}" if window else None
            for value in values:
                if not value:
                    continue
                ev = {'page_no':page_no, 'region_id':region_id, 'row_id':row_id,
                      'physical_row_index':row_index, 'polygon_page':block.get('polygon_page'),
                      'label_polygon_page':spec.get('label_polygon_page'),
                      'crop_polygon_page':spec.get('crop_polygon_page'),
                      'label':spec.get('label_text'), 'column_unit':_mass_unit(spec.get('label_text') or ''),
                      'source_image':source_image, 'rotation_ccw':rotation_ccw}
                item = _field_candidate(field, value, ev)
                item['raw_value']=block['text']
                item.update(row_id=row_id, physical_row_index=row_index, extraction_method='priority_field_ocr')
                valid, denied = validate_attachment_fields({field:[item]}, spec['document_type'])
                observations.extend(valid.get(field, []) + denied.get(field, []))
    region['field_observations'] = observations
    region['row_windows'] = spec.get('row_windows', [])
    return regions


def build_region_index_from_existing(index_path: str | Path, out: str | Path, *,
                                      device: str = "cpu", workers: int = 1,
                                      rules: Mapping[str, Any] | None = None) -> dict:
    """Reuse an existing full-page index and OCR only configured regions.

    This is the practical evaluation path: expensive full-page OCR is not
    repeated when tuning attachment templates.
    """
    import cv2
    if workers != 1:
        raise ValueError("既有索引区域重识别当前仅支持 workers=1")
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", "/tmp/paddlex_ocr")
    base = load_json(index_path)
    effective_rules = merge_rules(rules or base.get("rules"))
    out_path = Path(out); out_path.mkdir(parents=True, exist_ok=True)
    image_dir = out_path / "pages"; image_dir.mkdir(exist_ok=True)
    source_root = Path(index_path).resolve().parent
    pages = []
    engine = _paddle_ocr(device, effective_rules['ocr_runtime'])
    # Existing source images are rendered-page PNGs and are sufficient for
    # region OCR; no PDF/model pass is needed for the untouched pages.
    for page in base.get("pages", []):
        page_type = page.get("document_type", "unknown")
        source = page.get("source_image") or (page.get("regions") or [{}])[0].get("source_image")
        image_path = source_root / source if source else None
        regions = [{**r, "source_image":str(image_path) if image_path else r.get("source_image")} for r in page.get("regions", [])]
        page_text = "\n".join(str(r.get("text", r.get("raw_text", ""))) for r in regions)
        # The first-stage hint intentionally routes many attachments as
        # ``other``/``review``.  Reclassify from the full-page OCR before
        # selecting region templates; otherwise quality certificates and
        # nameplates never receive their field-level OCR pass.
        inferred_type = classify_page_text(page_text, rules=effective_rules)
        if inferred_type != "unknown":
            page_type = inferred_type
        if image_path and image_path.exists():
            original_image = cv2.imread(str(image_path))
            if original_image is not None:
                image, rotation, direction = _verify_attachment_orientation(
                    engine, original_image, int(page.get("rotation_ccw", 0) or 0),
                    image_dir, f"page_{int(page['page_no']):03d}", source_image=image_path)
                ocr_path = image_dir / f"page_{int(page['page_no']):03d}_upright.png"
                cv2.imwrite(str(ocr_path), image)
                page = {**page, 'rotation_ccw': rotation,
                        'upright_image': str(ocr_path.relative_to(out_path)),
                        'preprocessing_order': 'orientation_then_ocr',
                        'preprocessing': {**assess_image(image, []), **direction}}
                full_blocks = _ocr_blocks(engine, image, ocr_path)
                page['preprocessing'].update(assess_image(image, full_blocks))
                full_blocks = _map_blocks_to_original(full_blocks, original_image.shape, rotation)
                refreshed_type = classify_page_text('\n'.join(b['text'] for b in full_blocks), rules=effective_rules)
                if refreshed_type != 'unknown':
                    page_type = refreshed_type
                regions = [{**r, 'match_eligible':False, 'layout_role':'previous_index'} for r in regions]
                regions.extend(build_regions(full_blocks, page_no=int(page['page_no']), source_image=str(image_path),
                                             rotation_ccw=rotation, rules=effective_rules, region_id=f"p{int(page['page_no']):03d}_refreshed", document_type=page_type))
                specs = _template_ocr_regions(engine, image, ocr_path, image_dir, page_type, effective_rules, full_blocks)
                _map_region_specs_to_original(specs, original_image.shape, int(page.get("rotation_ccw", 0) or 0))
                for spec in specs:
                    regions.extend(_build_spec_regions(spec, page_no=int(page['page_no']),
                                                       source_image=str(image_path), rotation_ccw=rotation,
                                                       rules=effective_rules))
        pages.append({**page, "source_image":str(image_path) if image_path else source, "document_type": page_type, "classification": page.get("classification", page_type), "regions": regions})
    result = build_index_from_pages(pages, pdf_sha256=base.get("pdf_sha256"),
                                    pdf_name=base.get("pdf_name"), rules=effective_rules)
    write_json(out_path / "attachment_index.json", result)
    return result


def _ocr_pages_worker(payload: Mapping[str, Any]) -> list[dict]:
    """Process a page shard in an isolated process.

    PaddleOCR engines are process-local.  Keeping construction and inference
    inside the worker avoids unsafe sharing and lets CPU workers run in
    parallel.  Each worker writes distinct page images, so the merge step is
    deterministic.
    """
    import pypdfium2 as pdfium
    import cv2
    import os
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    pdf_path = Path(payload["pdf_path"])
    out_path = Path(payload["out_path"])
    image_dir = out_path / "pages"
    image_dir.mkdir(parents=True, exist_ok=True)
    rules = payload.get("rules")
    init_start = time.perf_counter()
    engine = TimedOCREngine(_paddle_ocr(str(payload.get("device", "cpu")), merge_rules(rules)['ocr_runtime']))
    initialization_seconds = time.perf_counter() - init_start
    pdf_obj = pdfium.PdfDocument(str(pdf_path))
    pages = []
    try:
        for page_no in payload["page_nos"]:
            page_start, calls_before, seconds_before = time.perf_counter(), engine.calls, engine.seconds
            hint = payload.get("page_hints", {}).get(str(page_no), {})
            try:
                rotation = int(hint.get("rotation_ccw", 0) or 0)
                image = _render_page(pdf_obj, page_no - 1, int(payload["dpi"]))
                original_shape = image.shape
                original_path = image_dir / f"page_{page_no:03d}.png"
                cv2.imwrite(str(original_path), image)
                image, rotation, direction = _verify_attachment_orientation(
                    engine, image, rotation, image_dir, f"page_{page_no:03d}", source_image=original_path)
                ocr_path = image_dir / f"page_{page_no:03d}_upright.png"
                cv2.imwrite(str(ocr_path), image)
                page_type = hint.get("document_type") or hint.get("decision")
                blocks = _ocr_blocks(engine, image, ocr_path)
                blocks.extend(_fragmented_ocr_blocks(engine, image, ocr_path, image_dir,
                                                     blocks, rules))
                blocks = _map_blocks_to_original(blocks, original_shape, rotation)
                preprocessing = assess_image(image, blocks)
                preprocessing["orientation_score"] = orientation_score(blocks)
                preprocessing["rotation_hint"] = rotation
                preprocessing.update(direction)
                hint_text = "\n".join(str(a.get("ocr_text", "")) for a in hint.get("attempts", []))
                detected_type = classify_page_text(hint_text + "\n" + "\n".join(str(b.get("text", "")) for b in blocks), page_type, rules)
                regions = build_regions(blocks, page_no=page_no,
                                        source_image=str(original_path.relative_to(out_path)),
                                        rotation_ccw=rotation, rules=rules,
                                        document_type=detected_type)
                specs = _template_ocr_regions(engine, image, ocr_path, image_dir, detected_type, rules, blocks)
                _map_region_specs_to_original(specs, original_shape, rotation)
                for spec in specs:
                    regions.extend(_build_spec_regions(spec, page_no=page_no,
                                                       source_image=str(original_path.relative_to(out_path)),
                                                       rotation_ccw=rotation, rules=rules))
                from .nameplates import physical_regions
                cards = physical_regions(engine, image, blocks, page_no, image_dir,
                                         original_shape, rotation, rules)
                if cards:
                    # Full-page/crop OCR remains evidence, but cannot pool card fields.
                    for region in regions:
                        if region.get('document_type') == 'nameplate' or region.get('entity_role') == 'nameplate':
                            region.update(match_eligible=False, layout_role='page_evidence')
                    for card in cards:
                        card['source_image'] = str(original_path.relative_to(out_path))
                    regions.extend(cards)
                pages.append({"page_no": page_no, "classification": page_type,
                              "document_type": detected_type,
                              "rotation_ccw": rotation,
                              "pixel_size": [original_shape[1], original_shape[0]], "dpi": int(payload['dpi']),
                              "source_image": str(original_path.relative_to(out_path)),
                              "upright_image": str(ocr_path.relative_to(out_path)),
                              "preprocessing_order": "orientation_then_ocr",
                              "regions": regions, "preprocessing": preprocessing,
                              "performance": {'device':str(payload.get('device', 'cpu')),
                                  'profile':merge_rules(rules)['ocr_runtime']['profile'],
                                  'recognition_batch_size':merge_rules(rules)['ocr_runtime']['recognition_batch_size'],
                                  'page_wall_seconds':time.perf_counter()-page_start,
                                  'ocr_calls':engine.calls-calls_before,
                                  'ocr_seconds':engine.seconds-seconds_before,
                                  'engine_initialization_seconds':initialization_seconds}})
            except Exception as exc:
                if 'out of memory' in str(exc).lower() or 'resourceexhausted' in str(exc).lower():
                    # Leave this page pending so the parent can retry in a fresh,
                    # smaller GPU worker; earlier checkpoints remain intact.
                    raise
                pages.append({'page_no':page_no, 'classification':hint.get('decision'),
                              'document_type':'unknown', 'rotation_ccw':0, 'regions':[],
                              'ocr_status':'failed', 'error_type':type(exc).__name__,
                              'preprocessing':{'orientation_review':True,
                                               'review_reasons':['attachment_ocr_failed']}})
                print(f"attachment page {page_no}: failed ({type(exc).__name__})", flush=True)
            write_json(out_path / 'checkpoints' / f'page_{page_no:03d}.json', pages[-1])
            print(f"attachment page {page_no} done", flush=True)
            progress = payload.get('progress')
            if progress:
                progress('attachments', len(pages), len(payload['page_nos']), f'附件OCR {len(pages)}/{len(payload["page_nos"])}')
    finally:
        pdf_obj.close()
    return pages


def build_attachment_index(pdf: str | Path, out: str | Path, *, page_index_path: str | Path | None = None,
                           device: str = "gpu:0", dpi: int = 180,
                           rules: Mapping[str, Any] | None = None,
                           workers: int = 1, progress=None, page_nos=None) -> dict:
    """OCR every PDF page and write ``attachment_index.json``.

    By default this uses one process.  With ``workers>1`` on CPU, each worker
    owns an independent PaddleOCR engine and page shards are merged by page
    number; engines are never shared across processes.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError("缺少 pypdfium2，请在 ocr 环境执行 pip install -r requirements.txt") from exc
    pdf_path = Path(pdf).resolve()
    out_path = Path(out)
    target_file = out_path if out_path.suffix.lower() == ".json" else None
    if target_file is not None:
        out_path = target_file.parent
    out_path.mkdir(parents=True, exist_ok=True)
    # Paddlex otherwise defaults to ``~/.paddlex``.  Managed/readonly home
    # directories are common on workers, so keep model/cache state beside the
    # run and make the path explicit in the output environment.
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(out_path / "paddlex_cache"))
    image_dir = out_path / "pages"
    image_dir.mkdir(exist_ok=True)
    page_hints = {}
    if page_index_path:
        manifest = json.loads(Path(page_index_path).read_text(encoding="utf-8"))
        page_hints = {int(p["page_no"]): p for p in manifest.get("pages", [])}
    effective_rules = merge_rules(rules)
    page_hints_json = {str(k): v for k, v in page_hints.items()}
    pdf_probe = pdfium.PdfDocument(str(pdf_path))
    try:
        page_count = len(pdf_probe)
    finally:
        pdf_probe.close()
    page_nos = list(page_nos) if page_nos is not None else list(range(1, page_count + 1))
    if workers > 1:
        if not str(device).startswith("cpu"):
            raise ValueError("多进程 OCR 目前只支持 CPU device；GPU 请使用 workers=1")
        import multiprocessing as mp
        worker_count = min(int(workers), len(page_nos))
        payloads = []
        for i in range(worker_count):
            payloads.append({"pdf_path": str(pdf_path), "out_path": str(out_path),
                             "page_nos": page_nos[i::worker_count], "page_hints": page_hints_json,
                             "device": device, "dpi": dpi, "rules": effective_rules})
        ctx = mp.get_context("spawn")
        with ctx.Pool(worker_count) as pool:
            pages = [page for shard in pool.map(_ocr_pages_worker, payloads) for page in shard]
        pages.sort(key=lambda x: x["page_no"])
        index = build_index_from_pages(pages, pdf_sha256=digest(pdf_path), pdf_name=pdf_path.name,
                                       rules=effective_rules)
        target = target_file or (out_path / "attachment_index.json")
        write_json(target, index)
        return index
    pages = _ocr_pages_worker({'pdf_path':str(pdf_path), 'out_path':str(out_path),
                               'dpi':dpi, 'device':device, 'rules':effective_rules,
                               'page_hints':{str(k):v for k,v in page_hints.items()},
                               'page_nos':page_nos, 'progress':progress})
    index = build_index_from_pages(pages, pdf_sha256=digest(pdf_path), pdf_name=pdf_path.name,
                                   rules=effective_rules)
    target = target_file or (out_path / "attachment_index.json")
    write_json(target, index)
    return index


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, value: Any) -> None:
    from material_ocr.workflow import write_json as atomic_write
    atomic_write(path, value)


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf")
    parser.add_argument("--out", required=True, help="输出目录或 attachment_index.json 路径")
    parser.add_argument("--page-index")
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--dpi", type=int, default=180)
    parser.add_argument("--rules", help="可选 JSON 规则配置")
    parser.add_argument('--ocr-profile', choices=['complete', 'batched_gpu', 'fast_gpu'], default='complete',
                        help='batched_gpu keeps all crops with batch=32; fast_gpu also skips broad repeats (experimental)')
    parser.add_argument("--workers", type=int, default=1,
                        help="CPU OCR 并行进程数；每个进程独立加载一份模型，默认 1")
    parser.add_argument("--existing-index", help="复用既有全页索引，只执行模板区域 OCR")
    args = parser.parse_args(argv)
    effective_rules = load_rules(args.rules)
    if args.ocr_profile in ('batched_gpu', 'fast_gpu'):
        if not args.device.startswith('gpu'):
            parser.error('GPU OCR profiles require --device gpu:0 (or another GPU id)')
        effective_rules['ocr_runtime'].update(profile=args.ocr_profile, recognition_batch_size=32)
    if args.existing_index:
        build_region_index_from_existing(args.existing_index, args.out,
                                         device=args.device,
                                         workers=args.workers,
                                         rules=effective_rules)
        return 0
    if not args.pdf:
        parser.error("未提供 --existing-index 时必须提供 --pdf")
    out = Path(args.out)
    if out.suffix.lower() == ".json":
        index = build_attachment_index(args.pdf, out.parent, page_index_path=args.page_index,
                                       device=args.device, dpi=args.dpi,
                                       rules=effective_rules,
                                       workers=args.workers)
        write_json(out, index)
    else:
        build_attachment_index(args.pdf, out, page_index_path=args.page_index,
                               device=args.device, dpi=args.dpi,
                               rules=effective_rules,
                               workers=args.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
