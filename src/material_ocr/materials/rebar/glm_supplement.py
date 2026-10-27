"""Targeted GLM OCR supplements driven by legacy table geometry only.

The old adaptive-table output is accepted as a source of page polygons and
pixel-coordinate metadata. No Paddle OCR value, token, score, record identifier,
or inferred field content is copied into a GLM supplement. Rendered pages and
crops stay in memory; only compact JSON sidecars are persisted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Iterable

from material_ocr.ocr.glm import sha256
from material_ocr.ocr.backends import DEFAULT_OCR_MODEL, get_backend, model_ids

SCHEMA = "glm-cell-supplement-v1"
PROMPT = "Text Recognition:"
# Only geometries are retained. These names are keys into a geometry evidence
# map, not imported OCR observations.
# These are the fields that the adaptive table matcher already exposes as
# physical cells.  The values are deliberately *not* copied into a plan.
GEOMETRY_FIELDS = (
    "row_no", "material_name", "specification", "grade", "weight",
    "manufacturer", "certificate_no", "table_cell",
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _polygon(value) -> list[list[float]] | None:
    if not isinstance(value, list) or len(value) < 3:
        return None
    result = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            return None
        if x < 0 or y < 0:
            return None
        result.append([x, y])
    if max(x for x, _ in result) <= min(x for x, _ in result):
        return None
    if max(y for _, y in result) <= min(y for _, y in result):
        return None
    return result


def _geometry_key(page_no: int, polygon: list[list[float]]) -> str:
    normalized = [[round(x, 2), round(y, 2)] for x, y in polygon]
    return hashlib.sha256(_canonical_json([page_no, normalized])).hexdigest()[:20]


def _homography_polygon(box, matrix, *, cell_index: int) -> list[list[float]]:
    """Map a calibrated input-image bbox to original rendered-page pixels.

    ``coordinate_result.json`` stores cell boxes in the calibrated input image
    coordinate system.  The calibration inverse is a homogeneous 3x3 matrix;
    applying it to all four corners preserves skew/rotation instead of
    replacing the measured cell with a guessed axis-aligned box.
    """
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError(f"coordinate cell {cell_index} requires bbox [x1,y1,x2,y2]")
    try:
        x1, y1, x2, y2 = (float(value) for value in box)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"coordinate cell {cell_index} has a non-numeric bbox") from exc
    if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
        raise ValueError(f"coordinate cell {cell_index} has a non-finite bbox")
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"coordinate cell {cell_index} has a degenerate bbox")
    if not isinstance(matrix, (list, tuple)) or len(matrix) != 3 or any(
            not isinstance(row, (list, tuple)) or len(row) != 3 for row in matrix):
        raise ValueError("coordinate provenance requires a 3x3 input_to_original matrix")
    try:
        m = [[float(value) for value in row] for row in matrix]
    except (TypeError, ValueError) as exc:
        raise ValueError("coordinate input_to_original contains a non-numeric value") from exc
    corners = ((x1, y1), (x2, y1), (x2, y2), (x1, y2))
    output = []
    for x, y in corners:
        w = m[2][0] * x + m[2][1] * y + m[2][2]
        if not w or not math.isfinite(w):
            raise ValueError(f"coordinate cell {cell_index} maps through an invalid homography")
        xx = (m[0][0] * x + m[0][1] * y + m[0][2]) / w
        yy = (m[1][0] * x + m[1][1] * y + m[1][2]) / w
        if not (math.isfinite(xx) and math.isfinite(yy)):
            raise ValueError(f"coordinate cell {cell_index} maps to a non-finite point")
        output.append([xx, yy])
    return output


def _coordinate_render_dpi(value: dict, default: int) -> int:
    """Read DPI from the calibration metadata without reading OCR payloads."""
    calibration = ((value.get("provenance") or {}).get("calibration") or {})
    # Existing adaptive pages encode this in a descriptive coordinate-system
    # string.  A future producer may provide an explicit dpi field instead.
    candidate = calibration.get("render_dpi") or calibration.get("dpi") or value.get("render_dpi")
    if candidate is None:
        match = re.search(r"(?:^|[^0-9])([0-9]{2,4})\s*dpi", str(calibration.get("coordinate_system", "")), re.I)
        candidate = match.group(1) if match else default
    try:
        dpi = int(candidate)
    except (TypeError, ValueError) as exc:
        raise ValueError("coordinate result has an invalid render DPI") from exc
    if dpi <= 0:
        raise ValueError("coordinate result has an invalid render DPI")
    return dpi


def _coordinate_result_geometry(value: dict, *, expected_pdf_sha256: str,
                                render_dpi: int = 300) -> list[dict]:
    """Convert every top-level coordinate-result cell into geometry only.

    The adaptive result contains Paddle text, tokens, scores and records beside
    its measured cells.  This function intentionally never inspects those
    fields.  Cell order is the only source of a generated region identifier;
    row ownership comes solely from each cell's measured ``grid`` range.
    """
    provenance = value.get("provenance") or {}
    if provenance.get("pdf_sha256") != expected_pdf_sha256:
        raise ValueError("Coordinate geometry source PDF SHA256 does not match the GLM source")
    page_no = provenance.get("page_no", value.get("page_no"))
    try:
        page_no = int(page_no)
    except (TypeError, ValueError) as exc:
        raise ValueError("coordinate result provenance requires page_no") from exc
    calibration = provenance.get("calibration") or {}
    original_size = calibration.get("original_size")
    if (not isinstance(original_size, (list, tuple)) or len(original_size) != 2 or
            any(not isinstance(item, (int, float)) or item <= 0 for item in original_size)):
        raise ValueError("coordinate result calibration requires positive original_size")
    matrix = calibration.get("input_to_original")
    identity = (isinstance(matrix, (list, tuple)) and len(matrix) == 3 and
        all(isinstance(row, (list, tuple)) and len(row) == 3 for row in matrix) and
        all(float(matrix[row][column]) == (1.0 if row == column else 0.0)
            for row in range(3) for column in range(3)))
    dpi = _coordinate_render_dpi(value, render_dpi)
    regions = []
    for index, cell in enumerate(value.get("cells") or []):
        if not isinstance(cell, dict):
            raise ValueError(f"coordinate cell {index} must be an object")
        grid = cell.get("grid")
        if not isinstance(grid, dict):
            raise ValueError(f"coordinate cell {index} requires grid row range")
        try:
            row_start, row_end = int(grid["row_start"]), int(grid["row_end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"coordinate cell {index} has invalid grid row range") from exc
        if row_start < 0 or row_end <= row_start:
            raise ValueError(f"coordinate cell {index} has a degenerate grid row range")
        polygon = _homography_polygon(cell.get("bbox"), matrix, cell_index=index)
        key = _geometry_key(page_no, polygon)
        region = dict(
            region_id=f"p{page_no:03d}-coordinate-cell-{index:04d}-{key}",
            page_no=page_no, field_name="table_cell",
            row_group_id=f"p{page_no:03d}-grid-r{row_start}-{row_end}",
            polygon_page=polygon,
            source_pixel_size=[int(original_size[0]), int(original_size[1])],
            render_dpi=dpi,
            coordinate_system="original_rendered_page_pixels_top_left",
            geometry_sequence=index + 1,
            geometry_sequence_count=len(value.get("cells") or []),
        )
        if not identity:
            # The original-page polygon serves PDF navigation.  Row/column
            # matching must stay in the calibrated input image's axes, where
            # the measured table cells are upright.  This extra metadata is
            # intentionally absent for identity transforms, keeping previous
            # plans and their cache identities unchanged.
            region.update(matcher_bbox=[float(number) for number in cell["bbox"]],
                matcher_coordinate_system="calibrated_input_pixels_top_left")
        regions.append(region)
    return regions


def load_coordinate_geometry_plan(paths, *, expected_pdf_sha256: str,
                                  render_dpi: int = 300) -> dict:
    """Build a GLM supplement plan from one or more coordinate-result files.

    Only measured cell bboxes, grid row ranges and calibration metadata are
    read.  Paddle values, header semantics, OCR tokens, scores and record IDs
    are deliberately excluded from the returned plan.
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    all_regions = []
    for path in paths:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        all_regions.extend(_coordinate_result_geometry(
            value, expected_pdf_sha256=expected_pdf_sha256, render_dpi=render_dpi))
    if not all_regions:
        raise ValueError("coordinate result contains no measured cells")
    # A page may be supplied more than once (for example, an old retry).  Keep
    # one geometry observation for each generated physical-cell key.
    unique = {}
    for region in all_regions:
        unique.setdefault(region["region_id"], region)
    regions = sorted(unique.values(), key=lambda region: (
        region["page_no"], region["geometry_sequence"], region["region_id"]))
    body = dict(schema_version=SCHEMA, pdf_sha256=expected_pdf_sha256,
                source_kind="coordinate_result_geometry_only", regions=regions)
    body["plan_sha256"] = hashlib.sha256(_canonical_json(body)).hexdigest()
    return body


def build_geometry_plan(records_document: dict, *, expected_pdf_sha256: str,
                        pages: Iterable[int] | None = None,
                        fields: Iterable[str] | None = None) -> dict:
    """Extract physical table-cell polygons from an old adaptive result.

    This deliberately ignores all record values, raw cells, token text, scores,
    row numbers and record IDs. Duplicate polygons are collapsed, then ordered
    top-to-bottom to provide stable geometry-derived sequence metadata.
    """
    allowed_pages = set(map(int, pages)) if pages is not None else None
    selected_fields = set(fields) if fields is not None else set(GEOMETRY_FIELDS)
    unknown_fields = selected_fields - set(GEOMETRY_FIELDS)
    if unknown_fields:
        raise ValueError(f"Unsupported adaptive cell geometry fields: {sorted(unknown_fields)}")
    if isinstance(records_document.get("cells"), list) and isinstance(records_document.get("provenance"), dict):
        regions = _coordinate_result_geometry(records_document, expected_pdf_sha256=expected_pdf_sha256)
        regions = [region for region in regions if "table_cell" in selected_fields
                   and (allowed_pages is None or region["page_no"] in allowed_pages)]
        body = dict(schema_version=SCHEMA, pdf_sha256=expected_pdf_sha256,
                    source_kind="coordinate_result_geometry_only", regions=regions)
        body["plan_sha256"] = hashlib.sha256(_canonical_json(body)).hexdigest()
        return body
    if records_document.get("pdf_sha256") != expected_pdf_sha256:
        raise ValueError("Adaptive geometry source PDF SHA256 does not match the GLM source")
    collected: dict[int, dict[str, dict]] = {}
    for record in records_document.get("records", []):
        try:
            page_no = int(record["page_no"])
        except (KeyError, TypeError, ValueError):
            continue
        if allowed_pages is not None and page_no not in allowed_pages:
            continue
        evidence_map = record.get("field_evidence") or {}
        nested_fields = record.get("fields") or {}
        aliases = {"weight": ("weight", "quantity"),
                   "certificate_no": ("certificate_no", "quality_certificate_no"),
                   "manufacturer": ("manufacturer", "manufacturer_certificate"),
                   "row_no": ("row_no",), "material_name": ("material_name",),
                   "specification": ("specification",), "grade": ("grade",),
                   "table_cell": ("table_cell",)}
        geometries = {}
        for name in GEOMETRY_FIELDS:
            evidence = next((evidence_map.get(alias) for alias in aliases[name]
                             if evidence_map.get(alias)), None)
            if not evidence:
                field = next((nested_fields.get(alias) for alias in aliases[name]
                              if nested_fields.get(alias)), None)
                evidence = (field or {}).get("evidence") or {}
            polygon = _polygon(evidence.get("polygon_page") or evidence.get("rendered_page_polygon"))
            if not polygon:
                continue
            source_size = (evidence.get("source_pixel_size") or record.get("source_pixel_size") or
                           record.get("pixel_size"))
            dpi = evidence.get("render_dpi") or record.get("render_dpi") or record.get("dpi")
            if (not isinstance(source_size, list) or len(source_size) != 2 or
                    any(not isinstance(v, (int, float)) or v <= 0 for v in source_size) or
                    not isinstance(dpi, (int, float)) or dpi <= 0):
                continue
            geometries[name] = (evidence, polygon, source_size, dpi)
        if not geometries:
            continue
        # A row group ID is a digest of its set of physical polygons, never an
        # old record ID or a decoded row value.
        geometry_signature = sorted(
            (name, _geometry_key(page_no, item[1])) for name, item in geometries.items())
        row_key = f"p{page_no:03d}-rowgeom-{hashlib.sha256(_canonical_json(geometry_signature)).hexdigest()[:16]}"
        for field_name in selected_fields:
            item = geometries.get(field_name)
            if not item:
                continue
            evidence, polygon, source_size, dpi = item
            # Quantity backends call this field ``quantity``; old template
            # records call it ``weight``.  Keep both as independent geometry
            # candidates and let the joiner choose the field it expects.
            key = f"{field_name}:{_geometry_key(page_no, polygon)}"
            collected.setdefault(page_no, {}).setdefault(key, dict(
                region_id=f"p{page_no:03d}-legacy-cell-{key.replace(':', '-')}", page_no=page_no,
                field_name=field_name, row_group_id=row_key, polygon_page=polygon,
                source_pixel_size=[int(source_size[0]), int(source_size[1])],
                render_dpi=int(dpi), coordinate_system="original_rendered_page_pixels_top_left"))
    planned = []
    for page_no in sorted(collected):
        regions = sorted(collected[page_no].values(), key=lambda r: (
            sum(p[1] for p in r["polygon_page"]) / len(r["polygon_page"]),
            sum(p[0] for p in r["polygon_page"]) / len(r["polygon_page"]),
            r["field_name"], r["region_id"]))
        for index, region in enumerate(regions, 1):
            region["geometry_sequence"] = index
            region["geometry_sequence_count"] = len(regions)
        planned.extend(regions)
    body = dict(schema_version=SCHEMA, pdf_sha256=expected_pdf_sha256,
                source_kind="legacy_adaptive_geometry_only", regions=planned)
    body["plan_sha256"] = hashlib.sha256(_canonical_json(body)).hexdigest()
    return body


def load_geometry_plan(path: Path, *, expected_pdf_sha256: str,
                       pages: Iterable[int] | None = None,
                       fields: Iterable[str] | None = None) -> dict:
    """Read a prior Paddle/adaptive records file and discard its OCR values."""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(value.get("cells"), list) and isinstance(value.get("provenance"), dict):
        plan = load_coordinate_geometry_plan(path, expected_pdf_sha256=expected_pdf_sha256)
        if pages is not None:
            allowed = set(map(int, pages))
            plan["regions"] = [r for r in plan["regions"] if r["page_no"] in allowed]
        if fields is not None and "table_cell" not in set(fields):
            plan["regions"] = []
        plan["plan_sha256"] = hashlib.sha256(_canonical_json(
            {k: v for k, v in plan.items() if k != "plan_sha256"})).hexdigest()
        return plan
    return build_geometry_plan(value, expected_pdf_sha256=expected_pdf_sha256,
                               pages=pages, fields=fields)


def _clip_box(polygon, width: int, height: int) -> list[int] | None:
    left = max(0, int(round(min(x for x, _ in polygon))))
    top = max(0, int(round(min(y for _, y in polygon))))
    right = min(width, int(round(max(x for x, _ in polygon))))
    bottom = min(height, int(round(max(y for _, y in polygon))))
    if right <= left or bottom <= top:
        return None
    return [left, top, right, bottom]


def _sidecar_path(cache_root: Path, pdf_sha256: str, plan_sha256: str, page_no: int) -> Path:
    return (Path(cache_root) / "documents" / pdf_sha256 / "supplements" / plan_sha256 /
            "pages" / f"page_{page_no:04d}.json")


def _sidecar_valid(value: dict, *, pdf_sha256: str, model_fingerprint: str,
                   plan_sha256: str, page_no: int, prompt: str,
                   max_new_tokens: int) -> bool:
    return (value.get("schema_version") == SCHEMA and value.get("pdf_sha256") == pdf_sha256
            and value.get("model_fingerprint") == model_fingerprint
            and value.get("plan_sha256") == plan_sha256 and value.get("page_no") == page_no
            and value.get("task_prompt") == prompt
            and int(value.get("max_new_tokens", 0)) == max_new_tokens
            and isinstance(value.get("regions"), list))


def _observation_geometry_key(region: dict) -> str | None:
    """Identify the actual GLM crop independently of a plan or field label."""
    polygon = region.get("polygon_page")
    size = region.get("source_pixel_size")
    dpi = region.get("render_dpi")
    if not polygon or not size or not dpi:
        return None
    identity = [polygon, size, dpi]
    if region.get('rotation_ccw'):
        identity.append(int(region['rotation_ccw']))
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def _reusable_page_observations(cache_root: Path, *, pdf_sha256: str,
                                model_fingerprint: str, page_no: int,
                                prompt: str, max_new_tokens: int) -> dict:
    """Read only GLM sidecars with the same source and inference settings."""
    result = {}
    root = Path(cache_root) / "documents" / pdf_sha256 / "supplements"
    for path in sorted(root.glob(f"*/pages/page_{page_no:04d}.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not _sidecar_valid(value, pdf_sha256=pdf_sha256,
                    model_fingerprint=model_fingerprint,
                    plan_sha256=value.get("plan_sha256"), page_no=page_no,
                    prompt=prompt, max_new_tokens=max_new_tokens):
                continue
            for region in value["regions"]:
                key = _observation_geometry_key(region)
                if key and isinstance(region.get("raw_text"), str):
                    result.setdefault(key, region)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return result


def _write_sidecar(path: Path, *, pdf_sha256: str, model_fingerprint: str,
                   plan_sha256: str, page_no: int, dpi: int, prompt: str,
                   max_new_tokens: int, regions: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(dict(schema_version=SCHEMA,
        pdf_sha256=pdf_sha256, model_fingerprint=model_fingerprint,
        plan_sha256=plan_sha256, page_no=page_no, dpi=dpi,
        task_prompt=prompt, max_new_tokens=max_new_tokens,
        regions=regions), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def recognize_geometry_plan(pdf: Path, plan: dict, *, cache_root: Path,
                            model_dir: Path, model_fingerprint: str,
                            python_device: str = "cuda:0", prompt: str = PROMPT,
                            max_new_tokens: int = 1024, ocr_model: str = DEFAULT_OCR_MODEL) -> list[Path]:
    """Run GLM only on listed cell crops and cache text-only sidecars.

    The function is intended to run in the isolated GLM environment. It never
    saves a rendered page, crop, or second copy of the source PDF.
    """
    import pypdfium2
    GlmOcrEngine = get_backend(ocr_model).engine_class()

    pdf = Path(pdf).resolve()
    digest = sha256(pdf)
    if digest != plan.get("pdf_sha256"):
        raise ValueError("Supplement plan belongs to another PDF")
    plan_digest = plan.get("plan_sha256")
    if not plan_digest:
        raise ValueError("Supplement plan has no stable digest")
    grouped: dict[int, list[dict]] = {}
    for region in plan.get("regions", []):
        grouped.setdefault(int(region["page_no"]), []).append(region)
    if not grouped:
        return []

    output_paths = []
    engine = None
    document = pypdfium2.PdfDocument(pdf)
    try:
        for page_no in sorted(grouped):
            path = _sidecar_path(cache_root, digest, plan_digest, page_no)
            existing_regions = {}
            if path.is_file():
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    if _sidecar_valid(saved, pdf_sha256=digest,
                                      model_fingerprint=model_fingerprint,
                                      plan_sha256=plan_digest, page_no=page_no,
                                      prompt=prompt, max_new_tokens=max_new_tokens):
                        expected_ids = {r["region_id"] for r in grouped[page_no]}
                        existing_regions = {r["region_id"]: r for r in saved["regions"]
                                            if r.get("region_id") in expected_ids}
                    if len(existing_regions) == len(grouped[page_no]):
                        print(f"CELL {page_no} {len(existing_regions)} {len(grouped[page_no])}", flush=True)
                        output_paths.append(path)
                        continue
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
            geometries = [r for r in grouped[page_no] if r["region_id"] not in existing_regions]
            reusable = _reusable_page_observations(cache_root, pdf_sha256=digest,
                model_fingerprint=model_fingerprint, page_no=page_no,
                prompt=prompt, max_new_tokens=max_new_tokens)
            for geometry in list(geometries):
                observed = reusable.get(_observation_geometry_key(geometry))
                if observed is None:
                    continue
                existing_regions[geometry["region_id"]] = dict(geometry,
                    ocr_blocks=observed.get("ocr_blocks", []), raw_text=observed["raw_text"], truncated=bool(observed.get("truncated", False)),
                    inference_model=ocr_model, task_prompt=prompt,
                    source_geometry_id=geometry["region_id"],
                    reused_glm_geometry_observation=True,
                    coordinate_note="GLM text reused from the identical measured crop and inference settings")
            geometries = [r for r in geometries if r["region_id"] not in existing_regions]
            if existing_regions:
                _write_sidecar(path, pdf_sha256=digest, model_fingerprint=model_fingerprint,
                    plan_sha256=plan_digest, page_no=page_no,
                    dpi=int(grouped[page_no][0]["render_dpi"]), prompt=prompt,
                    max_new_tokens=max_new_tokens, regions=list(existing_regions.values()))
                print(f"CELL {page_no} {len(existing_regions)} {len(grouped[page_no])}", flush=True)
            if not geometries:
                output_paths.append(path)
                continue
            dpis = {int(item["render_dpi"]) for item in geometries}
            if len(dpis) != 1:
                raise ValueError(f"Page {page_no} has mixed legacy render DPIs")
            dpi = dpis.pop()
            page = document[page_no - 1]
            try:
                bitmap = page.render(scale=dpi / 72)
                try:
                    image = bitmap.to_pil().convert("RGB").copy()
                finally:
                    bitmap.close()
            finally:
                page.close()
            try:
                source_sizes = {tuple(item["source_pixel_size"]) for item in geometries}
                if len(source_sizes) != 1:
                    raise ValueError(f"Page {page_no} has inconsistent legacy coordinate sizes")
                source_width, source_height = next(iter(source_sizes))
                scale_x, scale_y = image.width / source_width, image.height / source_height
                results = list(existing_regions.values())
                for geometry in geometries:
                    scaled = [[x * scale_x, y * scale_y] for x, y in geometry["polygon_page"]]
                    bounds = _clip_box(scaled, image.width, image.height)
                    if bounds is None:
                        continue
                    crop = image.crop(tuple(bounds))
                    rotation = int(geometry.get('rotation_ccw', 0)) % 360
                    if rotation:
                        rotated = crop.rotate(rotation, expand=True)
                        crop.close()
                        crop = rotated
                    try:
                        if engine is None:
                            engine = GlmOcrEngine(model_dir, device=python_device,
                                max_new_tokens=max_new_tokens, prompt=prompt)
                        recognized = engine.recognize(crop)
                    finally:
                        crop.close()
                    raw_text = str(recognized.get("raw_text") or "")
                    results.append(dict(region_id=geometry["region_id"],
                        field_name=geometry["field_name"], raw_text=raw_text,
                        truncated=bool(recognized.get("truncated", False)),
                        polygon_page=geometry["polygon_page"],
                        source_pixel_size=geometry["source_pixel_size"], render_dpi=dpi,
                        coordinate_system=geometry["coordinate_system"],
                        geometry_sequence=geometry["geometry_sequence"],
                        geometry_sequence_count=geometry["geometry_sequence_count"],
                        inference_model=ocr_model, task_prompt=prompt,
                        crop_box_rendered_page=bounds,
                        source_geometry_id=geometry["region_id"],
                        row_group_id=geometry.get("row_group_id"),
                        coordinate_note="Polygon comes from legacy geometry; crop was rendered from the source PDF"))
                    if "ocr_blocks" in recognized:
                        results[-1]["ocr_blocks"] = recognized["ocr_blocks"]
                    for key in ("matcher_bbox", "matcher_coordinate_system", "rotation_ccw",
                                "kind", "boundary_method", "physical_card_boundary_verified"):
                        if key in geometry:
                            results[-1][key] = geometry[key]
                    _write_sidecar(path, pdf_sha256=digest, model_fingerprint=model_fingerprint,
                        plan_sha256=plan_digest, page_no=page_no, dpi=dpi,
                        prompt=prompt, max_new_tokens=max_new_tokens, regions=results)
                    print(f"CELL {page_no} {len(results)} {len(grouped[page_no])}", flush=True)
                output_paths.append(path)
            finally:
                image.close()
    finally:
        document.close()
        if engine is not None:
            lock_stream = getattr(engine, "_model_lock", None)
            if lock_stream is not None:
                try:
                    import fcntl
                    fcntl.flock(lock_stream, fcntl.LOCK_UN)
                    lock_stream.close()
                except OSError:
                    pass
    return output_paths


def attach_sidecars(checkpoints: list[dict], sidecar_paths: Iterable[Path], *,
                    expected_pdf_sha256: str, expected_model_fingerprint: str) -> list[dict]:
    """Append validated supplement regions in memory, preserving page OCR JSON."""
    from copy import deepcopy

    by_page = {int(item["page_no"]): deepcopy(item) for item in checkpoints}
    for path in sidecar_paths:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if value.get("schema_version") != SCHEMA:
            raise ValueError(f"Unsupported GLM supplement sidecar: {path}")
        if value.get("pdf_sha256") != expected_pdf_sha256:
            raise ValueError(f"Supplement sidecar PDF SHA256 mismatch: {path}")
        if value.get("model_fingerprint") != expected_model_fingerprint:
            raise ValueError(f"Supplement sidecar model fingerprint mismatch: {path}")
        page_no = int(value["page_no"])
        checkpoint = by_page.get(page_no)
        if checkpoint is None or checkpoint.get("pdf_sha256") != expected_pdf_sha256:
            raise ValueError(f"Supplement has no matching full-page checkpoint for page {page_no}")
        existing = {region.get("region_id") for region in checkpoint.get("regions", [])}
        for region in value.get("regions", []):
            if region.get("region_id") in existing:
                continue
            copied = deepcopy(region)
            copied["supplement_schema"] = SCHEMA
            copied["checkpoint_reference"] = str(Path(path).resolve())
            copied["checkpoint_sha256"] = sha256(path)
            checkpoint.setdefault("regions", []).append(copied)
            existing.add(copied["region_id"])
    return [by_page[key] for key in sorted(by_page)]


def extract_supplement_observations(sidecar_paths: Iterable[Path], *,
                                    expected_pdf_sha256: str,
                                    expected_model_fingerprint: str) -> list[dict]:
    """Return positioned raw GLM cell observations for a geometry join.

    The result intentionally remains an observation inventory; it does not use
    Paddle OCR values or guess which ledger row owns a cell. The consumer can
    join ``row_group_id`` and field polygons to its unchanged adaptive matcher.
    """
    observations = []
    for path in sidecar_paths:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if value.get("schema_version") != SCHEMA or value.get("pdf_sha256") != expected_pdf_sha256:
            raise ValueError(f"Invalid supplement identity: {path}")
        if value.get("model_fingerprint") != expected_model_fingerprint:
            raise ValueError(f"Invalid supplement model fingerprint: {path}")
        for region in value.get("regions", []):
            observations.append(dict(page_no=int(value["page_no"]),
                field_name=region.get("field_name"), raw_text=region.get("raw_text", ""),
                row_group_id=region.get("row_group_id"),
                polygon_page=region.get("polygon_page"),
                source_pixel_size=region.get("source_pixel_size"),
                render_dpi=region.get("render_dpi"),
                geometry_sequence=region.get("geometry_sequence"),
                source_region_id=region.get("region_id"),
                source_sidecar=str(Path(path).resolve()),
                status="glm_cell_observation_pending_coordinate_join"))
    return observations


def extract_supplement_identifiers(sidecar_paths: Iterable[Path], *,
                                   expected_pdf_sha256: str,
                                   expected_model_fingerprint: str) -> list[dict]:
    """Compatibility helper returning plausible IDs from certificate cells."""
    candidates = []
    for observation in extract_supplement_observations(sidecar_paths,
            expected_pdf_sha256=expected_pdf_sha256,
            expected_model_fingerprint=expected_model_fingerprint):
        if observation.get("field_name") != "certificate_no":
            continue
        text = re.sub(r"\s+", "", str(observation.get("raw_text") or "")).upper()
        identifiers = list(dict.fromkeys(re.findall(r"(?<![A-Z])[A-Z]{0,4}\d[A-Z0-9./_-]{2,}", text)))
        for identifier in identifiers:
            candidates.append({**observation, "value": identifier,
                               "status": "candidate_requires_row_association"})
    return candidates


def main(argv=None) -> int:
    """CLI entry point for the short-lived local GLM worker environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--ocr-model", choices=model_ids(), default=DEFAULT_OCR_MODEL)
    parser.add_argument("--pdf", type=Path, required=True)
    plan_group = parser.add_mutually_exclusive_group(required=True)
    plan_group.add_argument("--plan", type=Path)
    plan_group.add_argument("--coordinate-result", type=Path, nargs="+")
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    parser.add_argument("--render-dpi", type=int, default=300)
    args = parser.parse_args(argv)
    if not args.worker:
        parser.error("This module is intended for the isolated GLM worker; pass --worker")
    model = get_backend(args.ocr_model).fingerprint(args.model_dir)
    pdf_digest = sha256(args.pdf)
    if args.coordinate_result:
        plan = load_coordinate_geometry_plan(args.coordinate_result,
            expected_pdf_sha256=pdf_digest, render_dpi=args.render_dpi)
    else:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
    paths = recognize_geometry_plan(args.pdf, plan, cache_root=args.cache_root,
        model_dir=args.model_dir, model_fingerprint=model["fingerprint"],
        python_device=args.device, max_new_tokens=args.max_new_tokens, ocr_model=args.ocr_model)
    print(json.dumps({"sidecars": [str(path) for path in paths],
                      "new_image_files": 0, "pdf_sha256": plan["pdf_sha256"],
                      "plan_sha256": plan["plan_sha256"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
