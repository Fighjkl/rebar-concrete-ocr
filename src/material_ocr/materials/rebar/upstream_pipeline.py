"""Recover entry tables, preserve business routes and whole-page Paddle attachments."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from .upstream_adapter import (read_json, file_hash, validate_upstream,
                               adapt_records, make_checkpoints)

ROOT = Path(__file__).resolve().parents[4]


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def upstream_code_hash():
    paths = sorted((ROOT / "src/material_ocr/upstream_legacy").rglob("*.py"))
    paths += sorted((ROOT / "src/material_ocr/upstream_legacy").rglob("*.json"))
    paths += sorted(Path(__file__).parent.glob("upstream_*.py"))
    h = hashlib.sha256()
    for path in paths:
        h.update(path.relative_to(ROOT).as_posix().encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def run_worker(pdf, output, *, model_dir, glm_python, device, progress, max_tokens):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1",
        MATERIAL_OCR_GLM_PYTHON=str(glm_python),
        MATERIAL_OCR_GLM_MODEL=str(model_dir),
        MATERIAL_OCR_GLM_MAX_TOKENS=str(max_tokens),
        PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]))
    paddle_device = device.replace("cuda:", "gpu:")
    command = [sys.executable, "-u", "-m", "material_ocr.materials.rebar.upstream_worker",
               "--pdf", str(pdf), "--out", str(output / "upstream"), "--device", paddle_device]
    write_json(output / "upstream_command.json", command)
    with (output / "upstream.log").open("w", encoding="utf-8") as log:
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, encoding="utf-8", errors="replace", start_new_session=os.name == "posix")
        try:
            for line in child.stdout:
                log.write(line)
                log.flush()
                if line.startswith("UPSTREAM_PROGRESS "):
                    if progress:
                        progress(*json.loads(line.removeprefix("UPSTREAM_PROGRESS ")))
                elif progress and ("Loading" in line or "page " in line or "方向模型" in line):
                    print(line.rstrip(), flush=True)
            code = child.wait()
        finally:
            if child.poll() is None:
                from material_ocr.processes import stop_process
                stop_process(child, process_group=os.name == "posix")
    if code:
        raise RuntimeError(f"上游子进程退出 {code}，查看 {output / 'upstream.log'}")


def seal_upstream(folder, identity):
    files = {p.relative_to(folder).as_posix(): file_hash(p)
             for p in sorted(folder.rglob("*")) if p.is_file() and p.name != "reuse_manifest.json"}
    write_json(folder / "reuse_manifest.json", dict(identity=identity, files=files))


def check_reuse(folder, identity):
    seal = read_json(folder / "reuse_manifest.json")
    if seal["identity"] != identity:
        # Compatibility applies only to releases with unchanged extraction.
        # The v2 layout/parser/generation repairs require fresh upstream outputs.
        compatible = False
        proof = ROOT / "PADDLE_ANCHORS_PROVENANCE.json"
        if proof.is_file():
            provenance = read_json(proof)
            actual = dict(seal["identity"])
            previous_code = actual.pop("upstream_code_sha256", None)
            expected = dict(identity)
            expected.pop("upstream_code_sha256", None)
            compatible = (
                actual == expected
                and not provenance.get("audited_modified_upstream_files")
                and previous_code in [provenance.get("previous_upstream_identity_hash"),
                                      *provenance.get("compatible_upstream_identity_hashes", [])]
                and file_hash(Path(__file__)) == provenance.get("this_pipeline_sha256")
                and all(file_hash(ROOT / rel) == digest for rel, digest in
                        provenance.get("unchanged_upstream_files", {}).items())
                and bool(provenance.get("unchanged_upstream_files")))
        if not compatible:
            raise ValueError("上游复用的 PDF、模型、代码或参数不同，请重新运行 inspection")
    for rel, digest in seal["files"].items():
        path = (folder / rel).resolve()
        if not path.is_relative_to(folder.resolve()) or not path.is_file() or file_hash(path) != digest:
            raise ValueError(f"上游文件缺失或改变，不能复用：{rel}")


def run_pipeline(pdf, output, *, model_dir, glm_python, mode="inspection",
                 device="cuda:0", cache_dir=None, reuse_upstream=None,
                 max_tokens=16384, force_attachments=False, progress=None,
                 paddle_model_dir=None, attachment_rotations=(0,), allow_no_entry=False):
    # Import production lazily: --help and pure upstream adapter tests need no fcntl.
    from . import glm_production as prod
    if mode not in ("inspection", "full"):
        raise ValueError(mode)
    pdf, output, model_dir = (Path(p).resolve() for p in (pdf, output, model_dir))
    glm_python = Path(glm_python).expanduser().absolute()  # keep venv symlink!
    if not pdf.is_file() or not glm_python.is_file():
        raise FileNotFoundError("PDF 或 GLM Python 不存在")
    if output.exists():
        raise FileExistsError(f"请使用一个全新的结果目录：{output}")
    model = prod._ocr_identity(model_dir, "local-glm")
    digest = file_hash(pdf)
    identity = dict(pdf_sha256=digest, model_fingerprint=model["fingerprint"],
                    upstream_code_sha256=upstream_code_hash(), dpi=300,
                    prompt="Table Recognition:", max_new_tokens=max_tokens)
    output.mkdir(parents=True)
    started = prod._utc()
    run_id = output.name
    config = dict(start_time_utc=started, command=["autodl_test_upstream.py", str(pdf)],
        model_dir=str(model_dir), python_executable=str(glm_python), device=device,
        ocr_model="local-glm", new_ocr_executed=not bool(reuse_upstream),
        upstream_identity=identity, upstream_mode=mode,
        attachment_backend="local-paddle", attachment_dpi=300,
        attachment_full_page_only=True, attachment_crops=False)
    metadata_path = output / "experiment/metadata.json"
    write_json(metadata_path, dict(status="running", config_reference=config))
    try:
        folder = Path(reuse_upstream).resolve() if reuse_upstream else output / "upstream"
        if reuse_upstream:
            check_reuse(folder, identity)
            config["reused_upstream_identity"] = read_json(folder / "reuse_manifest.json")["identity"]
        else:
            if progress:
                progress("upstream", 0, 1, "Paddle 页头筛选、报审裁图、GLM 整页表格识别")
            run_worker(pdf, output, model_dir=model_dir, glm_python=glm_python,
                       device=device, progress=progress, max_tokens=max_tokens)
            from .upstream_recovery import repair_folder
            recovery = repair_folder(folder)
            if recovery and progress:
                progress("recover",sum(p["recovered"] for p in recovery),len(recovery),
                         "恢复完整业务表格；保留原始 GLM 输出及空行/截断核验提示")
        data = read_json(folder / "records_all_pages.json")
        manifest = read_json(folder / "page_index.json")
        if data["pdf_sha256"] != digest:
            raise ValueError("上游 PDF 摘要不一致")
        # The web opts into a separate no-entry branch. Target-present runs and
        # standalone CLI defaults retain the original validation/matching path.
        if mode == "full" and allow_no_entry and not any(
                p["decision"] == "target" for p in manifest["pages"]):
            if len(manifest["pages"]) != prod._pdf_page_count(pdf):
                raise ValueError("筛页结果未覆盖 PDF 全部页面")
            from .no_entry_pipeline import run as run_no_entry
            return run_no_entry(pdf, output, data=data, manifest=manifest, folder=folder,
                config=config, cache_dir=cache_dir or ROOT / "runs/paddle-anchors-attachment-cache",
                paddle_model_dir=Path(paddle_model_dir or os.environ.get(
                    "MATERIAL_OCR_PADDLE_MODELS_ROOT", ROOT / "models/paddlex_cache/official_models")),
                device=device, force_attachments=force_attachments,
                attachment_rotations=attachment_rotations, progress=progress)
        # Inspection always preserves diagnostics, even when extraction needs review.
        problems = None
        try:
            validate_upstream(data, manifest)
        except ValueError as exc:
            problems = str(exc)
        config["upstream_reference"] = str(folder)
        write_json(output / "upstream_summary.json", dict(upstream=str(folder),
            target_pages=manifest["target_pages"], submission_pages=manifest["submission_pages"],
            review_pages=manifest["review_pages"], records=len(data["records"]),
            full_pipeline_ready=not bool(problems), blocking_reason=problems,
            note="review/error 筛页保留提示；full 会将未明确筛为报审/进场的页全部交给原附件 OCR。"))
        if not reuse_upstream:
            seal_upstream(folder, identity)
        if mode == "inspection":
            write_json(metadata_path, dict(status="needs_review" if problems else "completed_pending_review",
                config_reference=config, end_time_utc=prod._utc(), gpu_validation="runtime"))
            if progress:
                progress("completed", 1, 1, f"前半部分完成；原始图、HTML 与字段：{folder}")
            return read_json(output / "upstream_summary.json")
        if problems:
            raise ValueError(problems + "；已停止在上游边界，未运行附件与台账")
        upstream_cps = make_checkpoints(data, manifest, folder)
        upstream_pages = {c["page_no"] for c in upstream_cps}
        page_count = prod._pdf_page_count(pdf)
        if {p["page_no"] for p in manifest["pages"]} != set(range(1, page_count + 1)):
            raise ValueError("筛页结果未覆盖 PDF 全部页面")
        attachments = set(range(1, page_count + 1)) - upstream_pages
        from .paddle_attachments import run_attachments
        paddle_root = Path(paddle_model_dir or os.environ.get(
            "MATERIAL_OCR_PADDLE_MODELS_ROOT", ROOT / "models/paddlex_cache/official_models"))
        resources, checkpoints, ordered, attachment_info = run_attachments(
            pdf, output, attachments, manifest,
            cache_dir=cache_dir or ROOT / "runs/paddle-anchors-attachment-cache",
            model_dir=paddle_root, device=device, force=force_attachments,
            rotations=attachment_rotations, progress=progress)
        inference = attachment_info["identity"]
        config["attachment_ocr"] = attachment_info
        config["new_ocr_executed"] |= bool(attachment_info["newly_recognized_pages"])
        # The baseline entry parser is not used for upstream pages. Unknown pages
        # still get attachment OCR; an unexpected entry title is flagged for review.
        unexpected = resources.get("records", [])
        resources["records"] = adapt_records(data, folder)
        for n in sorted({r["page_no"] for r in unexpected}):
            resources["issues"].append(dict(code="entry_on_unscreened_page", page_no=n,
                reason="附件 OCR 发现疑似进场表，未混入已筛选记录，请核对筛页结果"))
        for n in manifest["review_pages"]:
            resources["issues"].append(dict(code="header_classification_review", page_no=n,
                reason="页头分类不确定，已保留 Paddle 整页附件 OCR，需检查是否遗漏进场表"))
        checkpoints = sorted(checkpoints + upstream_cps, key=lambda c: c["page_no"])
        write_json(output / "upstream_checkpoints.json", upstream_cps)
        write_json(output / "checkpoint_sources.json", dict(pdf_sha256=digest, inference=inference,
            upstream=str(folder), references=[dict(path=str(p), sha256=file_hash(p)) for p in ordered]))
        # Attachments deliberately do not call any GLM/ROI recovery functions.
        # Only resource construction changes; the original two business routes
        # and deduplication/conflict/review/export functions remain unchanged.
        ledger = prod._build_ledger(resources, checkpoints)
        from .draft_completion import complete_drafts
        ledger = complete_drafts(ledger, resources)
        result = prod._standard_files(output, pdf, digest, ordered, resources, ledger,
            run_id=run_id, model=model, config=config, merged_checkpoints=checkpoints)
        from .draft_report import write_report
        write_report(output / "review/draft_review.html", ledger)
        # Original writer assumes no saved images. Correct the run metadata for
        # the NEW upstream while leaving its ledger/review/export output untouched.
        images = sum(1 for _ in folder.rglob("*.png"))
        metadata = read_json(metadata_path)
        attachment_images = sum(1 for _ in (output / "attachment_pages").rglob("*.png"))
        metadata.update(upstream_identity=identity, upstream_reference=str(folder),
                        upstream_images_referenced=images, attachment_images_saved=attachment_images,
                        images_saved=(images if not reuse_upstream else 0) + attachment_images,
                        ocr_backends=dict(entry="local-glm", submission="local-paddle", attachments="local-paddle"))
        metadata["storage"].update(images_saved=metadata["images_saved"])
        write_json(metadata_path, metadata)
        metrics = result["metrics"]
        metrics.update(images_saved=metadata["images_saved"], upstream_images_referenced=images)
        result["ocr_backends"] = metadata["ocr_backends"]
        for name in ("statistics.json", "summary.json", "review/match_results.json"):
            value = read_json(output / name)
            def correct_images(obj):
                if isinstance(obj, dict):
                    if "images_saved" in obj:
                        obj["images_saved"] = metadata["images_saved"]
                    for v in obj.values():
                        correct_images(v)
                elif isinstance(obj, list):
                    for v in obj:
                        correct_images(v)
            correct_images(value)
            value["ocr_backends"] = metadata["ocr_backends"]
            write_json(output / name, value)
        (output / "experiment/result.md").write_text(
            f"# 附件整页 Paddle 与铭牌锚点测试\n状态：completed_pending_human_review\n"
            f"上游证据：{folder}\n上游 PNG 证据：{images} 张；本次共保存图片：{metadata['images_saved']} 张。\n"
            "附件为 Paddle 整页 OCR + v3 铭牌锚点；停用附件裁剪和 GLM 补读。两条业务匹配路线、冲突、去重、核验及导出保持原规则。\n", encoding="utf-8")
        if progress:
            progress("completed", 1, 1, "完整流程完成，台账仍需人工核验")
        return result
    except BaseException as exc:
        write_json(metadata_path, dict(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
            error=str(exc), config_reference=config, end_time_utc=prod._utc()))
        raise
