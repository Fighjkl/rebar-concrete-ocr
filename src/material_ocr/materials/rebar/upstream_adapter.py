"""Input contract only: yesterday's upstream -> current GLM ledger resources."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_upstream(data, manifest):
    """Never silently run matching with a missing or partly failed entry table."""
    targets = [p for p in manifest["pages"] if p["decision"] == "target"]
    problems = []
    if not targets:
        problems.append("没有筛出进场表，请查看 upstream/index.html")
    for page in manifest["pages"]:
        if page["decision"] in ("target", "submission"):
            if page.get("extraction_status") != "draft_ready_pending_review":
                problems.append(f"第 {page['page_no']} 页提取未完成：{page.get('extraction_error') or page.get('extraction_status')}")
    for page in targets:
        if not any(r["page_no"] == page["page_no"] for r in data.get("records", [])):
            problems.append(f"第 {page['page_no']} 页没有业务记录")
    if problems:
        raise ValueError("；".join(problems))


def adapt_records(data, folder):
    """Keep parsed values unchanged and retain real HTML evidence, never boxes."""
    folder = Path(folder)
    result = []
    for source in data["records"]:
        record = deepcopy(source)
        page = record["page_no"]
        path = folder / record["source_result"]
        ref = dict(checkpoint_reference=str(path.resolve()), checkpoint_sha256=file_hash(path))
        record["evidence"] = dict(page_no=page, source_role="inspection",
            method="glm_full_page_html", coordinate_system="html_logical_grid",
            precision="page", polygon_page=None, logical_row=(record.get("grid_rows") or [None])[0],
            render_dpi=300, confidence_known=False, **ref)
        for evidence in record.get("field_evidence", {}).values():
            evidence.update(ref, page_no=page, source_role="inspection",
                method="glm_full_page_html", precision="html_cell", confidence_known=False)
            evidence["logical_row"] = (evidence.get("grid") or {}).get("row_start")
            for name in ("source_image", "raw_model_output"):
                if evidence.get(name):
                    evidence[name] = str((folder / evidence[name]).resolve())
        record["issues"] = list(record.get("issues", [])) + list(record.get("evidence_warnings", []))
        result.append(record)
    return result


def make_checkpoints(data, manifest, folder):
    """Explicit roles keep attachment recovery from reinterpreting upstream."""
    folder = Path(folder)
    forms = {f["page_no"]: f for f in data.get("submission_forms", [])}
    result = []
    for page in manifest["pages"]:
        if page["decision"] not in ("target", "submission"):
            continue
        number = page["page_no"]
        source = folder / page["result_path"]
        cp = dict(page_no=number, pdf_sha256=data["pdf_sha256"], status="completed",
            upstream_role="entry" if page["decision"] == "target" else "submission",
            dpi=300, rotation_ccw=page.get("rotation_ccw", 0), regions=[],
            checkpoint_reference=str(source.resolve()), checkpoint_sha256=file_hash(source))
        if page["decision"] == "target":
            glm = source.parent
            cp["raw_text"] = (glm / "raw.txt").read_text(encoding="utf-8")
            meta = read_json(glm / "input_meta.json")
            cp["pixel_size"] = meta["original_size"]
        else:
            form = deepcopy(forms[number])
            cp["submission_form"] = form
            cp["pixel_size"] = form.get("page_pixel_size")
            cp["raw_text"] = "\n".join(str(f.get("raw_text", "")) for f in form["fields"].values())
        result.append(cp)
    return result


def submission_context(checkpoint, new_field):
    """Supply observed cropped fields to the ORIGINAL multi-report aggregator."""
    form = checkpoint["submission_form"]
    page = checkpoint["page_no"]
    mapping = {"document_no": "文件编号", "supply_date": "供货日期与工程材料报审签认日期",
               "use_location": "使用部位", "material_name": "材料名称"}
    fields = {}
    for name, column in mapping.items():
        field = form["fields"].get(name, {})
        evidence = dict(page_no=page, polygon_page=field.get("polygon_page"),
            source_pixel_size=form.get("page_pixel_size"), render_dpi=form.get("render_dpi", 300),
            coordinate_system=form.get("coordinate_system"),
            precision="region" if field.get("polygon_page") else "page",
            method="paddle_submission_crop", source_role="submission",
            raw_text=field.get("raw_text", ""),
            checkpoint_reference=checkpoint["checkpoint_reference"],
            checkpoint_sha256=checkpoint["checkpoint_sha256"])
        issues = list(field.get("issues", [])) + list(form.get("issues", []))
        fields[column] = new_field(field.get("value"), page, evidence, issues)
    fields["供应商是否报审"] = new_field("", page, issues=[
        "报审表存在，未识别供应商是否报审的明确字段，待最终核验"])
    return fields
