"""Adapt GLM per-cell text to the existing coordinate table matcher.

GLM-OCR's native page output is a logical document transcription and does not
provide word boxes.  This module is intentionally narrower: callers provide
the *measured* table-cell polygons (for example from the existing table
geometry pass) and the text recognized for each cell by GLM.  The original
``coordinate_match`` code then remains responsible for header detection, row
ownership, field parsing, and fail-closed diagnostics.

The adapter never manufactures an OCR confidence value.  GLM cell scores are
represented as ``None`` and are carried through the evidence.  A downstream
matcher that requires confidence must treat ``None`` as unknown, rather than
as a high or low score.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any, Iterable, Mapping


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _polygon(value: Any, *, cell_id: str) -> list[list[float]]:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        raise ValueError(f"cell {cell_id} requires a real polygon_page")
    result = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ValueError(f"cell {cell_id} has an invalid polygon_page")
        result.append([_number(point[0], "polygon x"), _number(point[1], "polygon y")])
    # A polygon with zero area cannot be a cell boundary.  We deliberately do
    # not repair or expand it: callers must provide the measured geometry.
    xs = [point[0] for point in result]
    ys = [point[1] for point in result]
    if max(xs) <= min(xs) or max(ys) <= min(ys):
        raise ValueError(f"cell {cell_id} has a degenerate polygon_page")
    return result


def _bbox_from_polygon(polygon: list[list[float]]) -> list[float]:
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def _normalise_bbox(value: Any, *, cell_id: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"cell {cell_id} requires bbox [x0,y0,x1,y1]")
    box = [_number(item, "bbox coordinate") for item in value]
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError(f"cell {cell_id} has a degenerate bbox")
    return box


def _cell_text(cell: Mapping[str, Any]) -> str:
    value = cell.get("raw_text", cell.get("text", cell.get("ocr_text", "")))
    if value is None:
        return ""
    return str(value)


def normalise_cells(cells: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate and normalize measured cells without changing their text.

    ``row_group_id`` is required.  The value is supplied by the geometry/table
    detector and is retained as evidence; the bridge never infers rows from
    vertical distance.  A polygon is also required so page navigation can use
    the actual PDF image location.  ``bbox`` may be omitted when it can be
    derived from that polygon.
    """
    result = []
    seen = set()
    for index, source in enumerate(cells):
        if not isinstance(source, Mapping):
            raise ValueError(f"cell {index} must be an object")
        cell_id = str(source.get("cell_id") or source.get("region_id") or source.get("id") or f"cell-{index + 1}")
        if cell_id in seen:
            raise ValueError(f"duplicate cell_id: {cell_id}")
        seen.add(cell_id)
        if "row_group_id" not in source or source.get("row_group_id") in (None, ""):
            raise ValueError(f"cell {cell_id} requires row_group_id")
        polygon = _polygon(source.get("polygon_page"), cell_id=cell_id)
        derived_bbox = _bbox_from_polygon(polygon)
        # Navigation polygons and table-matching boxes have different axes on
        # rotated/deskewed pages.  The calibrated bbox is validated on its own
        # coordinate frame; it must not be tested against original-page bounds.
        matcher_bbox = source.get("matcher_bbox")
        bbox = _normalise_bbox(source.get("bbox", derived_bbox), cell_id=cell_id)
        if (bbox[0] < min(p[0] for p in polygon) - 1e-6 or
                bbox[1] < min(p[1] for p in polygon) - 1e-6 or
                bbox[2] > max(p[0] for p in polygon) + 1e-6 or
                bbox[3] > max(p[1] for p in polygon) + 1e-6):
            raise ValueError(f"cell {cell_id} bbox is outside polygon_page")
        item = dict(source)
        item.update(cell_id=cell_id, row_group_id=str(source["row_group_id"]),
                    polygon_page=deepcopy(polygon), bbox=bbox, raw_text=_cell_text(source),
                    glm_confidence=None, confidence_known=False)
        if matcher_bbox is not None:
            item["matcher_bbox"] = _normalise_bbox(matcher_bbox, cell_id=cell_id)
            item["matcher_coordinate_system"] = str(source.get(
                "matcher_coordinate_system", "calibrated_input_pixels_top_left"))
        result.append(item)
    if not result:
        raise ValueError("at least one GLM cell is required")
    return result


def build_coordinate_input(cells: Iterable[Mapping[str, Any]], *, table_index: int = 1) -> tuple[dict, list[dict]]:
    """Build the minimal Paddle-shaped JSON accepted by ``coordinate_match``.

    The shape is an adapter contract, not a claim that GLM produced Paddle
    scores.  Every text token has ``None`` as its score and the original cell
    geometry is returned separately for evidence mapping.
    """
    if isinstance(table_index, bool) or int(table_index) < 1:
        raise ValueError("table_index must be a positive integer")
    normalised = normalise_cells(cells)
    cell_boxes = [item.get("matcher_bbox", item["bbox"]) for item in normalised]
    texts = [_cell_text(item) for item in normalised]
    boxes = [item.get("matcher_bbox", item["bbox"]) for item in normalised]
    payload = {
        "res": {
            "table_res_list": [{"cell_box_list": cell_boxes}],
            "overall_ocr_res": {
                "rec_texts": texts,
                "rec_boxes": boxes,
                # Unknown is intentionally represented as None.  Do not use
                # 1.0 here: that would falsely convert GLM output into Paddle
                # high-confidence evidence.
                "rec_scores": [None] * len(texts),
            },
        }
    }
    return payload, normalised


def _matcher_cell_map(result: Mapping[str, Any], cells: list[dict[str, Any]]) -> dict[str, dict]:
    observed = list(result.get("cells", []))
    if len(observed) != len(cells):
        raise ValueError("coordinate matcher changed the cell count; refusing to mis-map evidence")
    return {str(item["cell_id"]): {"glm_cell": source, "matcher_cell": item}
            for item, source in zip(observed, cells)}


def attach_cell_evidence(result: Mapping[str, Any], cells: Iterable[Mapping[str, Any]], *,
                         page_no: int | None = None, pdf_sha256: str | None = None) -> dict:
    """Attach original GLM cell IDs, row groups, and polygons to matcher output."""
    normalised = normalise_cells(cells)
    output = deepcopy(dict(result))
    mapping = _matcher_cell_map(output, normalised)
    by_matcher = {entry["matcher_cell"]["cell_id"]: entry["glm_cell"] for entry in mapping.values()}
    evidence = []
    for matcher_cell, source in zip(output.get("cells", []), normalised):
        evidence.append({"glm_cell_id": source["cell_id"],
                         "matcher_cell_id": matcher_cell["cell_id"],
                         "row_group_id": source["row_group_id"],
                         "polygon_page": deepcopy(source["polygon_page"]),
                         "raw_text": source["raw_text"], "ocr_score": None,
                         "confidence_known": False,
                         "source": {key: deepcopy(source[key]) for key in
                                    ("checkpoint_reference", "checkpoint_sha256", "page_no")
                                    if key in source}})
        for key in ("region_id", "source_pixel_size", "render_dpi", "coordinate_system",
                    "matcher_bbox", "matcher_coordinate_system",
                    "checkpoint_reference", "checkpoint_sha256", "inference_model"):
            if key in source:
                evidence[-1][key] = deepcopy(source[key])
    output["glm_cell_evidence"] = evidence
    if page_no is not None:
        output["page_no"] = int(page_no)
    if pdf_sha256 is not None:
        output["pdf_sha256"] = str(pdf_sha256)
    # Records and fields retain the matcher's field semantics.  Add only
    # provenance; never replace values or mark a field accepted.
    for record in output.get("records", []):
        anchor = by_matcher.get(record.get("anchor_cell_id"))
        if anchor:
            record["row_group_id"] = anchor["row_group_id"]
            record["glm_anchor_cell_id"] = anchor["cell_id"]
            record.setdefault("evidence", {})["polygon_page"] = deepcopy(anchor["polygon_page"])
        for field in record.get("fields", {}).values():
            ev = field.get("evidence") if isinstance(field, Mapping) else None
            if not isinstance(ev, Mapping):
                continue
            source = by_matcher.get(ev.get("cell_id"))
            if source:
                field["glm_cell_id"] = source["cell_id"]
                field["row_group_id"] = source["row_group_id"]
                field["confidence_known"] = False
                field["evidence"]["polygon_page"] = deepcopy(source["polygon_page"])
                field["evidence"]["ocr_score"] = None
    output["glm_coordinate_adapter"] = {
        "model": normalised[0].get("inference_model", "local-glm") if normalised else "local-glm", "confidence_known": False,
        "coordinate_source": "measured_table_cell_polygon",
        "cell_count": len(normalised),
    }
    return output


def analyze_cells(cells: Iterable[Mapping[str, Any]], *, options=None,
                  page_no: int | None = None, pdf_sha256: str | None = None) -> dict:
    """Run the existing coordinate matcher on validated GLM cell evidence."""
    from . import coordinate_match
    payload, normalised = build_coordinate_input(cells)
    if options is None:
        from types import SimpleNamespace
        options = SimpleNamespace(min_coverage=.8, min_margin=.15, min_score=.95)
    result = coordinate_match.analyze(payload, options)
    return attach_cell_evidence(result, normalised, page_no=page_no, pdf_sha256=pdf_sha256)


def _sidecar_maps(result: Mapping[str, Any], checkpoint: Mapping[str, Any]) -> tuple[dict, dict]:
    """Index bridge evidence by matcher cell id and by a legacy region id."""
    by_matcher, by_region = {}, {}
    entries = list(result.get("glm_cell_evidence", []))
    entries.extend(checkpoint.get("cell_evidence", []) if isinstance(checkpoint.get("cell_evidence"), list) else [])
    entries.extend(checkpoint.get("cells", []) if isinstance(checkpoint.get("cells"), list) else [])
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        value = dict(item)
        matcher_id = value.get("matcher_cell_id")
        source_id = value.get("glm_cell_id") or value.get("cell_id") or value.get("region_id")
        if matcher_id:
            by_matcher[str(matcher_id)] = value
        if value.get("region_id"):
            by_region[str(value["region_id"])] = value
        if source_id and not value.get("glm_cell_id"):
            value.setdefault("glm_cell_id", source_id)
    # A result from an older bridge may only contain matcher cells.  Preserve
    # their measured boxes, but do not claim a word-level location.
    for item in result.get("cells", []):
        if not isinstance(item, Mapping) or not item.get("cell_id"):
            continue
        by_matcher.setdefault(str(item["cell_id"]), dict(item, matcher_cell_id=item["cell_id"]))
    return by_matcher, by_region


def _field_cell(field: Mapping[str, Any] | None, by_matcher: Mapping[str, dict],
                by_region: Mapping[str, dict], anchor: Mapping[str, Any] | None = None) -> dict | None:
    evidence = field.get("evidence") if isinstance(field, Mapping) else None
    evidence = evidence if isinstance(evidence, Mapping) else {}
    key = evidence.get("cell_id") or evidence.get("matcher_cell_id")
    source = by_matcher.get(str(key)) if key else None
    if source is None and evidence.get("region_id"):
        source = by_region.get(str(evidence["region_id"]))
    # Never fall back to the row anchor for a field.  A missing certificate
    # cell must remain missing; borrowing the serial/specification polygon
    # would send a reviewer to the wrong place and can attach a certificate
    # to another row.  The anchor is used only for record-level evidence.
    return source


def _cell_evidence(field: Mapping[str, Any] | None, source: Mapping[str, Any] | None,
                   checkpoint: Mapping[str, Any], *, row_group_id: str | None = None) -> dict:
    """Make a page-navigation evidence item for one GLM cell observation."""
    raw_evidence = field.get("evidence") if isinstance(field, Mapping) else {}
    raw_evidence = raw_evidence if isinstance(raw_evidence, Mapping) else {}
    page_no = checkpoint.get("page_no")
    polygon = (source or {}).get("polygon_page") or raw_evidence.get("polygon_page")
    item = {
        "page_no": page_no,
        "polygon_page": deepcopy(polygon),
        "precision": "cell" if polygon else "page",
        "method": "paddle_cell_text" if checkpoint.get("inference_model") == "local-paddle" else "glm_cell_text",
        "inference_model": checkpoint.get("inference_model", "local-glm"),
        "source_role": "inspection",
        "raw_text": str(field.get("raw_text", "")) if isinstance(field, Mapping) else "",
        "ocr_score": None,
        "confidence_known": False,
        "word_box": False,
    }
    for key in ("checkpoint_reference", "checkpoint_sha256", "source_pixel_size", "render_dpi",
                "matcher_bbox", "matcher_coordinate_system",
                "coordinate_system", "pdf_sha256", "model_fingerprint", "inference_model"):
        value = (source or {}).get(key, checkpoint.get(key))
        if value is not None:
            item[key] = deepcopy(value)
    if "source_pixel_size" not in item:
        size = checkpoint.get("original_pixel_size") or checkpoint.get("pixel_size")
        if size:
            item["source_pixel_size"] = deepcopy(size)
    if "render_dpi" not in item and checkpoint.get("dpi"):
        item["render_dpi"] = checkpoint["dpi"]
    if source:
        item["cell_id"] = source.get("glm_cell_id") or source.get("cell_id") or source.get("region_id")
        item["matcher_cell_id"] = source.get("matcher_cell_id")
        item["region_id"] = source.get("region_id")
        if source.get("row_group_id") is not None:
            item["geometry_cell_row_group_id"] = str(source["row_group_id"])
        row_group_id = row_group_id or source.get("row_group_id")
    if row_group_id is not None:
        item["row_group_id"] = str(row_group_id)
    return item


def _value(field: Mapping[str, Any] | None, default=""):
    if not isinstance(field, Mapping):
        return default
    value = field.get("value", default)
    return default if value is None else value


def _tonnes(quantity: Any) -> str | None:
    if not isinstance(quantity, Mapping):
        return str(quantity).strip() if quantity not in (None, "") else None
    raw, unit = quantity.get("value"), str(quantity.get("unit", "")).lower()
    try:
        number = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if unit in ("kg", "千克", "公斤"):
        number /= Decimal(1000)
    elif unit not in ("t", "吨"):
        return None
    return format(number, "f")


def flatten_entry_records(result: Mapping[str, Any], checkpoint: Mapping[str, Any]) -> list[dict]:
    """Convert matcher rows into the GLM ``resources.records`` contract.

    This is a structural adapter only.  It does not select a certificate or
    infer a missing row value.  The existing ``build_glm_ledger`` remains the
    sole implementation of the two matching routes.
    """
    # ``coordinate_match.analyze`` deliberately clears records when table
    # candidates conflict or a product band contains unresolved text.  Keep
    # the page-level GLM candidate in that case; never turn a partial table
    # into replacement ledger rows.
    selection = result.get("table_selection") or {}
    if (result.get("table_issues") or selection.get("issues") or
            selection.get("selected_candidate") is None and selection.get("decision")):
        return []
    page_no = int(checkpoint.get("page_no", result.get("page_no", 0)) or 0)
    if page_no <= 0:
        raise ValueError("checkpoint page_no is required")
    by_matcher, by_region = _sidecar_maps(result, checkpoint)
    output = []
    for index, row in enumerate(result.get("records", []), 1):
        fields = row.get("fields", {}) if isinstance(row, Mapping) else {}
        anchor_source = _field_cell(fields.get("row_no"), by_matcher, by_region, row)
        row_group_id = row.get("row_group_id") or (anchor_source or {}).get("row_group_id")
        if row_group_id is None:
            # The matcher has already established a physical row.  Use its
            # anchor as the stable group identifier; never derive one from
            # neighbouring rows or a fixed row count.
            row_group_id = row.get("anchor_cell_id") or f"matcher-row-{index}"
        raw_cells = {}
        field_evidence = {}
        for name, field in fields.items():
            if not isinstance(field, Mapping):
                continue
            raw_cells[name] = str(field.get("raw_text", ""))
            source = _field_cell(field, by_matcher, by_region)
            field_evidence[name] = _cell_evidence(field, source, checkpoint,
                                                  row_group_id=str(row_group_id))
        specification_field = fields.get("specification", {})
        parsed = specification_field.get("parsed") if isinstance(specification_field, Mapping) else None
        if not isinstance(parsed, Mapping):
            parsed = {}
        grade = str(parsed.get("grade") or row.get("grade") or "")
        diameter = str(parsed.get("diameter_mm") or row.get("diameter_mm") or "")
        # A logical GLM cell result may use the legacy ``weight`` alias or a
        # separate ``grade`` field.  These are still GLM observations and do
        # not import values from the old Paddle records.
        quantity_field = fields.get("quantity") or fields.get("weight") or {}
        certificate_field = (fields.get("certificate_no") or
                             fields.get("quality_certificate_no") or {})
        certificate = str(_value(certificate_field, "") or "").strip()
        record_evidence = field_evidence.get("row_no") or _cell_evidence(
            fields.get("specification"), anchor_source, checkpoint, row_group_id=str(row_group_id))
        issues = list(row.get("issues", []))
        for name, field in fields.items():
            if isinstance(field, Mapping):
                issues.extend(f"{name}:{issue}" for issue in field.get("issues", []))
        source_record_id = str(row.get("record_id") or f"glm_cell_r{index}")
        output.append({
            "record_id": f"p{page_no:03d}:{source_record_id}",
            "source_record_id": source_record_id,
            "page_no": page_no,
            "pdf_sha256": checkpoint.get("pdf_sha256", result.get("pdf_sha256")),
            "row_no": _value(fields.get("row_no"), row.get("row_no")),
            "row_group_id": str(row_group_id),
            "material_name": str(_value(fields.get("material_name"), "") or ""),
            "grade": grade,
            "diameter_mm": diameter,
            "weight_t": _tonnes(_value(quantity_field, None)),
            "certificate_no": certificate,
            "manufacturer": str(_value(fields.get("manufacturer") or
                                        fields.get("manufacturer_certificate"), "") or ""),
            "raw_cells": raw_cells,
            "field_evidence": field_evidence,
            "evidence": record_evidence,
            "issues": sorted(set(str(issue) for issue in issues if issue)),
            "extraction_backend": "glm_coordinate_cell_v1",
            "ready_for_search": False,
        })
    return output


def flatten_coordinate_records(result: Mapping[str, Any], checkpoint: Mapping[str, Any]) -> list[dict]:
    """Public name for the GLM-to-resources record adapter.

    ``flatten_entry_records`` remains as a compatibility alias for existing
    callers.  Both functions fail closed on blocked table candidates and
    preserve each field's own cell geometry, including a certificate cell's
    original page polygon.
    """
    return flatten_entry_records(result, checkpoint)
