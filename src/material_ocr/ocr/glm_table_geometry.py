"""Offline PaddleX geometry for GLM's adaptive table-cell text pipeline.

This module deliberately loads only cached layout, table-kind and cell-box
models. It never loads a text detector, text recognizer, table structure text
recognizer, or Paddle OCR pipeline. Rendered pages and crops remain in memory;
only compact coordinate JSON is persisted.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Iterable

SCHEMA = "glm-table-geometry-v1"
DEFAULT_MODELS = Path("models/paddlex_cache/official_models")
MODEL_NAMES = {
    "layout": "PP-DocLayout-L",
    "table_classification": "PP-LCNet_x1_0_table_cls",
    "wired_cells": "RT-DETR-L_wired_table_cell_det",
    "wireless_cells": "RT-DETR-L_wireless_table_cell_det",
}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _finite_box(value, *, image_shape=None) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in (x1, y1, x2, y2)):
        return None
    if image_shape is not None:
        height, width = image_shape[:2]
        x1, x2 = max(0.0, min(width, x1)), max(0.0, min(width, x2))
        y1, y2 = max(0.0, min(height, y1)), max(0.0, min(height, y2))
    if x2 <= x1 or y2 <= y1:
        return None
    return [x1, y1, x2, y2]


def _iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def _nms_boxes(boxes, threshold=0.3):
    """Drop duplicate detections, keeping higher detector-score geometry."""
    ordered = sorted(boxes, key=lambda item: (-item["score"], item["bbox"]))
    selected = []
    for item in ordered:
        if not any(_iou(item["bbox"], kept["bbox"]) > threshold for kept in selected):
            selected.append(item)
    return selected


def _prediction_boxes(prediction, *, label=None, image_shape=None, threshold=0.3):
    boxes = []
    for raw in (prediction or {}).get("boxes", []):
        if not isinstance(raw, dict) or (label and str(raw.get("label", "")).lower() != label):
            continue
        try:
            score = float(raw.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        bbox = _finite_box(raw.get("coordinate"), image_shape=image_shape)
        if bbox is not None and math.isfinite(score) and score >= threshold:
            boxes.append({"bbox": bbox, "score": score})
    return boxes


def _table_kind(prediction) -> tuple[str, float | None]:
    labels = list(prediction.get("label_names", []))
    scores = list(prediction.get("scores", []))
    if not labels:
        raise ValueError("table classifier returned no labels")
    if scores and len(scores) == len(labels):
        index = max(range(len(labels)), key=lambda i: float(scores[i]))
        label, score = str(labels[index]), float(scores[index])
    else:
        label, score = str(labels[0]), None
    if label not in ("wired_table", "wireless_table"):
        raise ValueError(f"unsupported table classifier label: {label}")
    return label, score


def _transform_polygon(bbox, matrix):
    x1, y1, x2, y2 = bbox
    points = [[x1, y1, 1.0], [x2, y1, 1.0], [x2, y2, 1.0], [x1, y2, 1.0]]
    result = []
    for x, y, w0 in points:
        w = matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * w0
        if not math.isfinite(w) or abs(w) < 1e-12:
            raise ValueError("invalid coordinate calibration transform")
        xx = (matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * w0) / w
        yy = (matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * w0) / w
        if not math.isfinite(xx) or not math.isfinite(yy):
            raise ValueError("cell polygon mapped to non-finite page coordinates")
        result.append([xx, yy])
    return result


def _load_local_models(models_root: str | Path, device: str, cpu_threads: int):
    root = Path(models_root).resolve()
    paths = {key: root / name for key, name in MODEL_NAMES.items()}
    for path in paths.values():
        if not path.is_dir() or not (path / "inference.yml").is_file():
            raise FileNotFoundError(f"required cached PaddleX model is missing: {path}")
    # Explicit local model_dir prevents automatic downloads. Suppress source
    # connectivity probes as well, which are unrelated to local inference.
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    from paddlex import create_model
    from paddlex.inference.utils.pp_option import PaddlePredictorOption

    device_type = "cpu" if device == "cpu" else "gpu"
    option = PaddlePredictorOption(run_mode="paddle", device_type=device_type,
                                   cpu_threads=int(cpu_threads))
    return {
        "layout": create_model(MODEL_NAMES["layout"], model_dir=str(paths["layout"]),
                               device=device, pp_option=option),
        "table_classification": create_model(
            MODEL_NAMES["table_classification"],
            model_dir=str(paths["table_classification"]), device=device, pp_option=option),
        "wired_cells": create_model(MODEL_NAMES["wired_cells"],
                                    model_dir=str(paths["wired_cells"]),
                                    device=device, pp_option=option),
        "wireless_cells": create_model(MODEL_NAMES["wireless_cells"],
                                       model_dir=str(paths["wireless_cells"]),
                                       device=device, pp_option=option),
    }


def _render_page(pdf, page_no: int, dpi: int):
    import numpy as np

    # PDFium objects are not context managers in the supported version.
    # Register cleanup explicitly so every resource closes even if a later
    # render/conversion or close call raises.
    with ExitStack() as cleanup:
        page = pdf[page_no - 1]
        cleanup.callback(page.close)
        bitmap = page.render(scale=float(dpi) / 72.0)
        cleanup.callback(bitmap.close)
        pil_image = bitmap.to_pil()
        cleanup.callback(pil_image.close)
        rgb_image = pil_image.convert("RGB")
        cleanup.callback(rgb_image.close)
        # Copy before releasing all native/PIL buffers. The worker's contract
        # is a normal NumPy array independent of PDFium's bitmap lifetime.
        return np.asarray(rgb_image).copy()


def _calibrate(image, rotation_ccw: int, deskew: bool):
    import cv2
    import numpy as np
    from material_ocr.extraction.adaptive import estimate_skew, rotate_expanded
    from material_ocr.ocr.orientation import rotate_page

    if rotation_ccw not in (0, 90, 180, 270):
        raise ValueError("rotation_ccw must be one of 0, 90, 180, 270")
    original_size = [int(image.shape[1]), int(image.shape[0])]
    upright, coarse = rotate_page(image, rotation_ccw)
    if deskew:
        # estimate_skew was written against OpenCV BGR images.
        skew_info = estimate_skew(cv2.cvtColor(upright, cv2.COLOR_RGB2BGR))
    else:
        skew_info = {"status": "disabled_by_user", "rotation_ccw": 0.0,
                     "requires_review": True, "method": "deskew_disabled"}
    corrected, fine = rotate_expanded(upright, float(skew_info.get("rotation_ccw", 0.0)))
    transform = np.asarray(fine, dtype=float) @ np.asarray(coarse, dtype=float)
    return corrected, {
        "method": "page_orientation_then_optional_small_grid_skew",
        "coarse_rotation_ccw": rotation_ccw,
        "deskew": skew_info,
        "original_size": original_size,
        "input_size": [int(corrected.shape[1]), int(corrected.shape[0])],
        "original_to_input": transform.tolist(),
        "input_to_original": np.linalg.inv(transform).tolist(),
        "coordinate_system": "rendered_page_pixel_centres_top_left; PDF page rotation applied by PDFium",
        "requires_review": bool(skew_info.get("requires_review", False)),
    }


def _cell_grid(cell_items):
    from material_ocr.materials.rebar.coordinate_match import recover_grid

    cells = [{"cell_id": item["cell_id"], "bbox": item["bbox"],
              "tokens": [], "issues": []} for item in cell_items]
    return recover_grid(cells), cells


def _table_geometry(*, page_no: int, table_index: int, page_image, table_box,
                    models, pdf_sha256: str, calibration: dict, model_names=None):
    x1, y1, x2, y2 = map(int, table_box["bbox"])
    crop = page_image[y1:y2, x1:x2]
    if crop.size == 0:
        raise ValueError(f"page {page_no} table {table_index} has an empty crop")
    classification = list(models["table_classification"].predict(crop))
    if len(classification) != 1:
        raise ValueError("table classifier returned an unexpected number of results")
    kind, kind_score = _table_kind(classification[0])
    detector_key = "wired_cells" if kind == "wired_table" else "wireless_cells"
    detections = list(models[detector_key].predict(crop, threshold=0.3))
    if len(detections) != 1:
        raise ValueError("table-cell detector returned an unexpected number of results")
    local = _nms_boxes(_prediction_boxes(detections[0], image_shape=crop.shape, threshold=0.3))
    cells = []
    for index, item in enumerate(sorted(local, key=lambda value: (
            value["bbox"][1], value["bbox"][0], value["bbox"][3], value["bbox"][2]))):
        bx1, by1, bx2, by2 = item["bbox"]
        bbox = [float(x1 + bx1), float(y1 + by1), float(x1 + bx2), float(y1 + by2)]
        cells.append({"cell_id": f"p{page_no:03d}_t{table_index:03d}_c{index + 1:04d}",
                      "bbox": bbox,
                      "polygon_page": _transform_polygon(bbox, calibration["input_to_original"]),
                      # This measures object geometry only; it is not text
                      # recognition confidence and must not gate OCR values.
                      "geometry_detection_score": float(item["score"])})
    grid, grid_cells = _cell_grid(cells)
    for output, calibrated in zip(cells, grid_cells):
        output["grid"] = calibrated["grid"]
        output["geometry_issues"] = calibrated["issues"]
    if not cells:
        grid = {"x_edges": [], "y_edges": [], "issues": ["no_cells"]}
    table_polygon = _transform_polygon(table_box["bbox"], calibration["input_to_original"])
    provenance = {
        "pdf_sha256": pdf_sha256,
        "page_no": int(page_no),
        "table_index": int(table_index),
        "calibration": calibration,
        "geometry_models": model_names or dict(MODEL_NAMES),
        "table_kind": kind,
        "table_class_geometry_score": kind_score,
    }
    return {
        "schema": "adaptive-coordinate-geometry-v1",
        "coordinate_system": "calibrated_input_pixels; polygons in original rendered page pixels",
        "provenance": provenance,
        "table": {"bbox": [float(value) for value in table_box["bbox"]],
                  "polygon_page": table_polygon,
                  "layout_detection_score": float(table_box["score"])},
        "grid": grid,
        "cells": cells,
        # Structural contract fields expected by geometry-plan consumers.
        # These are intentionally empty: geometry has no OCR text or labels.
        "headers": {},
        "records": [],
        "ocr_matches": [],
        "geometry_only": True,
        "text_recognition_performed": False,
    }


def _model_fingerprint(models_root: Path) -> str:
    sources = []
    for name in MODEL_NAMES.values():
        folder = models_root / name
        files = [folder / "inference.yml", folder / "config.json"]
        for path in files:
            if not path.is_file():
                raise FileNotFoundError(f"cached model metadata missing: {path}")
            sources.append([str(path.relative_to(models_root)), sha256_file(path)])
    return hashlib.sha256(canonical_json(sources)).hexdigest()


def detect_pdf_geometry(pdf_path: str | Path, *, pages: Iterable[int],
                        output_root: str | Path,
                        models_root: str | Path = DEFAULT_MODELS,
                        device: str = "cpu", cpu_threads: int = 4,
                        render_dpi: int = 300,
                        rotations: dict[int, int] | None = None,
                        deskew: bool = True) -> dict:
    """Detect table/cell polygons on selected PDF pages; persist JSON only.

    Cache directories are immutable and keyed by the source PDF, the explicit
    page/rotation config, model metadata and detection configuration. PNG/JPEG
    pages and crops are never written.
    """
    source = Path(pdf_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    page_list = sorted(set(int(page) for page in pages))
    if not page_list or page_list[0] < 1:
        raise ValueError("pages must contain positive one-based page numbers")
    if render_dpi < 72 or render_dpi > 600:
        raise ValueError("render_dpi must be between 72 and 600")
    rotations = {int(page): int(angle) for page, angle in (rotations or {}).items()}
    for page, angle in rotations.items():
        if page not in page_list or angle not in (0, 90, 180, 270):
            raise ValueError("rotation keys must be selected pages and angles 0/90/180/270")
    models_root = Path(models_root).resolve()
    pdf_hash = sha256_file(source)
    fingerprint = _model_fingerprint(models_root)
    config = {"schema": SCHEMA, "pdf_sha256": pdf_hash, "pages": page_list,
              "rotations": {str(page): angle for page, angle in rotations.items()},
              "render_dpi": int(render_dpi), "deskew": bool(deskew),
              "model_fingerprint": fingerprint, "device": device,
              "cell_threshold": 0.3, "nms_iou_threshold": 0.3}
    cache_key = hashlib.sha256(canonical_json(config)).hexdigest()
    cache = Path(output_root).resolve() / "documents" / pdf_hash / "geometry" / cache_key
    manifest_path = cache / "manifest.json"
    if manifest_path.is_file():
        cached = json.loads(manifest_path.read_text(encoding="utf-8"))
        if cached.get("config") == config and all(Path(path).is_file()
                for path in cached.get("coordinate_result_paths", [])):
            return cached

    # pypdfium keeps its raster in memory. No intermediate image files exist.
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(source))
    try:
        if page_list[-1] > len(pdf):
            raise ValueError(f"requested page {page_list[-1]} exceeds PDF page count {len(pdf)}")
        models = _load_local_models(models_root, device, cpu_threads)
        results = []
        for page_no in page_list:
            rendered = _render_page(pdf, page_no, render_dpi)
            calibrated, calibration = _calibrate(rendered, rotations.get(page_no, 0), deskew)
            # Drop the original render as soon as calibration has made its own
            # array. This bounds peak memory when several pages are selected.
            del rendered
            layout_results = list(models["layout"].predict(calibrated, threshold=0.3))
            if len(layout_results) != 1:
                raise ValueError(f"layout detector returned an unexpected result for page {page_no}")
            table_boxes = _nms_boxes(_prediction_boxes(layout_results[0], label="table",
                                                      image_shape=calibrated.shape, threshold=0.3))
            table_boxes.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
            for table_index, box in enumerate(table_boxes, start=1):
                result = _table_geometry(page_no=page_no, table_index=table_index,
                                         page_image=calibrated, table_box=box,
                                         models=models, pdf_sha256=pdf_hash,
                                         calibration=calibration)
                path = cache / "pages" / f"page_{page_no:04d}" / f"table_{table_index:03d}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                results.append(str(path))
            if not table_boxes:
                # Keep calibration and the no-table fact auditable without writing
                # an empty geometry file that downstream would mistake for a plan.
                page_manifest = cache / "pages" / f"page_{page_no:04d}" / "page.json"
                page_manifest.parent.mkdir(parents=True, exist_ok=True)
                page_manifest.write_text(json.dumps({"pdf_sha256": pdf_hash, "page_no": page_no,
                    "calibration": calibration, "tables_detected": 0,
                    "reason": "layout_detector_found_no_table_regions"}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            del calibrated, layout_results, table_boxes
        manifest = {"schema": SCHEMA, "config": config,
                    "cache_directory": str(cache), "coordinate_result_paths": results,
                    "table_count": len(results),
                    "image_persistence": "none; rendered pages and table crops remain in memory"}
        cache.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest
    finally:
        close = getattr(pdf, "close", None)
        if close is not None:
            close()


def _parse_pages(value: str) -> list[int]:
    pages = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = (int(item) for item in part.split("-", 1))
            if right < left:
                raise argparse.ArgumentTypeError("page range end must not precede start")
            pages.extend(range(left, right + 1))
        else:
            pages.append(int(part))
    return sorted(set(pages))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--pages", type=_parse_pages, required=True,
                        help="one-based pages, e.g. 2,5-6")
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--manifest-output", type=Path,
                        help="also write the returned manifest to this JSON path")
    parser.add_argument("--models-root", default=str(DEFAULT_MODELS))
    parser.add_argument("--device", default="cpu", help="cpu, gpu:0, etc.")
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--render-dpi", type=int, default=300)
    parser.add_argument("--rotation", action="append", default=[], metavar="PAGE:DEGREES")
    parser.add_argument("--no-deskew", action="store_true")
    args = parser.parse_args(argv)
    rotations = {}
    for entry in args.rotation:
        page, angle = entry.split(":", 1)
        rotations[int(page)] = int(angle)
    output = detect_pdf_geometry(args.pdf, pages=args.pages, output_root=args.output_root,
                                 models_root=args.models_root, device=args.device,
                                 cpu_threads=args.cpu_threads, render_dpi=args.render_dpi,
                                 rotations=rotations, deskew=not args.no_deskew)
    if args.manifest_output is not None:
        args.manifest_output.parent.mkdir(parents=True, exist_ok=True)
        args.manifest_output.write_text(json.dumps(output, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
