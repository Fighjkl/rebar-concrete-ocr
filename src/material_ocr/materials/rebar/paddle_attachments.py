"""Whole-page Paddle attachment worker. No GLM, ROI detection or crop calls."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .upstream_adapter import file_hash, read_json
from .paddle_attachment_resources import assemble_attachments
from . import paddle_attachment_orientation as orientation
from . import paddle_attachment_detail as detail
from . import paddle_delivery_number as number_detail

ROOT = Path(__file__).resolve().parents[4]
SETTINGS = dict(dpi=300, det_limit=2560, rec_score_thresh=0.0,
                use_doc_orientation_classify=False, use_doc_unwarping=False,
                use_textline_orientation=False)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def blocks_from_result(data, prefix):
    if callable(data):
        data = data()
    if isinstance(data, str):
        data = json.loads(data)
    data = data.get("res", data)
    texts, scores, polygons = (data.get(k) for k in ("rec_texts", "rec_scores", "rec_polys"))
    if any(v is None for v in (texts, scores, polygons)) or not len(texts) == len(scores) == len(polygons):
        raise ValueError("Paddle rec_texts/rec_scores/rec_polys 不对应，拒绝错位坐标")
    return [dict(id=f"{prefix}_{i:04d}", text=str(text), score=float(score),
                 polygon=[[float(x), float(y)] for x, y in poly])
            for i, (text, score, poly) in enumerate(zip(texts, scores, polygons))]


def page_text(blocks):
    from material_ocr.ocr.paddle_native import reading_text
    return reading_text([dict(b, polygon_ocr=b["polygon"]) for b in blocks])


def create_engine(model_dir, device):
    from paddleocr import PaddleOCR
    return PaddleOCR(device=device.replace("cuda:", "gpu:"),
        text_detection_model_name="PP-OCRv5_server_det",
        text_recognition_model_name="PP-OCRv5_server_rec",
        text_detection_model_dir=str(Path(model_dir) / "PP-OCRv5_server_det"),
        text_recognition_model_dir=str(Path(model_dir) / "PP-OCRv5_server_rec"),
        use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False,
        text_rec_score_thresh=SETTINGS["rec_score_thresh"],
        text_det_limit_side_len=SETTINGS["det_limit"], text_det_limit_type="max")


def render(document, number, dpi):
    page = document[number - 1]
    try:
        bitmap = page.render(scale=dpi / 72)
        try:
            original = bitmap.to_pil()
            try:
                return original.convert("RGB").copy()
            finally:
                original.close()
        finally:
            bitmap.close()
    finally:
        page.close()


def worker(job, engine_factory=None, document_factory=None, classifier_factory=None):
    """Injectable inference in tests; production only receives full-page images."""
    import pypdfium2
    engine = (engine_factory or create_engine)(job["model_dir"], job["device"])
    document = (document_factory or pypdfium2.PdfDocument)(job["pdf"])
    classifier = None
    try:
        for page_no in job["pages"]:
            folder = Path(job["target"]) / f"page_{page_no:04d}"
            folder.mkdir(parents=True, exist_ok=True)
            image = render(document, page_no, SETTINGS["dpi"])
            size = list(image.size)
            try:
                image.save(folder / "page.png")
                passes = []
                fast = job.get("orientation_policy") == orientation.POLICY
                decision = dict(source="explicit_angles",rotation_ccw=job["angles"][str(page_no)][0])
                if fast:
                    decision = orientation.trusted_hint(job.get("page_hints",{}).get(str(page_no),{}))
                    if decision is None:
                        try:
                            if classifier is None:
                                classifier = (classifier_factory or orientation.create_classifier)(job["model_dir"],job["device"])
                            decision = orientation.classify(classifier,image,folder)
                        except Exception as exc:
                            # Honest, bounded fallback: the normal case never
                            # uses OCR to choose among all four directions.
                            decision = dict(source="classifier_failed",score=0,
                                rotation_ccw=job["angles"][str(page_no)][0],error=str(exc))
                    offsets=job.get("rotation_offsets",[0])
                    planned=list(dict.fromkeys((decision["rotation_ccw"]+n)%360 for n in offsets))
                else:
                    planned=list(job["angles"][str(page_no)])
                retry_reasons=[]
                for angle in planned:
                    started = time.monotonic()
                    upright = image.rotate(angle, expand=True)
                    try:
                        input_path = folder / f"input_r{angle}.png"
                        upright.save(input_path)
                        results = list(engine.predict(input=str(input_path)))
                        if len(results) != 1:
                            raise ValueError(f"附件第 {page_no} 页应得到一个整页 OCR 结果")
                        raw_path = folder / f"ocr_r{angle}.json"
                        results[0].save_to_json(str(raw_path))
                        blocks = blocks_from_result(read_json(raw_path), f"r{angle}")
                        passes.append(dict(rotation_ccw=angle, blocks=blocks,
                            input_size=list(upright.size), raw_json=str(raw_path),
                            elapsed_seconds=time.monotonic() - started,
                            quality=orientation.quality(blocks)))
                    finally:
                        upright.close()
                    # In fast/default mode only visibly weak OCR triggers
                    # another whole-page direction. Stop as soon as usable.
                    if fast and len(job.get("rotation_offsets",[0]))==1:
                        reason=orientation.retry_reason(passes,size)
                        if not reason:
                            break
                        retry_reasons.append(reason)
                        candidates=[(decision["rotation_ccw"]+n)%360 for n in (180,90,270)]
                        remaining=[a for a in candidates if a not in planned]
                        if remaining and len(planned)<=orientation.POLICY["max_extra_ocr_passes"]:
                            planned.append(remaining[0])
                best=max(passes,key=lambda v:v["quality"]["rank"]) if fast else passes[0]
                kind = detail.retry_kind(best, size)
                if kind:
                    upright = image.rotate(best["rotation_ccw"], expand=True)
                    prepared = detail.prepare_image(upright, kind)
                    try:
                        detail_path = folder / "input_detail.png"
                        prepared.save(detail_path)
                        results = list(engine.predict(input=str(detail_path),
                            text_det_thresh=.3 if kind=='certificate_text' else detail.POLICY["text_det_thresh"],
                            text_det_box_thresh=.6 if kind=='certificate_text' else detail.POLICY["text_det_box_thresh"],
                            **({"text_det_limit_side_len":3508} if kind=='certificate_text' else {})))
                        if len(results) != 1:
                            raise ValueError("附件补读必须返回一个整页结果")
                        raw_path = folder / "ocr_detail.json"
                        results[0].save_to_json(str(raw_path))
                        blocks = blocks_from_result(read_json(raw_path), "detail")
                        passes.append(dict(rotation_ccw=best["rotation_ccw"], blocks=blocks,
                            detail_retry=kind, input_size=list(prepared.size), raw_json=str(raw_path),
                            quality=orientation.quality(blocks)))
                    finally:
                        prepared.close()
                        upright.close()
                if number_detail.needs_literal_check(best):
                    upright=image.rotate(best['rotation_ccw'],expand=True).convert('L')
                    try:
                        path=folder/'input_literal.png';upright.save(path)
                        results=list(engine.predict(input=str(path),text_det_limit_side_len=3508))
                        if len(results)!=1:raise ValueError('编号原图补读必须返回一个整页结果')
                        raw_path=folder/'ocr_literal.json';results[0].save_to_json(str(raw_path))
                        blocks=blocks_from_result(read_json(raw_path),'literal')
                        passes.append(dict(rotation_ccw=best['rotation_ccw'],blocks=blocks,
                            detail_retry='delivery_literal',input_size=list(upright.size),raw_json=str(raw_path),
                            quality=orientation.quality(blocks)))
                    finally:upright.close()
                if number_detail.needs_retry(best):
                    upright=image.rotate(best["rotation_ccw"],expand=True)
                    prepared=number_detail.prepare_image(upright)
                    try:
                        path=folder/"input_number.png"
                        prepared.save(path)
                        results=list(engine.predict(input=str(path),
                            text_det_thresh=number_detail.POLICY["text_det_thresh"],
                            text_det_box_thresh=number_detail.POLICY["text_det_box_thresh"]))
                        if len(results)!=1:
                            raise ValueError("编号补读必须返回一个整页结果")
                        raw_path=folder/"ocr_number.json"
                        results[0].save_to_json(str(raw_path))
                        blocks=blocks_from_result(read_json(raw_path),"number")
                        passes.append(dict(rotation_ccw=best["rotation_ccw"],blocks=blocks,
                            detail_retry="delivery_number",input_size=list(prepared.size),raw_json=str(raw_path),
                            quality=orientation.quality(blocks)))
                    finally:
                        prepared.close();upright.close()
                decision.update(selected_rotation_ccw=best["rotation_ccw"],ocr_passes=len(passes),
                    review_required=bool(orientation.retry_reason(passes,size)) if fast else best["quality"]["weak"],
                    retry_reasons=list(dict.fromkeys(retry_reasons)))
                cp = dict(schema_version="paddle-full-page-anchors-v1", status="completed",
                    inference_model="local-paddle", pdf_sha256=job["pdf_sha256"],
                    page_no=page_no, cache_key=job["cache_key"], dpi=SETTINGS["dpi"],
                    original_pixel_size=size, pixel_size=best["input_size"],
                    rotation_ccw=best["rotation_ccw"], raw_text=page_text(best["blocks"]),
                    orientation=decision,
                    ocr_passes=passes, regions=[], truncated=False, source_image=str(folder / "page.png"),
                    page_image_sha256=file_hash(folder / "page.png"),
                    note="Every OCR input is a whole page; no GLM or attachment crops.")
                save(folder / "checkpoint.json", cp)
                print("PADDLE_ATTACHMENT_PAGE " + str(page_no), flush=True)
            finally:
                image.close()
    finally:
        document.close()


def launch(job, output, progress):
    jobfile = output / "paddle_attachments.job.json"
    log_path = output / "paddle_attachments.log"
    save(jobfile, job)
    command = [sys.executable, "-u", "-m", __name__, "--worker", str(jobfile)]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
        PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]))
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, text=True, encoding="utf-8", errors="replace", start_new_session=os.name == "posix")
        try:
            count = 0
            for line in process.stdout:
                log.write(line)
                log.flush()
                if line.startswith("PADDLE_ATTACHMENT_PAGE "):
                    count += 1
                    if progress:
                        progress("attachments", count, len(job["pages"]), "Paddle 整页附件 OCR")
            code = process.wait()
        finally:
            if process.poll() is None:
                from material_ocr.processes import stop_process
                stop_process(process, process_group=os.name == "posix")
    if code:
        raise RuntimeError(f"Paddle 附件 OCR 退出 {code}，查看 {log_path}")


def valid_checkpoint(path, key, digest, page_no):
    try:
        cp = read_json(path)
        if (cp["status"] != "completed" or cp["cache_key"] != key or cp["pdf_sha256"] != digest
                or cp["page_no"] != page_no or cp.get("inference_model") != "local-paddle"
                or cp.get("regions") or not cp.get("ocr_passes")):
            return None
        if file_hash(cp["source_image"]) != cp["page_image_sha256"]:
            return None
        cp["checkpoint_reference"] = str(path.resolve())
        cp["checkpoint_sha256"] = file_hash(path)
        return cp
    except (OSError, ValueError, KeyError, TypeError):
        return None


def run_attachments(pdf, output, pages, manifest, *, cache_dir, model_dir, device,
                    force=False, rotations=(0,), progress=None):
    from material_ocr.ocr.paddle_native import model_fingerprint
    from material_ocr.anchor_v3.report import review
    from .paddle_attachment_resources import anchor_page
    digest = file_hash(pdf)
    model = model_fingerprint(model_dir)
    base_angles = {p["page_no"]: int(p.get("rotation_ccw", 0)) for p in manifest["pages"]}
    angles = {str(n): list(dict.fromkeys((base_angles.get(n, 0) + offset) % 360 for offset in rotations))
              for n in pages}
    identity = dict(model_fingerprint=model["fingerprint"], settings=SETTINGS, angles=angles,
        orientation_policy=orientation.POLICY,
        orientation_model={p.name:file_hash(p) for p in sorted((Path(model_dir)/orientation.MODEL_NAME).glob("inference.*")) if p.is_file()},
        orientation_worker_sha256=file_hash(Path(orientation.__file__)),
        detail_policy=detail.POLICY, detail_worker_sha256=file_hash(Path(detail.__file__)),
        number_policy=number_detail.POLICY, number_worker_sha256=file_hash(Path(number_detail.__file__)),
        page_hints={str(p["page_no"]):orientation.trusted_hint(p) for p in manifest["pages"] if p["page_no"] in pages},
        worker_sha256=file_hash(Path(__file__)),
        note="Inference cache excludes field parsers: saved boxes can be reparsed without GPU.")
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
    target = Path(cache_dir).resolve() / key / digest
    paths = {n: target / f"page_{n:04d}/checkpoint.json" for n in pages}
    checkpoints = {n: valid_checkpoint(path, key, digest, n) for n, path in paths.items()}
    missing = sorted(n for n, cp in checkpoints.items() if cp is None or force)
    if missing:
        if progress:
            progress("attachments", len(pages)-len(missing), len(pages),
                     "附件 Paddle 整页 OCR；复用可靠方向或轻量分类，仅识别异常页重试，不裁剪")
        launch(dict(pdf=str(pdf), pdf_sha256=digest, pages=missing, cache_key=key,
            angles=angles, target=str(target), model_dir=str(model_dir), device=device,
            orientation_policy=orientation.POLICY,rotation_offsets=list(rotations),
            page_hints={str(p["page_no"]):p for p in manifest["pages"] if p["page_no"] in missing}), output, progress)
        for n in missing:
            checkpoints[n] = valid_checkpoint(paths[n], key, digest, n)
            if checkpoints[n] is None:
                raise RuntimeError(f"附件第 {n} 页 checkpoint 未完成")
    ordered = [checkpoints[n] for n in sorted(pages)]
    resources, reports = assemble_attachments(ordered)
    for cp in ordered:
        folder = output / "attachment_pages" / f"page_{cp['page_no']:04d}"
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cp["source_image"], folder / "page.png")
        save(folder / "checkpoint.json", cp)
        (folder / "raw.txt").write_text(cp["raw_text"], encoding="utf-8")
        page=next(p for p in resources["pages"] if p["page_no"]==cp["page_no"])
        save(folder / "spatial_fields.json",page)
        if cp["page_no"] in reports:
            report = reports[cp["page_no"]]
            _, blocks = anchor_page(cp)
            save(folder / "anchors.json", report)
            review(folder / "review.html", folder / "page.png", *cp["original_pixel_size"], report, blocks)
    info = dict(backend="local-paddle", model=model, settings=SETTINGS, cache_key=key,
        pages=sorted(pages), newly_recognized_pages=missing, full_page_only=True,
        attachment_crops=False, glm_attachment_calls=0, recovery_disabled=True,
        anchor_version="0.3.0", rotations_relative_to_upstream=list(rotations),
        orientation_policy=orientation.POLICY,
        orientation_pages=[dict(page_no=cp["page_no"],**cp.get("orientation",{})) for cp in ordered],
        field_parser_version="paddle-multiform-v1",
        resource_counts={k:len(resources[k]) for k in ("certificates","deliveries","nameplates")},
        certificate_fields=[dict(page_no=c["page_no"],certificate_numbers=c["certificate_numbers"],
            heat_lots=c["heat_lots"],issues=c["issues"]) for c in resources["certificates"]],
        identity=identity)
    save(output / "attachment_ocr_summary.json", info)
    return resources, ordered, [paths[n] for n in sorted(pages)], info


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit("Internal worker: --worker JOB_JSON")
    worker(read_json(sys.argv[2]))
