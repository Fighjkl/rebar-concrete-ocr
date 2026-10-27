"""Adaptive entry-table backend for the existing v7.1 batch runner.

Does not route PDF pages, replace cover extraction, or read generated HTML.
Coordinates always refer to the saved, calibrated input.png. The inverse
transform maps pixel centres back to the original rendered page (not PDF points).
"""
import hashlib
import importlib.metadata
import json
import math
import re
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from material_ocr.upstream_legacy.ocr.orientation import rotate_page
from material_ocr.upstream_legacy.materials.rebar.entry_schema import parse_specification, OPTIONAL_ALIASES


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def image_read(path):
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot read image: {path}")
    return image


def image_write(path, image):
    ok, data = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("PNG encoding failed")
    data.tofile(str(path))


def estimate_skew(image):
    """Use long orthogonal grid lines, never a fixed row/column template."""
    h, w = image.shape[:2]
    scale = min(1., 1600 / max(h, w))
    small = cv2.resize(image, None, fx=scale, fy=scale) if scale < 1 else image
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 60, 180)
    sh, sw = gray.shape
    segments = cv2.HoughLinesP(edges, 1, np.pi/720, 70,
                              minLineLength=max(30, int(min(sh, sw)*.20)), maxLineGap=12)
    horizontal, vertical = [], []
    if segments is not None:
        for x1, y1, x2, y2 in segments[:, 0]:
            dx, dy = float(x2-x1), float(y2-y1)
            angle = (math.degrees(math.atan2(dy, dx))+90) % 180-90
            if abs(angle) <= 10 and abs(dx) >= sw*.25:
                horizontal.append(angle)
            elif abs(angle) >= 80 and abs(dy) >= sh*.18:
                vertical.append(angle-90 if angle > 0 else angle+90)
    info = {"method": "long_grid_line_consensus", "horizontal_line_count": len(horizontal),
            "vertical_line_count": len(vertical), "rotation_ccw": 0., "requires_review": False}
    if len(horizontal) < 4 or len(vertical) < 3:
        return {**info, "status": "insufficient_grid_lines", "requires_review": True}
    hm, vm = float(np.median(horizontal)), float(np.median(vertical))
    hdev = float(np.quantile(np.abs(np.array(horizontal)-hm), .8))
    vdev = float(np.quantile(np.abs(np.array(vertical)-vm), .8))
    angle = (hm+vm)/2
    info.update(horizontal_angle=hm, vertical_angle=vm,
                horizontal_deviation=hdev, vertical_deviation=vdev, estimated_angle=angle)
    if abs(hm-vm) > .75 or max(hdev, vdev) > .75 or abs(angle) > 5:
        return {**info, "status": "inconsistent_or_large_skew", "requires_review": True}
    if abs(angle) < .25:
        return {**info, "status": "already_near_upright"}
    return {**info, "status": "small_skew_corrected", "rotation_ccw": angle}


def rotate_expanded(image, angle):
    if angle == 0:
        return image.copy(), np.eye(3)
    h, w = image.shape[:2]
    matrix = cv2.getRotationMatrix2D(((w-1)/2, (h-1)/2), angle, 1.)
    corners = np.array([[0,0,1],[w-1,0,1],[w-1,h-1,1],[0,h-1,1]], dtype=float)
    mapped = corners @ matrix.T
    low, high = np.floor(mapped.min(axis=0)), np.ceil(mapped.max(axis=0))
    matrix[:, 2] -= low
    size = tuple(int(v) for v in high-low+1)
    result = cv2.warpAffine(image, matrix, size, flags=cv2.INTER_CUBIC, borderValue=(255,255,255))
    return result, np.vstack([matrix, [0,0,1]])


def calibrate_page(folder, rotation, deskew=True):
    folder = Path(folder)
    out = folder / "adaptive"
    out.mkdir(exist_ok=False)
    original = image_read(folder / "page.png")
    upright, coarse_matrix = rotate_page(original, rotation)
    info = estimate_skew(upright) if deskew else {
        "status": "disabled_by_user", "rotation_ccw": 0., "requires_review": True}
    corrected, fine_matrix = rotate_expanded(upright, info["rotation_ccw"])
    transform = fine_matrix @ coarse_matrix
    image_write(out / "input.png", corrected)
    meta = {"method": "page_orientation_then_optional_small_grid_skew",
            "coarse_rotation_ccw": rotation, "deskew": info,
            "original_size": [original.shape[1], original.shape[0]],
            "input_size": [corrected.shape[1], corrected.shape[0]],
            "original_to_input": transform.tolist(), "input_to_original": np.linalg.inv(transform).tolist(),
            "coordinate_system": "300dpi rendered-page pixel centres; PDF metadata rotation already applied by renderer",
            "input_image_sha256": hashlib.sha256((out/"input.png").read_bytes()).hexdigest(),
            "requires_review": info["requires_review"], "geometry_review_status": "pending_human_review",
            "note": "No fixed template, fixed row count, perspective correction or cell-image orientation classification."}
    save_json(out / "calibration.json", meta)
    return meta


def original_polygon(box, inverse):
    x1,y1,x2,y2 = box
    points = np.array([[x1,y1,1],[x2,y1,1],[x2,y2,1],[x1,y2,1]], dtype=float) @ np.array(inverse).T
    return (points[:,:2]/points[:,2:]).tolist()


def attach_sources(result, folder, page_no, pdf_sha, calibration):
    """Keep v2.1 evidence and add legacy search keys without guessing values."""
    for record in result["records"]:
        record["source_record_id"] = record["record_id"]
        record["record_id"] = f"p{page_no:03d}:{record['record_id']}"
        record.update(page_no=page_no, pdf_sha256=pdf_sha,
                      geometry_review_status="pending_human_review", extraction_backend="adaptive_v2_1")
        record["source"] = {"page_no": page_no, "pdf_sha256": pdf_sha,
                            "calibration": f"pages/page_{page_no:03d}/adaptive/calibration.json",
                            "inference_result": f"pages/page_{page_no:03d}/adaptive/result.json",
                            "coordinate_system": "calibrated_input_pixels"}
        fields = record["fields"]
        record["row_no"] = fields["row_no"]["value"]
        record["material_name"] = fields["material_name"]["value"]
        record["manufacturer"] = fields["manufacturer"]["value"]
        record["certificate_no"] = fields["certificate_no"]["value"]
        spec = fields["specification"]["value"] or ""
        parsed = parse_specification(spec)
        record["diameter_mm"] = parsed["diameter_mm"] if parsed else None
        record["grade"] = parsed["grade"] if parsed else None
        record["certificate_no_source_status"] = fields["certificate_no"].get(
            "source_status", "extracted" if record["certificate_no"] else "not_detected")
        for name in OPTIONAL_ALIASES:
            if name in fields:
                record[name] = fields[name]["value"]
        quantity = fields["quantity"]["value"]
        record["weight_t"] = (str(Decimal(quantity["value"])/(1000 if quantity["unit"] == "kg" else 1))
                              if isinstance(quantity, dict) and quantity["unit"] in ("t","kg") else None)
        for field in fields.values():
            for evidence_key in ("evidence", "unit_header_evidence", "header_evidence"):
                evidence = field.get(evidence_key)
                if evidence:
                    evidence["rendered_page_polygon"] = original_polygon(evidence["cell_bbox"], calibration["input_to_original"])
        # Preserve the current workbench's inspection/ledger evidence contract.
        # Its polygons refer to the ORIGINAL rendered page, not calibrated input.
        aliases = {"row_no":"row_no", "material_name":"material_name",
                   "specification":"specification", "weight":"quantity",
                   "manufacturer":"manufacturer", "certificate_no":"certificate_no"}
        aliases.update({name:name for name in OPTIONAL_ALIASES if name in fields})
        record["raw_cells"] = {name:fields[key]["raw_text"] for name,key in aliases.items()}
        record["effective_cells"] = dict(record["raw_cells"])
        record["source_cells"] = {}
        record["field_evidence"] = {}
        for name,key in aliases.items():
            evidence = fields[key].get("evidence")
            if not evidence:
                continue
            record["source_cells"][name] = evidence["cell_id"]
            record["field_evidence"][name] = {
                "cell_id":evidence["cell_id"], "polygon_page":evidence["rendered_page_polygon"],
                "raw_text":fields[key]["raw_text"], "source_pixel_size":calibration["original_size"],
                "coordinate_system":"original_300dpi_page_pixels_top_left", "render_dpi":300,
                "issues":list(fields[key]["issues"]),
                "blocks":[{"text":token["text"], "score":token["ocr_score"],
                           "polygon_page":original_polygon(token["bbox"],calibration["input_to_original"])}
                          for token in evidence["ocr_tokens"]]}
        if calibration["requires_review"]:
            record["issues"].append("calibration_requires_review")
            record["status"] = "needs_review"
        record["search_fields_status"] = record["status"]
        record["ready_for_search"] = False
    result["summary"]["needs_review_count"] = sum(r["status"] == "needs_review" for r in result["records"])
    result["summary"]["business_issue_record_count"] = sum(
        any(not issue.startswith("row_no:low_ocr_score:") for issue in r["issues"])
        for r in result["records"])
    result["summary"]["calibration_requires_review"] = calibration["requires_review"]
    if calibration["requires_review"]:
        result["summary"]["status"] = "needs_review"
    for blank in result["blank_candidates"]:
        blank.update(page_no=page_no, pdf_sha256=pdf_sha)
    return result


class AdaptiveBackend:
    def __init__(self, device):
        self.device = device
        self.pipeline = None
        self.initialization_error = None

    def extract(self, folder, page_no, pdf_sha, calibration):
        from material_ocr.upstream_legacy.materials.rebar import coordinate_match as matcher
        from material_ocr.upstream_legacy.extraction.table_model_config import model_config
        folder = Path(folder)
        out = folder / "adaptive"
        if self.initialization_error:
            raise RuntimeError(self.initialization_error)
        if self.pipeline is None:
            print("Initializing adaptive table pipeline once for this PDF...", flush=True)
            started = time.perf_counter()
            try:
                from paddleocr import TableRecognitionPipelineV2
                self.pipeline = TableRecognitionPipelineV2(paddlex_config=model_config(layout=True), device=self.device)
            except Exception as exc:
                self.initialization_error = f"Adaptive model initialization failed: {exc}"
                raise
            self.initialization_seconds = time.perf_counter()-started
        options = {"use_table_orientation_classify": False, "use_wired_table_cells_trans_to_html": True,
                   "use_wireless_table_cells_trans_to_html": True, "use_ocr_results_with_table_cells": True}
        if hashlib.sha256((out/"input.png").read_bytes()).hexdigest() != calibration["input_image_sha256"]:
            raise ValueError("Calibrated image changed before inference")
        started = time.perf_counter()
        predictions = list(self.pipeline.predict(str(out/"input.png"), **options))
        seconds = time.perf_counter()-started
        if len(predictions) != 1 or not hasattr(predictions[0], "save_to_json"):
            raise ValueError("Table pipeline did not return exactly one page result")
        predictions[0].save_to_json(str(out/"result.json"))
        versions = {}
        for name in ("paddleocr", "paddlex", "paddlepaddle-gpu", "pypdfium2"):
            try:
                versions[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                pass
        save_json(out/"inference_summary.json", {"versions":versions, "device":self.device,
            "predict_seconds": seconds, "initialization_seconds_once_per_pdf": self.initialization_seconds,
            "input_image_sha256": calibration["input_image_sha256"], "predict_options":options})
        source = json.loads((out/"result.json").read_text(encoding="utf-8-sig"))
        started = time.perf_counter()
        result = matcher.analyze(source, SimpleNamespace(min_coverage=.8,min_margin=.15,min_score=.95))
        result = attach_sources(result, folder, page_no, pdf_sha, calibration)
        result["provenance"] = {"source_result":str((out/"result.json").resolve()),
            "source_result_sha256":hashlib.sha256((out/"result.json").read_bytes()).hexdigest(),
            "input_image_sha256":calibration["input_image_sha256"], "image_verified":True,
            "pdf_sha256":pdf_sha, "page_no":page_no, "calibration":calibration}
        result["summary"]["offline_processing_seconds"] = time.perf_counter()-started
        matcher.write_json(out/"coordinate_result.json", result)
        matcher.write_json(out/"records.json", {k:result[k] for k in
            ("schema_version","production_ready","provenance","table_selection","records","blank_candidates","table_issues")})
        matcher.write_json(out/"summary.json", result["summary"])
        return result
