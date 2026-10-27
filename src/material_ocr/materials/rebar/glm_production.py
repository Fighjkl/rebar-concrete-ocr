"""Production GLM-OCR runner for the rebar adapter.

The web process deliberately does not import torch or transformers.  This
module keeps orchestration and parsing in the normal environment and starts a
short-lived ``.venv-glm`` worker only when a matching JSON checkpoint is not
already available.  Source PDFs and model weights are referenced in place;
page images exist only in the worker's memory and are never written.

The public :func:`run_glm_pipeline` function returns a result whose
``automatic_ledger`` has the same 12-column ledger shape consumed by the web
normalizer.  Existing GLM logical parsing and matching are reused from
``material_ocr.materials.rebar.glm_experiment`` and
``tools.compare_ocr_ledgers.build_glm_ledger``.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from typing import Callable, Iterable

from material_ocr.ocr.glm import model_fingerprint, sha256
from material_ocr.ocr.backends import DEFAULT_OCR_MODEL, get_backend, model_ids
from material_ocr.workflow import write_json


Progress = Callable[[str, int, int, str], None]

DEFAULT_MODEL_DIR = Path("models/GLM-OCR")
DEFAULT_PROMPT = "Text Recognition:"
SCHEMA_VERSION = "glm-production-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[4]


@lru_cache(maxsize=4)
def _cached_model_fingerprint(directory: str, signature: tuple) -> dict:
    return model_fingerprint(directory)


def _model_identity(model_dir: Path) -> dict:
    names = ("config.json", "model.safetensors", "processor_config.json",
             "preprocessor_config.json", "tokenizer.json", "tokenizer_config.json",
             "chat_template.jinja", "generation_config.json")
    signature = tuple((name, (model_dir / name).stat().st_size,
                        (model_dir / name).stat().st_mtime_ns) for name in names)
    return _cached_model_fingerprint(str(model_dir.resolve()), signature)


def get_glm_cache_identity(model_dir: Path | None = None, *, ocr_model: str = DEFAULT_OCR_MODEL) -> dict:
    """Lightweight after the first call; invalidates when weights or rules change."""
    model = _ocr_identity(_effective_ocr_dir(model_dir, ocr_model), ocr_model)
    sources = [Path(__file__).resolve(),
               Path(__file__).with_name("glm_experiment.py"),
               Path(__file__).with_name("matching.py"),
               Path(__file__).with_name("heat_review.py"),
               Path(__file__).with_name("entry_duplicates.py"),
               Path(__file__).with_name("glm_web_contract.py"),
               Path(__file__).with_name("review_policy.py"),
               Path(__file__).with_name("glm_supplement.py"),
               Path(__file__).with_name("glm_entry_bridge.py"),
               Path(__file__).with_name("glm_attachment_recovery.py"),
               Path(__file__).with_name("coordinate_match.py"),
               PROJECT_ROOT / "src/material_ocr/ocr/glm_table_geometry.py",
               PROJECT_ROOT / "src/material_ocr/ocr/glm_tables.py",
               PROJECT_ROOT / "src/material_ocr/ocr/glm_geometry.py",
               PROJECT_ROOT / "src/material_ocr/ocr/paddle_native.py",
               PROJECT_ROOT / "src/material_ocr/ocr/backends.py",
               PROJECT_ROOT / "tools/compare_ocr_ledgers.py"]
    digest = hashlib.sha256()
    for source in sources:
        digest.update(source.name.encode())
        digest.update(source.read_bytes())
    identity = dict(model_id=ocr_model, model_fingerprint=model["fingerprint"],
                dpi=180, prompt=DEFAULT_PROMPT, max_new_tokens=4096,
                attn_implementation="sdpa", do_sample=False,
                rules_fingerprint=digest.hexdigest(), schema_version=SCHEMA_VERSION)
    if ocr_model == "local-paddle":
        for key in ("attn_implementation", "max_new_tokens", "do_sample"):
            identity.pop(key)
        identity.update(enable_mkldnn=False, use_doc_orientation_classify=False,
                        use_doc_unwarping=False, use_textline_orientation=False)
    return identity


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(value: str) -> str:
    # Keep the checkpoint folder readable, while the SHA suffix remains the
    # authoritative identity.  Never use user input as a path component.
    text = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    return text[:120] or "document"


def _checkpoint_dir(root: Path, pdf: Path, pdf_digest: str) -> Path:
    return root / "documents" / pdf_digest / "pages"


def _effective_model_dir(model_dir: Path | None) -> Path:
    return get_backend("local-glm").model_dir(model_dir)


def _effective_ocr_dir(model_dir, ocr_model):
    return get_backend(ocr_model).model_dir(model_dir)


def _ocr_identity(model_dir, ocr_model):
    backend = get_backend(ocr_model)
    model = _model_identity(model_dir) if backend.model_id == "local-glm" else backend.fingerprint(model_dir)
    return dict(model, model_id=backend.model_id)


def _unrotate_polygon(polygon, original_size, rotation):
    if not polygon:
        return None
    width, height = original_size
    output = []
    for x, y in polygon:
        if rotation == 90:
            x, y = width - 1 - y, x
        elif rotation == 180:
            x, y = width - 1 - x, height - 1 - y
        elif rotation == 270:
            x, y = y, height - 1 - x
        output.append([x, y])
    return output


def _upright_polygon(polygon, original_size, rotation):
    width, height = original_size
    result = []
    for x, y in polygon:
        if rotation == 90:
            x, y = y, width - 1 - x
        elif rotation == 180:
            x, y = width - 1 - x, height - 1 - y
        elif rotation == 270:
            x, y = height - 1 - y, x
        result.append([x, y])
    return result


def _find_geometry(pdf_digest: str, dpi: int, explicit: Path | None = None) -> dict | None:
    """Reuse only source rotations and measured input boxes, never OCR values."""
    manifests = ([Path(explicit)] if explicit else
                 sorted((PROJECT_ROOT / "experiments/analyses").glob("*geometry*.json")))
    for path in manifests:
        try:
            value = _read_checkpoint(path)
            documents = value.get("documents", [value])
            match = next((d for d in documents if d.get("pdf_sha256") == pdf_digest), None)
            if match and value.get("geometry_only"):
                pages = [dict(page_no=p["page_no"], dpi=p.get("dpi", dpi),
                              rotation_ccw=p.get("rotation_ccw", 0),
                              original_pixel_size=p.get("original_pixel_size"),
                              regions=[{k: r[k] for k in ("region_id", "kind", "bbox_upright", "polygon_page")
                                        if k in r} for r in p.get("regions", [])])
                         for p in match.get("pages", []) if p.get("dpi", dpi) == dpi]
                return dict(pdf_sha256=pdf_digest, geometry_only=True,
                            source_reference=str(path.resolve()), pages=pages)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    # Other uploaded test documents already have spatial OCR.  It is safe to
    # reuse their existing physical crop geometry and page rotation alone.
    candidates = sorted((PROJECT_ROOT / "runs/web/library").glob("*/run-*/work/attachments/attachment_index.json"))
    for path in candidates:
        try:
            value = _read_checkpoint(path)
            if value.get("pdf_sha256") != pdf_digest:
                continue
            pages = []
            for page in value.get("pages", []):
                if page.get("dpi", dpi) != dpi:
                    continue
                original = page.get("pixel_size")
                if not original:
                    continue
                rotation = int(page.get("rotation_ccw", 0) or 0) % 360
                regions = []
                for region in page.get("regions", []):
                    physical = (region.get("layout_role") == "physical_nameplate" or
                                region.get("physical_plate_id"))
                    delivery = region.get("document_type") == "delivery_note" and region.get("entity_key") != "full"
                    polygon = region.get("crop_polygon_page") or (region.get("polygon_page") if physical else None)
                    if not polygon or not (physical or delivery):
                        continue
                    upright = _upright_polygon(polygon, original, rotation)
                    xs, ys = zip(*upright)
                    regions.append(dict(region_id=region.get("region_id"),
                        kind="nameplate_candidate" if physical else "delivery_note_candidate",
                        polygon_page=polygon, bbox_upright=[min(xs), min(ys), max(xs), max(ys)]))
                pages.append(dict(page_no=page["page_no"], rotation_ccw=rotation,
                                  original_pixel_size=original, dpi=dpi, regions=regions))
            return dict(pdf_sha256=pdf_digest, geometry_only=True,
                        source_reference=str(path.resolve()), pages=pages)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return None


def _read_checkpoint(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _valid_checkpoint(value: dict, *, pdf_digest: str, model_digest: str,
                      prompt: str, dpi: int | None = None,
                      allow_partial: bool = False) -> bool:
    return (value.get("pdf_sha256") == pdf_digest
            and value.get("model_fingerprint") == model_digest
            and value.get("task_prompt", prompt) == prompt
            and (dpi is None or int(value.get("dpi", dpi)) == dpi)
            and (value.get("raw_text") is not None or value.get("regions"))
            and (allow_partial or value.get("ocr_stage", "completed") == "completed")
            and value.get("schema_version") in ("glm-native-page-v1", "glm-native-region-page-v1"))


def _discover_checkpoint_dirs(roots: Iterable[Path], pdf_digest: str,
                              model_digest: str, prompt: str, dpi: int,
                              page_count: int) -> dict[int, list[Path]]:
    """Find existing full-page JSON by source identity, without copying it."""
    found: dict[int, list[Path]] = {}
    seen = set()
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        # Accept either a GLM run root or its documents directory.  Recursive
        # glob is bounded to JSON pages and never reads source images.
        candidates = list(root.glob("documents/*/pages/page_*.json"))
        if root.name == "pages":
            candidates.extend(root.glob("page_*.json"))
        if (root / "pages").is_dir():
            candidates = list(candidates) + list(root.glob("pages/page_*.json"))
        for path in candidates:
            try:
                value = _read_checkpoint(path)
                page_no = int(value.get("page_no", 0))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not 1 <= page_no <= page_count:
                continue
            if _valid_checkpoint(value, pdf_digest=pdf_digest, model_digest=model_digest,
                                 prompt=prompt, dpi=None):
                # Prefer an existing checkpoint with ROI variants because the
                # logical parser can retain more nameplate evidence.  Ties are
                # deterministic and the source remains external by reference.
                resolved = path.resolve()
                if resolved not in seen:
                    found.setdefault(page_no, []).append(resolved)
                    seen.add(resolved)
    return found


def _render_page(pdf, number: int, dpi: int):
    """Render one page in the child process and release all native objects."""
    page = pdf[number - 1]
    try:
        bitmap = page.render(scale=dpi / 72)
        try:
            image = bitmap.to_pil().convert("RGB").copy()
        finally:
            bitmap.close()
    finally:
        page.close()
    return image


@contextmanager
def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("GLM-OCR checkpoint directory is already being processed") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _worker(args: argparse.Namespace) -> int:
    """Run full-page inference in the GLM-only environment.

    This function is intentionally reached only through ``--worker``.  The
    parent production process can therefore run without torch installed.
    """
    import pypdfium2

    source = Path(args.pdf).resolve()
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=True)
    digest = sha256(source)
    ocr_model = getattr(args, "ocr_model", "local-glm")
    GlmOcrEngine = get_backend(ocr_model).engine_class()
    if ocr_model == "local-glm":
        model = model_fingerprint(args.model_dir)
    else:
        model = _ocr_identity(args.model_dir, ocr_model)
    target = _checkpoint_dir(output, source, digest)
    target.mkdir(parents=True, exist_ok=True)
    document_for_count = pypdfium2.PdfDocument(source)
    try:
        pages = list(range(1, len(document_for_count) + 1))
    finally:
        document_for_count.close()
    if args.pages:
        wanted = [int(p) for p in args.pages]
        pages = [p for p in pages if p in wanted]
    with _lock(output / ".glm-production.lock"):
        pdf = pypdfium2.PdfDocument(source)
        geometry = None
        if args.geometry:
            geometry = json.loads(Path(args.geometry).read_text(encoding="utf-8"))
            if geometry.get("pdf_sha256") and geometry["pdf_sha256"] != digest:
                raise ValueError("GLM geometry manifest belongs to another PDF")
            geometry = {int(page["page_no"]): page for page in geometry.get("pages", [])}
        engine = None
        try:
            for page_no in pages:
                path = target / f"page_{page_no:04d}.json"
                partial = None
                if path.is_file():
                    try:
                        saved = _read_checkpoint(path)
                        if not args.force and _valid_checkpoint(saved, pdf_digest=digest,
                                             model_digest=model["fingerprint"],
                                             prompt=args.prompt, dpi=args.dpi, allow_partial=True):
                            if saved.get("ocr_stage", "completed") == "completed":
                                print(f"REUSED {source.name} page {page_no}", flush=True)
                                continue
                            partial = saved
                    except (OSError, ValueError, json.JSONDecodeError):
                        pass
                page_spec = (geometry or {}).get(page_no, {})
                rotation = int(page_spec.get("rotation_ccw", 0) or 0) % 360
                image = _render_page(pdf, page_no, args.dpi)
                original_size = list(image.size)
                if rotation:
                    rotated = image.rotate(rotation, expand=True)
                    image.close()
                    image = rotated
                try:
                    if partial is not None:
                        saved = partial
                    else:
                        if engine is None:
                            engine = GlmOcrEngine(args.model_dir, device=args.device,
                                                  max_new_tokens=args.max_new_tokens,
                                                  prompt=args.prompt)
                        result = engine.recognize(image)
                        saved = dict(schema_version="glm-native-page-v1", pdf_name=source.name,
                                     pdf_path=str(source), pdf_sha256=digest, page_no=page_no,
                                     original_pixel_size=original_size, pixel_size=list(image.size),
                                     rotation_ccw=rotation, dpi=args.dpi, ocr_stage="full_page",
                                     model_fingerprint=model["fingerprint"], source_region=None,
                                     regions=[], **result)
                        saved["inference_model"] = ocr_model
                        write_json(path, saved)
                    from material_ocr.ocr.glm_geometry import detect_form_regions, detect_panel_viewports
                    regions = page_spec.get("regions") or detect_form_regions(image, page_no=page_no)
                    # A mixed nameplate sheet may have one enclosing contour
                    # around touching cards.  Use measured repeated-panel
                    # viewports as a bounded OCR supplement when no physical
                    # form boxes survived; these are explicitly unverified
                    # viewports and never asserted as card boundaries.
                    if not page_spec.get("regions"):
                        panel_regions = detect_panel_viewports(image, page_no=page_no)
                        if len(panel_regions) >= 2 and len(panel_regions) > len(regions):
                            # Retain a delivery/table crop elsewhere on a
                            # mixed page when replacing merged card contours.
                            def overlaps(a, b):
                                return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])
                            regions = panel_regions + [r for r in regions if not any(
                                overlaps(r['bbox_upright'], p['bbox_upright']) for p in panel_regions)]
                    for region in regions:
                        if any(existing.get("region_id") == region.get("region_id")
                               and existing.get("bbox_upright") == region.get("bbox_upright")
                               for existing in saved.get("regions", [])):
                            continue
                        box = region.get("bbox_upright")
                        if not isinstance(box, list) or len(box) != 4:
                            continue
                        left, top, right, bottom = [int(round(x)) for x in box]
                        left, top = max(0, left), max(0, top)
                        right, bottom = min(image.width, right), min(image.height, bottom)
                        if right <= left or bottom <= top:
                            continue
                        crop = image.crop((left, top, right, bottom))
                        try:
                            if engine is None:
                                engine = GlmOcrEngine(args.model_dir, device=args.device,
                                    max_new_tokens=args.max_new_tokens, prompt=args.prompt)
                            region_result = dict(region)
                            region_result.update(engine.recognize(crop))
                            polygon = region.get("polygon_page")
                            if not page_spec.get("regions"):
                                polygon = _unrotate_polygon(polygon, original_size, rotation)
                            region_result.update(bbox_upright=[left, top, right, bottom],
                                polygon_page=polygon, pixel_size=list(crop.size),
                                coordinate_note="Measured input ROI only; no word-level boxes")
                            saved["regions"].append(region_result)
                            saved["ocr_stage"] = "regions"
                            write_json(path, saved)
                        finally:
                            crop.close()
                    saved["ocr_stage"] = "completed"
                    write_json(path, saved)
                    print(f"CHECKPOINT {path} {page_no}/{len(pages)}", flush=True)
                finally:
                    image.close()
                    if engine is not None and getattr(engine, "device", "").startswith("cuda"):
                        engine.torch.cuda.empty_cache()
        finally:
            pdf.close()
            if engine is not None:
                # The file lock is owned by the engine and is released on close;
                # deleting it would make concurrent jobs indistinguishable.
                lock_stream = getattr(engine, "_model_lock", None)
                if lock_stream is not None:
                    try:
                        fcntl.flock(lock_stream, fcntl.LOCK_UN)
                        lock_stream.close()
                    except OSError:
                        pass
    write_json(output / "parameters.json", dict(schema_version=SCHEMA_VERSION,
        model=model, pdf_name=source.name, pdf_sha256=digest, dpi=args.dpi,
        prompt=args.prompt, device=args.device, max_new_tokens=args.max_new_tokens,
        images_saved=0, storage_policy="source PDF/model by reference; page images in memory only"))
    return 0


def _run_worker(pdf: Path, out: Path, *, model_dir: Path, python_executable: Path,
                device: str, dpi: int, prompt: str, max_new_tokens: int,
                pages: list[int], progress: Progress | None, log_path: Path,
                geometry_manifest: Path | None = None, force: bool = False,
                ocr_model: str = DEFAULT_OCR_MODEL) -> list[str]:
    command = [str(python_executable), str(Path(__file__).resolve()), "--worker",
               "--pdf", str(pdf), "--out", str(out), "--model-dir", str(model_dir),
               "--device", device, "--dpi", str(dpi), "--prompt", prompt,
               "--max-new-tokens", str(max_new_tokens), "--pages", *map(str, pages)]
    command += ["--ocr-model", ocr_model]
    if force:
        command.append("--force")
    if geometry_manifest:
        command += ["--geometry", str(geometry_manifest)]
    write_json(log_path.with_suffix(".command.json"), dict(command=command, started_at_utc=_utc(),
        working_directory=str(Path.cwd()), config=dict(dpi=dpi, prompt=prompt, max_new_tokens=max_new_tokens, device=device)))
    env = dict(os.environ, PYTHONUNBUFFERED="1",
               PYTHONPATH=str(Path(__file__).resolve().parents[3]) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, env=env, start_new_session=os.name == "posix")
    completed = 0
    try:
        with log_path.open("w", encoding="utf-8") as log:
            for line in process.stdout or ():
                log.write(line)
                log.flush()
                line = line.rstrip()
                if line.startswith("CHECKPOINT ") or line.startswith("REUSED "):
                    completed += 1
                    if progress:
                        progress("attachments", completed, len(pages), f"{ocr_model} OCR {completed}/{len(pages)}")
        code = process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    if code:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"{ocr_model} OCR 子进程退出（{code}），日志：{log_path}；{tail}")
    return command


def _merge_page_checkpoints(checkpoints: list[Path]) -> list[dict]:
    from material_ocr.materials.rebar.glm_experiment import merge_checkpoint_variants
    grouped: dict[tuple[str, int], list[dict]] = {}
    for path in checkpoints:
        value = _read_checkpoint(path)
        key = (str(value.get("pdf_sha256", "")), int(value["page_no"]))
        value["checkpoint_reference"] = str(path.resolve())
        value["checkpoint_sha256"] = sha256(path)
        value["source_pixel_size"] = value.get("original_pixel_size") or value.get("pixel_size")
        value["render_dpi"] = value.get("dpi")
        for region in value.get("regions", []):
            region["checkpoint_reference"] = value["checkpoint_reference"]
            region["checkpoint_sha256"] = value["checkpoint_sha256"]
            region["source_pixel_size"] = value["source_pixel_size"]
            region["render_dpi"] = value["render_dpi"]
        grouped.setdefault(key, []).append(value)
    return [merge_checkpoint_variants(values) for _, values in sorted(grouped.items(), key=lambda item: item[0][1])]


def _load_resources(checkpoints: list[Path]) -> tuple[dict, list[dict]]:
    from material_ocr.materials.rebar.glm_experiment import extract_document
    merged = _merge_page_checkpoints(checkpoints)
    return extract_document(merged), merged


def _entry_pages(checkpoints: list[dict]) -> list[int]:
    """Select entry forms by their own title, not a certificate column label."""
    import re
    selected = []
    for page in checkpoints:
        texts = [page.get("raw_text", "")] + [r.get("raw_text", "") for r in page.get("regions", [])]
        if any(re.search(r"进场(?:数量清单|检验记录)", re.sub(r"\s+", "", text)) for text in texts):
            selected.append(int(page["page_no"]))
    return selected


def _find_entry_geometry(pdf_digest: str, pages: Iterable[int], *, library_root: Path | None = None) -> list[Path]:
    """Find the original adaptive detector's complete cells by PDF identity.

    Only cell geometry and calibration are consumed by the plan loader.  Old
    header/field OCR values never enter a GLM transcription.
    """
    wanted = set(map(int, pages))
    root = library_root or PROJECT_ROOT / "runs/web/library"
    found = {}
    for path in sorted(root.glob("*/run-*/work/inspection/pages/page_*/adaptive/coordinate_result.json"), reverse=True):
        try:
            value = _read_checkpoint(path)
            provenance = value.get("provenance", {})
            page_no = int(provenance.get("page_no", 0))
            if provenance.get("pdf_sha256") != pdf_digest or page_no not in wanted or page_no in found:
                continue
            if value.get("cells") and provenance.get("calibration", {}).get("input_to_original"):
                found[page_no] = path
        except (OSError, ValueError, TypeError):
            continue
    return [found[page] for page in sorted(found)]


def _run_cell_worker(pdf: Path, plan_path: Path, native_cache: Path, *, model_dir: Path,
                     python_executable: Path, device: str, log_path: Path,
                     progress: Progress | None = None, ocr_model: str = DEFAULT_OCR_MODEL) -> list[Path]:
    command = [str(python_executable), "-m", "material_ocr.materials.rebar.glm_supplement", "--worker",
               "--pdf", str(pdf), "--plan", str(plan_path), "--cache-root", str(native_cache),
               "--model-dir", str(model_dir), "--device", device, "--max-new-tokens", "1024"]
    command += ["--ocr-model", ocr_model]
    write_json(log_path.with_suffix(".command.json"), dict(command=command, started_at_utc=_utc()))
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONPATH=str(PROJECT_ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""))
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    sidecars = None
    counts = {}
    for region in _read_checkpoint(plan_path).get("regions", []):
        page = int(region["page_no"])
        counts[page] = counts.get(page, 0) + 1
    completed_by_page = {page: 0 for page in counts}
    try:
        with log_path.open("w", encoding="utf-8") as log:
            for line in process.stdout or ():
                log.write(line); log.flush()
                if line.startswith("CELL ") and progress:
                    parts = line.split()
                    if len(parts) >= 4:
                        page = int(parts[1])
                        completed_by_page[page] = max(completed_by_page.get(page, 0), int(parts[-2]))
                        progress("inspection", sum(completed_by_page.values()), sum(counts.values()),
                                 f"{ocr_model} 按原自适应表格逐格识别")
                if line.startswith("{"):
                    try:
                        message = json.loads(line)
                        if "sidecars" in message:
                            sidecars = [Path(p) for p in message["sidecars"]]
                    except ValueError:
                        pass
        code = process.wait()
    finally:
        if process.poll() is None:
            process.kill(); process.wait()
    if code or sidecars is None:
        raise RuntimeError(f"{ocr_model} 自适应表格单元格识别失败（{code}），日志：{log_path}")
    return sidecars


def _run_table_geometry_worker(pdf: Path, pages: Iterable[int], native_cache: Path,
                                output: Path, checkpoints: list[dict],
                                progress: Progress | None = None, *,
                                python_executable: Path | None = None,
                                device: str = "cpu", render_dpi: int = 300) -> list[Path]:
    """Measure table cells for an uploaded PDF when no historical geometry exists.

    The worker is the normal Paddle environment and loads only cached layout,
    table-kind and cell-detector models.  It writes compact geometry JSON into
    the source-identity GLM cache; no image/PDF/model is copied.  GLM text is
    still produced later by ``_run_cell_worker``.
    """
    pages = sorted({int(page) for page in pages})
    if not pages:
        return []
    python_executable = Path(python_executable or sys.executable)
    models_root = Path(os.environ.get(
        "MATERIAL_OCR_PADDLE_MODELS_ROOT",
        str(PROJECT_ROOT / "models/paddlex_cache/official_models"),
    ))
    rotations = {}
    by_page = {int(value.get("page_no")): value for value in checkpoints}
    for page in pages:
        rotation = int(by_page.get(page, {}).get("rotation_ccw", 0) or 0) % 360
        rotations[page] = rotation
    manifest_path = output / "glm_table_geometry_manifest.json"
    command = [str(python_executable), "-m", "material_ocr.ocr.glm_table_geometry",
               "--pdf", str(pdf), "--pages", ",".join(map(str, pages)),
               "--output-root", str(native_cache), "--models-root", str(models_root),
               "--device", str(device), "--cpu-threads",
               os.environ.get("MATERIAL_OCR_GEOMETRY_CPU_THREADS", "4"),
               "--render-dpi", str(render_dpi), "--manifest-output", str(manifest_path)]
    for page in pages:
        command += ["--rotation", f"{page}:{rotations[page]}"]
    log_path = output / "glm_table_geometry_worker.log"
    write_json(log_path.with_suffix(".command.json"),
               dict(command=command, started_at_utc=_utc(), pages=pages,
                    models_root=str(models_root), image_persistence="none"))
    # The manifest path is stable within this per-run output directory. Remove
    # any previous result before starting the worker so an otherwise-successful
    # subprocess that failed to write --manifest-output cannot reuse stale data.
    try:
        manifest_path.unlink()
    except FileNotFoundError:
        pass
    env = dict(os.environ, PYTHONUNBUFFERED="1",
               PYTHONPATH=str(PROJECT_ROOT / "src") + os.pathsep +
               os.environ.get("PYTHONPATH", ""))
    process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, env=env)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(process.stdout or "", encoding="utf-8")
    if process.returncode:
        raise RuntimeError(f"表格几何检测失败（{process.returncode}），日志：{log_path}")
    try:
        manifest = _read_checkpoint(manifest_path)
        if not isinstance(manifest, dict):
            raise ValueError("geometry manifest must be a JSON object")
        if manifest.get("schema") != "glm-table-geometry-v1":
            raise ValueError("unexpected geometry manifest schema")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError(f"表格几何检测未返回清单，日志：{log_path}") from exc
    try:
        raw_paths = manifest.get("coordinate_result_paths")
        if not isinstance(raw_paths, list):
            raise ValueError("coordinate_result_paths must be a list")
        paths = [Path(path).resolve() for path in raw_paths]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"表格几何检测未生成有效单元格结果，日志：{log_path}") from exc
    if not paths or any(not path.is_file() for path in paths):
        raise RuntimeError(f"表格几何检测未生成有效单元格结果，日志：{log_path}")
    if progress:
        progress("inspection", 0, 1, f"已测量 {len(paths)} 个自适应表格几何文件")
    return paths


def _restore_adaptive_entries(pdf: Path, checkpoints: list[dict], resources: dict, *, output: Path,
                              native_cache: Path, model: dict, python_executable: Path,
                              model_dir: Path, device: str, progress: Progress | None,
                              config: dict) -> tuple[dict, list[dict]]:
    """Feed GLM cell text into the unchanged adaptive keyword/row matcher."""
    from .glm_supplement import load_coordinate_geometry_plan, attach_sidecars, _sidecar_path, _sidecar_valid
    from .glm_entry_bridge import analyze_cells, flatten_coordinate_records
    pages = _entry_pages(checkpoints)
    if not pages:
        return resources, checkpoints
    digest = sha256(pdf)
    geometry_paths = _find_entry_geometry(digest, pages)
    geometry_pages = {int(_read_checkpoint(path)["provenance"]["page_no"]) for path in geometry_paths}
    missing_geometry = sorted(set(pages) - geometry_pages)
    geometry_error = None
    if missing_geometry:
        try:
            generated = _run_table_geometry_worker(pdf, missing_geometry, native_cache, output,
                                                    checkpoints, progress=progress)
            geometry_paths.extend(generated)
            geometry_paths = sorted({Path(path).resolve() for path in geometry_paths},
                                    key=lambda path: (int((_read_checkpoint(path).get("provenance") or {}).get("page_no", 0)), str(path)))
            geometry_pages = {int(_read_checkpoint(path)["provenance"]["page_no"]) for path in geometry_paths
                              if (_read_checkpoint(path).get("provenance") or {}).get("page_no") is not None}
            missing_geometry = sorted(set(pages) - geometry_pages)
            config["geometry_worker_executed"] = True
        except Exception as exc:
            geometry_error = str(exc)
    if missing_geometry:
        reason = ("进场表物理单元格边界检测未完成，保留整页识别候选与原页定位：" +
                  (geometry_error or "几何检测未返回这些页面的单元格"))
        resources.setdefault("issues", []).append(dict(code="adaptive_geometry_unavailable",
            pages=missing_geometry, reason=reason, error=geometry_error))
    if not geometry_paths:
        return resources, checkpoints
    plan = load_coordinate_geometry_plan(geometry_paths, expected_pdf_sha256=digest)
    if not plan.get("regions"):
        return resources, checkpoints
    plan_path = output / "glm_entry_geometry.json"
    write_json(plan_path, plan)
    expected_pages = sorted({int(r["page_no"]) for r in plan["regions"]})
    paths = [_sidecar_path(native_cache, digest, plan["plan_sha256"], page) for page in expected_pages]
    cached = True
    for page_no, path in zip(expected_pages, paths):
        try:
            value = _read_checkpoint(path)
            cached = cached and _sidecar_valid(value, pdf_sha256=digest, model_fingerprint=model["fingerprint"],
                plan_sha256=plan["plan_sha256"], page_no=page_no, prompt=DEFAULT_PROMPT, max_new_tokens=1024)
            cached = cached and {r["region_id"] for r in value["regions"]} == {
                r["region_id"] for r in plan["regions"] if int(r["page_no"]) == page_no}
        except (OSError, ValueError, KeyError):
            cached = False
    if not cached:
        if progress:
            progress("inspection", 0, len(plan["regions"]), "复用自适应表格边界，由 OCR 识别表头和单元格")
        paths = _run_cell_worker(pdf, plan_path, native_cache, model_dir=model_dir,
            python_executable=python_executable, device=device, log_path=output / "glm_entry_worker.log", progress=progress, ocr_model=model.get("model_id", "local-glm"))
        config["new_ocr_executed"] = True
    config["adaptive_entry"] = dict(geometry_sources=[str(p) for p in geometry_paths],
        sidecar_references=[str(p) for p in paths], reused=cached, images_saved=0)
    # Sidecars are used only for the table matcher, not reparsed as whole
    # nameplates, deliveries or certificates.
    checkpoint_by_page = {int(p["page_no"]): p for p in checkpoints}
    replacements = {}
    for path in paths:
        value = _read_checkpoint(path)
        page_no = int(value["page_no"])
        cells = [dict(r, cell_id=r["region_id"], checkpoint_reference=str(path.resolve()),
                      checkpoint_sha256=sha256(path)) for r in value["regions"]]
        result = analyze_cells(cells, page_no=page_no, pdf_sha256=digest)
        write_json(output / f"work/inspection/pages/page_{page_no:03d}/adaptive/coordinate_result.json", result)
        rows = flatten_coordinate_records(result, dict(checkpoint_by_page[page_no],
            checkpoint_reference=str(path.resolve()), checkpoint_sha256=sha256(path)))
        if rows:
            replacements[page_no] = rows
        else:
            resources.setdefault("issues", []).append(dict(code="glm_adaptive_table_unresolved", page_no=page_no,
                reason="OCR单元格文字未通过原自适应表头或行列校验，保留整页候选", table_issues=result.get("table_issues", [])))
    resources["records"] = sorted([r for r in resources.get("records", []) if int(r["page_no"]) not in replacements] +
        [r for page in sorted(replacements) for r in replacements[page]], key=lambda r: (r["page_no"], str(r.get("row_no", ""))))
    resources["adaptive_entry"] = dict(pages=sorted(replacements), record_count=sum(map(len, replacements.values())),
        note=f"{model.get('model_id', 'local-glm')} cell text; original adaptive coordinate_match headers, rows and keyword parsing")
    checkpoints = attach_sidecars(checkpoints, paths, expected_pdf_sha256=digest,
                                  expected_model_fingerprint=model["fingerprint"])
    return resources, checkpoints


def _build_ledger(resources: dict, checkpoints: list[dict]) -> dict:
    # Keep one implementation of the reviewed matching rules.  Importing the
    # existing comparison helper avoids a second, potentially divergent copy.
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from tools.compare_ocr_ledgers import build_glm_ledger
    return build_glm_ledger(resources, checkpoints)


def _certificate_retry_pages(resources):
    """Identify broken certificate links without requiring an existing match."""
    from .glm_experiment import token
    entry_numbers = {token(r.get('certificate_no', '')) for r in resources.get('records', [])} - {''}
    plate_heats = {h for p in resources.get('nameplates', []) for h in p.get('heat_lots', [])}
    return {c['page_no'] for c in resources.get('certificates', []) if
            not c.get('certificate_numbers') or not c.get('heat_lots') or
            (entry_numbers and not entry_numbers.intersection(map(token, c['certificate_numbers']))) or
            (plate_heats and not plate_heats.intersection(c.get('heat_lots', [])))}


def _restore_certificate_views(pdf, checkpoints, resources, *, output, native_cache,
                               model, python_executable, model_dir, device, progress, config):
    """Recover labelled certificate headers/lots even when no join exists yet."""
    from .glm_experiment import extract_document
    from .glm_supplement import attach_sidecars, _sidecar_path, _sidecar_valid
    pages = _certificate_retry_pages(resources)
    if not pages:
        return resources, checkpoints
    checkpoint_path=output/'certificate_retry_checkpoints.json'
    write_json(checkpoint_path,checkpoints)
    plan_path=output/'glm_certificate_geometry.json'
    command=[sys.executable,'-m','material_ocr.materials.rebar.glm_attachment_recovery',
        '--pdf',str(pdf),'--checkpoints',str(checkpoint_path),'--output',str(plan_path),
        '--certificates','--pages',*map(str,sorted(pages))]
    write_json(output/'glm_certificate_geometry.command.json',dict(command=command))
    with (output/'glm_certificate_geometry.log').open('w') as log:
        subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True)
    plan=_read_checkpoint(plan_path);digest=sha256(pdf)
    paths=[_sidecar_path(native_cache,digest,plan['plan_sha256'],n) for n in sorted(pages)]
    cached=True
    for n,path in zip(sorted(pages),paths):
        try:
            value=_read_checkpoint(path)
            cached &= _sidecar_valid(value,pdf_sha256=digest,model_fingerprint=model['fingerprint'],
                plan_sha256=plan['plan_sha256'],page_no=n,prompt=DEFAULT_PROMPT,max_new_tokens=1024)
            cached &= {r['region_id'] for r in value['regions']}=={
                r['region_id'] for r in plan['regions'] if r['page_no']==n}
        except (OSError,ValueError,KeyError):cached=False
    if paths and not cached:
        if progress:progress('attachments',0,len(plan['regions']),'定向补读证书编号及轧制批号区域')
        paths=_run_cell_worker(pdf,plan_path,native_cache,model_dir=model_dir,
            python_executable=python_executable,device=device,log_path=output/'glm_certificate_worker.log',
            progress=progress,ocr_model=model.get('model_id','local-glm'))
        config['new_ocr_executed']=True
    config['certificate_recovery']=dict(pages=sorted(pages),sidecar_references=[str(p) for p in paths],
        reused=cached,regions=len(plan['regions']),geometry_command=command)
    merged=attach_sidecars(checkpoints,paths,expected_pdf_sha256=digest,
        expected_model_fingerprint=model['fingerprint'])
    parsed=extract_document(merged);parsed['records']=resources['records']
    if 'adaptive_entry' in resources:parsed['adaptive_entry']=resources['adaptive_entry']
    parsed['issues']=list(resources.get('issues',[]))+parsed.get('issues',[])
    return parsed,merged


def _restore_attachment_views(pdf, checkpoints, resources, *, output, native_cache,
                              model, python_executable, model_dir, device, progress, config):
    """Retry broken attachment chains once; reuse identity-checked OCR views."""
    from .glm_experiment import extract_document
    from .glm_supplement import attach_sidecars, _sidecar_valid
    digest = sha256(pdf)
    cached = []
    covered = set()
    for path in sorted((native_cache / 'documents' / digest / 'supplements').glob('*/pages/page_*.json')):
        try:
            value = _read_checkpoint(path)
            page = int(value['page_no'])
            regions = value.get('regions', [])
            if (not _sidecar_valid(value, pdf_sha256=digest, model_fingerprint=model['fingerprint'],
                    plan_sha256=path.parents[1].name, page_no=page, prompt=DEFAULT_PROMPT, max_new_tokens=1024)
                    or len(regions) != 2 or
                    {r.get('region_id') for r in regions} != {f'p{page:03d}-attachment-view-0', f'p{page:03d}-attachment-view-1'}
                    or any(r.get('kind') != 'attachment_recovery_view' or
                           r.get('physical_card_boundary_verified') is not False for r in regions)):
                continue
            if page not in covered and page in {c['page_no'] for c in checkpoints}:
                cached.append(path)
                covered.add(page)
        except (OSError, ValueError, KeyError, TypeError):
            continue

    def reparse(paths):
        merged = attach_sidecars(checkpoints, paths, expected_pdf_sha256=digest,
                                expected_model_fingerprint=model['fingerprint'])
        parsed = extract_document(merged)
        # Attachment retries never replace the original adaptive entry rows.
        parsed['records'] = resources['records']
        if 'adaptive_entry' in resources:
            parsed['adaptive_entry'] = resources['adaptive_entry']
        parsed['issues'] = list(resources.get('issues', [])) + parsed.get('issues', [])
        return parsed, merged

    recovered, merged = reparse(cached) if cached else (resources, checkpoints)
    ledger = _build_ledger(recovered, merged)
    retry = _attachment_retry_pages(recovered, ledger) - covered
    available = {c['page_no'] for c in checkpoints}
    retry &= available
    paths = []
    if retry:
        # The small orientation model runs outside the web process. Only the
        # selected pages are rendered; its plan contains no OCR field values.
        checkpoint_path = output / 'attachment_retry_checkpoints.json'
        write_json(checkpoint_path, checkpoints)
        plan_path = output / 'glm_attachment_geometry.json'
        command = [sys.executable, '-m', 'material_ocr.materials.rebar.glm_attachment_recovery',
            '--pdf', str(pdf), '--checkpoints', str(checkpoint_path), '--output', str(plan_path),
            '--pages', *map(str, sorted(retry))]
        write_json(output / 'glm_attachment_geometry.command.json', dict(command=command))
        if progress:
            progress('attachments', 0, len(retry), '为未完成的证书附件链测量方向并定向补读')
        with (output / 'glm_attachment_geometry.log').open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        paths = _run_cell_worker(pdf, plan_path, native_cache, model_dir=model_dir,
            python_executable=python_executable, device=device,
            log_path=output / 'glm_attachment_worker.log', progress=progress, ocr_model=model.get('model_id', 'local-glm'))
        config['new_ocr_executed'] = True
        recovered, merged = reparse(cached + paths)
    config['attachment_recovery'] = dict(sidecar_references=[str(p) for p in cached + paths],
        cached_pages=sorted(covered), retry_pages=sorted(retry), reused=not bool(paths), images_saved=0)
    return recovered, merged


def _attachment_retry_pages(resources, ledger):
    """One bounded retry of failed chains, with no page-number assumptions."""
    pages = set()
    attachments = {x['page_no'] for key in ('nameplates', 'deliveries') for x in resources.get(key, [])}
    for certificate in resources.get('certificates', []):
        if not certificate.get('certificate_numbers') or not certificate.get('heat_lots'):
            pages.add(certificate['page_no'])
    for row in ledger.get('records', []):
        sources = row.get('sources', {})
        certificate = sources.get('certificate')
        plate = sources.get('nameplate')
        if certificate and (not plate or not sources.get('delivery')):
            page = certificate['page_no']
            pages.add(page)
            pages.update(n for n in (page-1, page+1) if n in attachments)
        if plate and (not sources.get('delivery') or
                not row['fields']['送货单编号'].get('value')):
            pages.add(plate['page_no'])
    return pages


def _panel_retry_pages(checkpoints, resources):
    """Include small sheets whose logical cards still share an input crop."""
    pages = set()
    for cp in checkpoints:
        local = [p for p in resources.get('nameplates', []) if p['page_no'] == cp['page_no']]
        independent = [r for r in cp.get('regions', []) if r.get('kind') in
                       ('physical_nameplate_candidate', 'independent_panel_view')]
        shared_multi_card = any(p.get('evidence',{}).get('logical_card_count',1)>1 for p in local)
        if (len(local) >= 2 and len(independent) < len(local) and
                (shared_multi_card or len(local) >= 4)):
            pages.add(cp['page_no'])
    return pages


def _restore_panel_views(pdf, checkpoints, resources, *, output, native_cache,
                         model, python_executable, model_dir, device, progress, config):
    """Supplement mixed-card pages lacking independent input regions."""
    from .glm_experiment import extract_document
    from .glm_supplement import attach_sidecars, _sidecar_path, _sidecar_valid
    pages = _panel_retry_pages(checkpoints, resources)
    if not pages:
        return resources, checkpoints
    checkpoint_path = output / 'panel_retry_checkpoints.json'
    write_json(checkpoint_path, checkpoints)
    plan_path = output / 'glm_panel_geometry.json'
    command = [sys.executable, '-m', 'material_ocr.materials.rebar.glm_attachment_recovery',
        '--pdf', str(pdf), '--checkpoints', str(checkpoint_path), '--output', str(plan_path),
        '--panels', '--pages', *map(str, sorted(pages))]
    write_json(output / 'glm_panel_geometry.command.json', dict(command=command))
    with (output / 'glm_panel_geometry.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    plan = _read_checkpoint(plan_path)
    digest = sha256(pdf)
    expected = sorted({r['page_no'] for r in plan['regions']})
    paths = [_sidecar_path(native_cache, digest, plan['plan_sha256'], n) for n in expected]
    cached = True
    for n, path in zip(expected, paths):
        try:
            value = _read_checkpoint(path)
            cached &= _sidecar_valid(value, pdf_sha256=digest, model_fingerprint=model['fingerprint'],
                plan_sha256=plan['plan_sha256'], page_no=n, prompt=DEFAULT_PROMPT, max_new_tokens=1024)
            cached &= {r['region_id'] for r in value['regions']} == {
                r['region_id'] for r in plan['regions'] if r['page_no'] == n}
        except (OSError, ValueError, KeyError):
            cached = False
    if paths and not cached:
        if progress:
            progress('attachments', 0, len(plan['regions']), 'OCR 按独立铭牌窗口补读混合页')
        paths = _run_cell_worker(pdf, plan_path, native_cache, model_dir=model_dir,
            python_executable=python_executable, device=device,
            log_path=output / 'glm_panel_worker.log', progress=progress, ocr_model=model.get('model_id', 'local-glm'))
        config['new_ocr_executed'] = True
    config['panel_recovery'] = dict(pages=expected, sidecar_references=[str(p) for p in paths],
        reused=cached, regions=len(plan['regions']), geometry_command=command)
    if not paths:
        return resources, checkpoints
    merged = attach_sidecars(checkpoints, paths, expected_pdf_sha256=digest,
                            expected_model_fingerprint=model['fingerprint'])
    parsed = extract_document(merged)
    parsed['records'] = resources['records']
    if 'adaptive_entry' in resources:
        parsed['adaptive_entry'] = resources['adaptive_entry']
    parsed['issues'] = list(resources.get('issues', [])) + parsed.get('issues', [])
    return parsed, merged


def _adapt_ledger(ledger: dict, *, pdf: Path, pdf_digest: str, ocr_model="local-glm") -> dict:
    from .glm_web_contract import adapt_glm_ledger
    return adapt_glm_ledger(ledger, pdf_name=pdf.name, pdf_digest=pdf_digest, ocr_model=ocr_model)


def _standard_files(output: Path, pdf: Path, digest: str, checkpoints: list[Path],
                    resources: dict, ledger: dict, *, run_id: str,
                    model: dict, config: dict, merged_checkpoints: list[dict]) -> dict:
    """Persist only compact JSON/text artifacts required by the web review."""
    ocr_model = config.get("ocr_model", "local-glm")
    model_label = get_backend(ocr_model).cache_namespace
    work = output / "work"
    inspection = work / "inspection"
    attachments = work / "attachments"
    inspection.mkdir(parents=True, exist_ok=True)
    attachments.mkdir(parents=True, exist_ok=True)
    index = dict(schema_version="glm-native-attachment-index-v1", pdf_name=pdf.name,
                 pdf_sha256=digest, pages=[], model=model_label, model_fingerprint=model["fingerprint"],
                 cache_references=[str(path) for path in checkpoints])
    for value in merged_checkpoints:
        full_region = dict(region_id=f'p{value["page_no"]:03d}_full',
            text=value.get("raw_text", ""), raw_text=value.get("raw_text", ""),
            blocks=[dict(text=value.get("raw_text", ""), score=None, polygon_page=None)])
        regions = [full_region]
        for raw_region in value.get("regions", []):
            region = dict(raw_region)
            region["blocks"] = [dict(text=region.get("raw_text", ""), score=None,
                                    polygon_page=region.get("polygon_page"))]
            regions.append(region)
        index["pages"].append(dict(page_no=value["page_no"], pdf_sha256=digest,
            document_type="unknown", rotation_ccw=value.get("rotation_ccw", 0),
            pixel_size=value.get("original_pixel_size") or value.get("pixel_size"), dpi=value.get("dpi"),
            ocr_status="completed", raw_text=value.get("raw_text", ""),
            regions=regions))
    index["pages"].sort(key=lambda item: item["page_no"])
    records = dict(schema_version="glm-native-records-v1", pdf_name=pdf.name,
                   pdf_sha256=digest, records=resources.get("records", []), extraction_issues=resources.get("issues", []),
                   model=model_label, model_fingerprint=model["fingerprint"])
    write_json(attachments / "attachment_index.json", index)
    write_json(inspection / "records_all_pages.json", records)
    write_json(output / "glm_resources.json", resources)
    ledger = _adapt_ledger(ledger, pdf=pdf, pdf_digest=digest, ocr_model=ocr_model)
    write_json(output / "automatic_ledger.json", ledger)
    write_json(output / "progress.json", dict(stage="completed", current=1, total=1,
                                                message=f"{ocr_model} OCR 与自动匹配完成", updated_at=_utc()))
    metadata = dict(schema_version=SCHEMA_VERSION, run_id=run_id, status="completed",
        start_time_utc=config["start_time_utc"], end_time_utc=_utc(), command=config["command"],
        working_directory=str(Path.cwd()), data_version={str(pdf): digest}, model=model,
        config_reference=config, new_ocr_executed=config["new_ocr_executed"], images_saved=0,
        checkpoint_references=[str(path) for path in checkpoints],
        exit_code=0, code_version=get_glm_cache_identity(Path(config["model_dir"]), ocr_model=ocr_model)["rules_fingerprint"],
        environment=dict(python=sys.version, platform=platform.platform()), seed=None,
        storage=dict(pdf_copied=False, model_copied=False, images_saved=0))
    write_json(output / "experiment/metadata.json", metadata)
    (output / "experiment").mkdir(parents=True, exist_ok=True)
    (output / "experiment/result.md").write_text(
        f"# OCR 生产任务 {run_id}\n- 状态：completed\n- 模型：{ocr_model}\n"
        f"- OCR是否新执行：{config['new_ocr_executed']}\n- 页面图片保存：0\n"
        "- 原PDF与模型均按引用使用，JSON checkpoint 可复用。\n", encoding="utf-8")
    metrics = dict(record_count=len(ledger.get("records", [])),
                   model=model_label, new_ocr_executed=config["new_ocr_executed"], images_saved=0,
                   complete_candidate_records=sum(r.get("match_status") == "matched_pending_final_review"
                                                  for r in ledger.get("records", [])))
    result = dict(automatic_ledger=ledger, metrics=metrics,
                statistics=dict(record_count=len(ledger.get("records", [])), model=model_label,
                                quantity_missing_records=sum(not r.get("fields", {}).get("验收数量", {}).get("value") for r in ledger.get("records", [])), matching=metrics),
                glm_resources=resources, cache_references=[str(path) for path in checkpoints],
                new_ocr_executed=config["new_ocr_executed"], model=model_label)
    review = output / "review"
    review.mkdir(parents=True, exist_ok=True)
    write_json(review / "automatic_ledger.json", ledger)
    write_json(review / "match_results.json", dict(result, pdf_name=pdf.name, pdf_sha256=digest))
    from html import escape
    rows = ledger.get("records", [])
    columns = list(rows[0].get("fields", {})) if rows else []
    content = "<table><thead><tr>" + "".join(f"<th>{escape(c)}</th>" for c in columns) + "</tr></thead><tbody>"
    for row in rows:
        content += "<tr>" + "".join("<td>" + escape(str(row["fields"][c].get("value", ""))) +
            "<small> p" + escape(str(row["fields"][c].get("page") or "?")) + "</small></td>" for c in columns) + "</tr>"
    content += "</tbody></table>"
    (review / "index.html").write_text("<!doctype html><meta charset='utf-8'><title>OCR台账</title>"
        "<style>body{font:14px sans-serif}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:6px}"
        "small{color:#777}</style><h1>OCR 台账</h1><p>请在当前 Vue 工作台核验关键字段后导出。</p>" +
        content, encoding="utf-8")
    write_json(output / "statistics.json", result["statistics"])
    write_json(output / "summary.json", dict(material_id="rebar", pdf_name=pdf.name,
        pdf_sha256=digest, metrics=metrics, statistics=result["statistics"], review="review/index.html",
        model=ocr_model, new_ocr_executed=config["new_ocr_executed"], write_allowed=False))
    return result


def run_glm_pipeline(pdf: Path, output: Path, *, cache_dir: Path,
                     model_dir: Path | None = None,
                     python_executable: Path | None = None,
                     device: str = "cuda:0", dpi: int = 180,
                     prompt: str = DEFAULT_PROMPT, max_new_tokens: int = 4096,
                     progress: Progress | None = None, run_id: str | None = None,
                     cache_sources: Iterable[Path] | None = None,
                     geometry_manifest: Path | None = None,
                     force_ocr_refresh: bool = False, ocr_model: str = DEFAULT_OCR_MODEL) -> dict:
    """Reuse native GLM JSON or recognize missing pages in an isolated worker.

    The cache namespace includes source SHA, model, prompt, DPI and token limit.
    Forced inference rewrites only this stable production checkpoint, never an
    external experimental source.  No PDF, model or page image is copied.
    """
    model_dir = _effective_ocr_dir(model_dir, ocr_model)
    if python_executable is None:
        python_executable = get_backend(ocr_model).python()
    pdf, output, cache_dir = (Path(p).resolve() for p in (pdf, output, cache_dir))
    if not pdf.is_file():
        raise FileNotFoundError(pdf)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"GLM output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    digest = sha256(pdf)
    model = _ocr_identity(model_dir, ocr_model)
    inference = dict(model_fingerprint=model["fingerprint"], dpi=dpi,
                     prompt=prompt, max_new_tokens=max_new_tokens, attn_implementation="sdpa")
    if ocr_model != "local-glm":
        inference["ocr_model"] = ocr_model
    inference_key = hashlib.sha256(json.dumps(inference, sort_keys=True).encode()).hexdigest()[:20]
    native_cache = cache_dir / inference_key
    local_target = _checkpoint_dir(native_cache, pdf, digest)
    historical = list(cache_sources) if cache_sources is not None else sorted(
        (PROJECT_ROOT / "experiments/runs").glob("*/glm"))
    roots = [native_cache, *map(Path, historical)]
    page_count = _pdf_page_count(pdf)
    expected = set(range(1, page_count + 1))
    source_pages = {} if force_ocr_refresh else _discover_checkpoint_dirs(
        roots, digest, model["fingerprint"], prompt, dpi, page_count=page_count)
    # A later forced production OCR is authoritative for its page.  Historical
    # experiment variants remain immutable and must not overwrite that refresh
    # merely because an old full-page generation happened to be longer.
    for page, paths in list(source_pages.items()):
        local = [path for path in paths if path.is_relative_to(native_cache)]
        if local and any(_read_checkpoint(path).get("raw_text") is not None for path in local):
            source_pages[page] = local
    # ROI-only checkpoints supplement full pages but cannot prove a page has
    # undergone full-page inference at the requested rendering resolution.
    present = {page for page, paths in source_pages.items() if any(
        _read_checkpoint(path).get("raw_text") is not None and
        int(_read_checkpoint(path).get("dpi", dpi)) == dpi for path in paths)}
    missing = sorted(expected - present)
    started = _utc()
    run_id = run_id or "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-glm"
    config = dict(start_time_utc=started, command=["run_glm_pipeline", str(pdf), str(output)],
        ocr_model=ocr_model, new_ocr_executed=bool(missing), cache_dir=str(native_cache), model_dir=str(model_dir),
        python_executable=str(python_executable or _default_glm_python()), device=device,
        dpi=dpi, prompt=prompt, max_new_tokens=max_new_tokens, force_ocr_refresh=force_ocr_refresh)
    metadata = dict(run_id=run_id, status="running", start_time_utc=started,
        command=config["command"], config_reference=config,
        data_version={str(pdf): digest}, model=model, images_saved=0,
        code_version=get_glm_cache_identity(model_dir, ocr_model=ocr_model)["rules_fingerprint"], seed=None,
        seed_reason="deterministic OCR; no random sampling configured")
    write_json(output / "experiment/metadata.json", metadata)
    try:
        if progress:
            progress("starting", 0, page_count, f"准备本地 {ocr_model} OCR 与已有 JSON 缓存")
        if missing:
            geometry = _find_geometry(digest, dpi, geometry_manifest)
            geometry_path = None
            if geometry:
                geometry_path = output / "glm_geometry.json"
                write_json(geometry_path, geometry)
                config["geometry_source"] = geometry.get("source_reference")
            if python_executable is None:
                python_executable = _default_glm_python()
            device = "cuda:" + device.split(":", 1)[1] if device.startswith("gpu:") else device
            if progress:
                progress("attachments", len(present), page_count,
                         f"{ocr_model} OCR 已有缓存 {len(present)}/{page_count} 页，识别剩余页面")
            def worker_progress(stage, current, total, message):
                if progress:
                    progress(stage, len(present) + current, page_count,
                             f"{ocr_model} OCR {len(present) + current}/{page_count} 页")
            command = _run_worker(pdf, native_cache, model_dir=model_dir,
                python_executable=Path(python_executable), device=device, dpi=dpi, prompt=prompt,
                max_new_tokens=max_new_tokens, pages=missing, progress=worker_progress,
                log_path=output / "glm_worker.log", geometry_manifest=geometry_path,
                force=force_ocr_refresh, ocr_model=ocr_model)
            config["worker_command"] = command
            for page_no in missing:
                path = local_target / f"page_{page_no:04d}.json"
                if not path.is_file():
                    raise RuntimeError(f"GLM worker did not create checkpoint for page {page_no}")
                source_pages.setdefault(page_no, []).append(path)
        elif progress:
            progress("attachments", page_count, page_count, f"已复用 {ocr_model} 全卷原始 JSON，不重复 OCR")
        ordered = list(dict.fromkeys(path for p in sorted(source_pages) for path in source_pages[p]))
        write_json(output / "checkpoint_sources.json", dict(pdf_sha256=digest,
            inference=inference, references=[dict(path=str(path.resolve()), sha256=sha256(path)) for path in ordered]))
        if progress:
            progress("matching", 0, 1, "按当前双路线进行四表匹配")
        resources, merged_checkpoints = _load_resources(ordered)
        # Native GLM page transcription can omit dense table cells.  When a
        # prior adaptive run exposes complete measured cells for an entry
        # form, retry those cells with GLM and route the resulting text back
        # through the unchanged coordinate matcher.  This does not copy the
        # old Paddle text or rerun full-page OCR.
        resources, merged_checkpoints = _restore_adaptive_entries(
            pdf, merged_checkpoints, resources, output=output, native_cache=native_cache,
            model=model, python_executable=Path(python_executable or _default_glm_python()),
            model_dir=model_dir, device=device, progress=progress, config=config)
        resources, merged_checkpoints = _restore_certificate_views(
            pdf, merged_checkpoints, resources, output=output, native_cache=native_cache,
            model=model, python_executable=Path(python_executable or _default_glm_python()),
            model_dir=model_dir, device=device, progress=progress, config=config)
        resources, merged_checkpoints = _restore_attachment_views(
            pdf, merged_checkpoints, resources, output=output, native_cache=native_cache,
            model=model, python_executable=Path(python_executable or _default_glm_python()),
            model_dir=model_dir, device=device, progress=progress, config=config)
        resources, merged_checkpoints = _restore_panel_views(
            pdf, merged_checkpoints, resources, output=output, native_cache=native_cache,
            model=model, python_executable=Path(python_executable or _default_glm_python()),
            model_dir=model_dir, device=device, progress=progress, config=config)
        ledger = _build_ledger(resources, merged_checkpoints)
        result = _standard_files(output, pdf, digest, ordered, resources, ledger,
            run_id=run_id, model=model, config=config, merged_checkpoints=merged_checkpoints)
        if progress:
            progress("completed", 1, 1, f"{ocr_model} OCR 与自动匹配完成，可集中核验关键字段")
        return result
    except BaseException as exc:
        metadata.update(status="interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed",
                        end_time_utc=_utc(), exit_code=1, error=str(exc), config_reference=config)
        write_json(output / "experiment/metadata.json", metadata)
        (output / "experiment/result.md").write_text(
            f"# GLM 生产任务 {run_id}\n- 状态：{metadata['status']}\n- 错误：{exc}\n"
            "- 保留已完成 JSON checkpoint，未保存页面图片、PDF 或模型副本。\n", encoding="utf-8")
        raise


def _pdf_page_count(pdf: Path) -> int:
    try:
        import pypdfium2
    except ImportError:
        result = subprocess.run([str(_default_glm_python()), "-c",
            "import pypdfium2,sys; p=pypdfium2.PdfDocument(sys.argv[1]); "
            "print(len(p)); p.close()", str(pdf)], capture_output=True, text=True, check=True)
        return int(result.stdout.strip())
    from material_ocr.pdf_runtime import PDF_RENDER_LOCK
    with PDF_RENDER_LOCK:
        document = pypdfium2.PdfDocument(pdf)
        try:
            return len(document)
        finally:
            document.close()


def _default_glm_python() -> Path:
    return get_backend("local-glm").python()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--ocr-model", choices=model_ids(), default=DEFAULT_OCR_MODEL)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--dpi", type=int, default=180)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--max-new-tokens", type=int, default=4096)
    parser.add_argument("--pages", type=int, nargs="*")
    parser.add_argument("--geometry", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    args.model_dir = get_backend(args.ocr_model).model_dir(args.model_dir)
    if not args.worker:
        parser.error("This module is invoked by run_glm_pipeline; pass --worker")
    return _worker(args)


if __name__ == "__main__":
    raise SystemExit(main())
