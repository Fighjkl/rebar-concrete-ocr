"""钢筋进场表教学实验：PDF -> 几何预览 -> 人工确认 -> OCR -> 逐行JSON。

首版范围：正向扫描、已标定的有线固定表。拒绝行列检测异常；不静默补行。
"""
import argparse
import hashlib
import html
import json
from pathlib import Path
import sys
import platform
import importlib.metadata

import cv2
import numpy as np

from material_ocr.upstream_legacy.materials.rebar.parsing import parse_row
from material_ocr.upstream_legacy.extraction.blank_rows import ink_evidence, classify_row
from material_ocr.upstream_legacy.extraction.row_retry import needs_retry, retry_row
from material_ocr.upstream_legacy.materials.rebar.validation import validation_states
from material_ocr.upstream_legacy.paths import rebar_config


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_image(path):
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"无法读取图片: {path}")
    return image


def save_image(path, image):
    success, buffer = cv2.imencode(".png", image)
    if not success:
        raise ValueError("PNG编码失败")
    buffer.tofile(str(path))


def render(args):
    import pypdfium2 as pdfium
    target = Path(args.out)
    target.mkdir(parents=True, exist_ok=False)
    pdf = pdfium.PdfDocument(args.pdf)
    try:
        if not 1 <= args.page <= len(pdf):
            raise ValueError("页码从1开始，不得超过PDF总页数")
        page = pdf[args.page - 1]
        try:
            bitmap = page.render(scale=args.dpi / 72)
            try:
                image = bitmap.to_pil()
                try:
                    pixel_size = list(image.size)
                    image.save(target / "page.png")
                finally:
                    image.close()
            finally:
                bitmap.close()
        finally:
            page.close()
        write_json(target / "page_meta.json", {
            "pdf_name": Path(args.pdf).name, "pdf_sha256": digest(args.pdf),
            "page_no": args.page, "dpi": args.dpi, "pixel_size": pixel_size,
            "image_sha256": digest(target / "page.png"),
            "coordinate_system": "rendered_page_pixels_top_left",
        })
    finally:
        pdf.close()
    print(f"打开 {target / 'page.png'} 确认方向正确。")


def peaks(projection, threshold):
    indices = np.flatnonzero(projection >= threshold)
    if not len(indices):
        return []
    groups = np.split(indices, np.where(np.diff(indices) > 1)[0] + 1)
    return [int(round(float(g.mean()))) for g in groups]


def geometry(image, config):
    height, width = image.shape[:2]
    column_boundaries = config['column_boundaries']
    if config.get('auto_align', False):
        from material_ocr.upstream_legacy.extraction.alignment import locate_body
        src, alignment = locate_body(image, config)
        if config.get('adaptive_columns'):
            column_boundaries = alignment['detected_column_boundaries']
    else:
        src = np.array(config["quad_normalized"], np.float32) * [width, height]
    if src.shape != (4, 2) or not cv2.isContourConvex(src.astype(np.float32)):
        raise ValueError("四角必须按左上、右上、右下、左下顺序形成凸四边形")
    if np.any(src < 0) or np.any(src > [width - 1, height - 1]):
        raise ValueError("四角超出页面范围")
    w, h = config["target_size"]
    dst = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
    matrix = cv2.getPerspectiveTransform(src.astype(np.float32), dst)
    table = cv2.warpPerspective(image, matrix, (w, h), borderValue=(255, 255, 255))
    gray = cv2.cvtColor(table, cv2.COLOR_BGR2GRAY)
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY_INV, 35, 15)
    horizontal = cv2.morphologyEx(bw, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (max(40, w // 12), 1)))
    vertical = cv2.morphologyEx(bw, cv2.MORPH_OPEN,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(30, h // 8))))
    # 四角人工圈定的是明细外框，边界本身使用0和末像素；内部线必须检测到。
    yp = peaks((horizontal > 0).mean(axis=1), config["line_coverage"])
    yp = [y for y in yp if 15 < y < h - 16]
    ys = [0] + yp + [h - 1]
    errors = []
    if len(ys) - 1 != config["expected_rows"]:
        errors.append(f"row_count:{len(ys)-1},expected:{config['expected_rows']}")
    heights = np.diff(ys)
    if len(heights) and (heights.min() < .55 * np.median(heights)
                         or heights.max() > 1.8 * np.median(heights)):
        errors.append("row_spacing_abnormal")
    xp = peaks((vertical > 0).mean(axis=0), config["line_coverage"])
    if config.get('auto_align') and config.get('adaptive_columns'):
        from material_ocr.upstream_legacy.extraction.alignment import adaptive_columns
        if adaptive_columns(xp, w, config) is None:
            errors.append('column_grid_incomplete_or_layout_changed')
    xs = [0]
    for fraction in column_boundaries[1:-1]:
        expected = fraction * (w - 1)
        candidates = [x for x in xp if abs(x - expected) <= w * .012]
        if len(candidates) != 1:
            errors.append(f"column_boundary_unresolved:{fraction}")
            xs.append(round(expected))  # 仅供预览；有error不允许OCR继续。
        else:
            xs.append(candidates[0])
    xs.append(w - 1)
    if any(b <= a for a, b in zip(xs, xs[1:])):
        errors.append("column_order_invalid")
    return table, matrix, xs, ys, errors, horizontal, vertical


def preview(args):
    run = Path(args.run)
    config = read_json(args.template)
    image = load_image(run / "page.png")
    meta = read_json(run / "page_meta.json")
    if digest(run / "page.png") != meta["image_sha256"]:
        raise ValueError("页面图已修改，请重新render以保持来源一致")
    from material_ocr.upstream_legacy.ocr.orientation import rotate_page
    rotation = getattr(args,'rotation',0)
    upright, rotation_matrix = rotate_page(image,rotation)
    table, matrix, xs, ys, errors, horizontal, vertical = geometry(upright, config)
    matrix = matrix @ rotation_matrix  # 所有单元格坐标始终映射回未旋转原页面。
    save_image(run / "table.png", table)
    save_image(run / "lines.png", cv2.bitwise_or(horizontal, vertical))
    overlay = table.copy()
    for x in xs:
        cv2.line(overlay, (x, 0), (x, len(table)-1), (255, 0, 0), 2)
    for y in ys:
        cv2.line(overlay, (0, y), (table.shape[1]-1, y), (0, 0, 255), 2)
    for i, (y1, y2) in enumerate(zip(ys, ys[1:]), 1):
        cv2.putText(overlay, f"R{i}", (8, y1 + 30), cv2.FONT_HERSHEY_SIMPLEX,
                    .75, (0, 120, 0), 2)
    save_image(run / "grid_preview.png", overlay)
    page_overlay = image.copy()
    w,h = config['target_size']
    quad = cv2.perspectiveTransform(np.float32([[[0,0],[w-1,0],[w-1,h-1],[0,h-1]]]),np.linalg.inv(matrix))[0]
    cv2.polylines(page_overlay, [quad.astype(np.int32)], True, (0, 0, 255), 3)
    save_image(run / "page_preview.png", page_overlay)
    manifest = {
        "image_sha256": digest(run / "page.png"), "template": config,
        "matrix_page_to_table": matrix.tolist(), "xs": xs, "ys": ys,
        "geometry_errors": errors,
        "alignment_mode": "automatic" if config.get('auto_align') else "fixed_coordinates",
        "quad_page": quad.tolist(),
        "rotation_ccw": rotation,
    }
    write_json(run / "geometry.json", manifest)
    print(json.dumps({"rows": len(ys)-1, "errors": errors}, ensure_ascii=False))
    print("检查page_preview.png与grid_preview.png；错误时调整模板后重新preview。")


def confirm(args):
    run = Path(args.run)
    manifest = read_json(run / "geometry.json")
    if manifest["geometry_errors"]:
        raise ValueError("几何检查未通过，不能确认")
    if digest(run / "page.png") != manifest["image_sha256"]:
        raise ValueError("页面已变更，请重新preview")
    write_json(run / "geometry_review.json", {
        "geometry_sha256": digest(run / "geometry.json"),
        "reviewer": args.reviewer,
        "statement": "人工确认表头/列含义、明细范围和每条行线正确；未确认OCR文字",
    })
    print("已保存本页几何确认，不代表OCR结果已核实。")


def ocr_cell(engine, crop, path):
    save_image(path, crop)
    result = list(engine.predict(input=str(path)))
    if len(result) != 1:
        raise ValueError("OCR返回数量异常")
    result[0].save_to_json(str(path.with_suffix(".json")))
    data = read_json(path.with_suffix(".json"))
    data = data.get("res", data)
    texts, scores, polys = data["rec_texts"], data["rec_scores"], data["rec_polys"]
    if not (len(texts) == len(scores) == len(polys)):
        raise ValueError("OCR文字/分数/框数量不一致")
    blocks = [{"text": t, "score": float(s), "poly_crop": p}
              for t, s, p in zip(texts, scores, polys) if t.strip()]
    # 每格窄列为主；把同一文字行的碎片按x排序，再按行自上而下连接。
    lines = []
    for block in sorted(blocks, key=lambda b: np.array(b["poly_crop"])[:, 1].mean()):
        points = np.array(block["poly_crop"])
        cy, bh = points[:, 1].mean(), np.ptp(points[:, 1])
        if lines and abs(cy - lines[-1]["cy"]) < .45 * max(bh, lines[-1]["height"]):
            lines[-1]["blocks"].append(block)
        else:
            lines.append({"cy": cy, "height": bh, "blocks": [block]})
    text_lines = []
    for line in lines:
        line["blocks"].sort(key=lambda b: np.array(b["poly_crop"])[:, 0].min())
        text_lines.append("".join(b["text"] for b in line["blocks"]))
    return "\n".join(text_lines), blocks


def create_ocr(device):
    from paddleocr import PaddleOCR
    return PaddleOCR(device=device,
        text_detection_model_name="PP-OCRv5_server_det",
        text_recognition_model_name="PP-OCRv5_server_rec",
        use_doc_orientation_classify=False, use_doc_unwarping=False,
        use_textline_orientation=False, text_rec_score_thresh=0.0)


def extract(args, engine=None):
    run = Path(args.run)
    manifest = read_json(run / "geometry.json")
    approved = False
    if (run / "geometry_review.json").exists():
        approval = read_json(run / "geometry_review.json")
        approved = approval["geometry_sha256"] == digest(run / "geometry.json")
    if not approved and not getattr(args, "allow_draft", False):
        raise ValueError("几何尚未确认或已改变，需要检查并confirm")
    if manifest["geometry_errors"] or manifest["image_sha256"] != digest(run / "page.png"):
        raise ValueError("几何无效或图片已改变")
    target = run / args.result_dir
    target.mkdir(exist_ok=False)  # 保留旧实验，避免覆盖人工核对结果。
    if engine is None:
        engine = create_ocr(args.device)
    from material_ocr.upstream_legacy.ocr.cell import FixedCellOCR
    if not isinstance(engine, FixedCellOCR):
        engine = FixedCellOCR(engine,args.device)
    print('cell OCR: preserve upright text; automatic 90-degree crop rotation disabled',flush=True)
    config = manifest["template"]
    image = load_image(run / "page.png")
    matrix = np.array(manifest["matrix_page_to_table"])
    table = cv2.warpPerspective(image, matrix, tuple(config["target_size"]),
                               borderValue=(255, 255, 255))
    inverse = np.linalg.inv(matrix)
    xs, ys = manifest["xs"], manifest["ys"]
    cells, records, blank_rows = {}, [], []
    expected_no = config.get("first_row_no")
    page_meta = read_json(run / "page_meta.json")
    page_no = page_meta["page_no"]
    (target / "cells").mkdir()
    for row_index, (y1, y2) in enumerate(zip(ys, ys[1:]), 1):
        raw, refs, ocr_issues, ink = {}, {}, [], {}
        for col_index, field in enumerate(config["column_names"]):
            x1, x2 = xs[col_index:col_index+2]
            pad = config["crop_inset_px"]
            box = [x1 + pad, y1 + pad, x2 - pad, y2 - pad]
            a, b, c, d = box
            if c <= a or d <= b:
                raise ValueError("单元格裁剪为空")
            cell_id = f"p{page_no}_r{row_index:02d}_{field}"
            crop_path = target / "cells" / f"{cell_id}.png"
            raw_text, blocks = ocr_cell(engine, table[b:d, a:c], crop_path)
            ink[field] = ink_evidence(table[b:d, a:c])
            quad = np.float32([[[a,b],[c,b],[c,d],[a,d]]])
            polygon = cv2.perspectiveTransform(quad, inverse)[0].tolist()
            for block in blocks:
                poly = np.array(block["poly_crop"], np.float32) + [a, b]
                block["poly_page"] = cv2.perspectiveTransform(
                    poly.astype(np.float32)[None], inverse)[0].tolist()
            low = not blocks or any(bk["score"] < config["min_ocr_score"] for bk in blocks)
            if low and col_index < 5:
                ocr_issues.append(f"low_or_missing_ocr:{field}")
            cells[cell_id] = {"field": field, "raw_text": raw_text,
                "polygon_page": polygon, "blocks": blocks, "ink_evidence": ink[field],
                "crop_path": f"cells/{cell_id}.png"}
            raw[field], refs[field] = raw_text, cell_id
        row_kind = classify_row(raw, ink)
        if row_kind == "blank_candidate":
            blank_rows.append({"physical_row_index": row_index,
                "status": "blank_candidate", "source_cells": refs,
                "reason": "all_columns_no_ocr_text_and_no_significant_residual_ink",
                "human_verified": False})
            print(f"row {row_index}/{len(ys)-1}: blank_candidate", flush=True)
            continue
        effective = dict(raw)
        row_cell = cells[refs['row_no']]
        retry = None
        if needs_retry(raw['row_no'],row_cell['blocks'],config['min_ocr_score'],expected_no):
            retry = retry_row(engine,load_image(target/row_cell['crop_path']),raw['row_no'],
                              target/'retries',refs['row_no'],config['min_ocr_score'],ocr_cell,direct=engine.direct_ocr)
            row_cell['retry'] = retry
            print(f"row {row_index}: {retry['status']}",flush=True)
            if retry['selected_text'] is not None:
                effective['row_no'] = retry['selected_text']
                ocr_issues = [i for i in ocr_issues if i!='low_or_missing_ocr:row_no']
            else:
                ocr_issues.append('row_retry_unresolved')
        record = parse_row(effective, expected_no)
        if row_kind == "unreadable":
            record["issues"].append("visible_ink_but_no_ocr_text")
        # 空白行不递增业务序号；读不到序号时保留异常，不能猜测一个数字。
        expected_no = record["row_no"] + 1 if record["row_no"] is not None else None
        record["issues"].extend(ocr_issues)
        record.update({"record_id": f"p{page_no}_r{row_index:02d}",
            "physical_row_index": row_index, "raw_cells": raw,
            "effective_cells": effective,
            "row_number_source": retry['method'] if retry and retry['selected_text'] is not None else "initial_ocr",
            "row_number_retry": retry,
            "source_cells": refs, "ready_for_search": False,
            "field_evidence": {name: dict(cell_id=cid, polygon_page=cells[cid]['polygon_page'],
                raw_text=cells[cid]['raw_text'], blocks=cells[cid]['blocks'],
                source_pixel_size=page_meta.get('pixel_size'),
                coordinate_system=page_meta.get('coordinate_system'),
                crop_path=cells[cid]['crop_path']) for name, cid in refs.items()},
            "status": "needs_review" if record["issues"] else "rule_pass_review_required"})
        record.update(validation_states(record))
        records.append(record)
        print(f"row {row_index}/{len(ys)-1}: {record['status']}", flush=True)
    versions = {}
    for name in ["paddleocr", "paddlex", "paddlepaddle-gpu", "opencv-contrib-python", "numpy"]:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not_installed"
    output = {"schema_version":"v6", "cell_crop_policy":"fixed_cell_preserve_upright_v6", "page": page_meta,
        "geometry_review_status": "human_confirmed" if approved else "pending_human_review",
        "template_id": config["template_id"], "device_requested": args.device,
        "versions": versions, "python": sys.version, "platform": platform.platform(),
        "records": records, "blank_rows": blank_rows, "cells": cells,
        "summary": {"physical_rows":len(ys)-1, "record_count":len(records),
                    "blank_candidate_count":len(blank_rows),
                    "row_retry_count":sum(r['row_number_retry'] is not None for r in records),
                    "row_retry_accepted_count":sum(r['row_number_source']!='initial_ocr' for r in records),
                    "search_fields_needs_review_count":sum(r['search_fields_status']=='needs_review' for r in records)},
        "blank_detection_note": "规则空白候选，需核对；极淡字迹、误裁剪仍可能漏检"}
    write_json(target / "result.json", output)
    items = []
    for record in records:
        images = "".join(f'<td><img src="{cells[cid]["crop_path"]}"><pre>{html.escape(cells[cid]["raw_text"])}</pre></td>'
                         for cid in record["source_cells"].values())
        summary = {k: record[k] for k in ["row_no","diameter_mm","weight_t","manufacturer","certificate_no","status","issues","row_number_status","search_fields_status","row_number_source"]}
        retry_html = ''
        if record['row_number_retry']:
            retry_html = '<p>序号重试证据（初次原文仍保留在左侧）</p>' + ''.join(
                (f'<a href="{a["crop_path"]}">重试图</a>' if 'crop_path' in a else '')+
                f'<pre>{html.escape(a.get("text","") or a.get("error",""))}</pre>' for a in record['row_number_retry']['attempts'])
        items.append(f'<tr>{images}<td><pre>{html.escape(json.dumps(summary,ensure_ascii=False,indent=2))}</pre>{retry_html}</td></tr>')
    for row in blank_rows:
        images = ''.join(f'<td><img src="{cells[cid]["crop_path"]}"></td>' for cid in row["source_cells"].values())
        items.append(f'<tr>{images}<td>物理行{row["physical_row_index"]}：空白候选，未人工确认</td></tr>')
    (target / "review.html").write_text('<!doctype html><meta charset="utf-8"><title>逐格核对</title>'
        '<style>body{font-family:sans-serif;padding:24px}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:10px;vertical-align:top}img{max-width:260px}pre{white-space:pre-wrap}</style>'
        '<h1>逐格人工核对：规则通过不等于文字正确</h1><p>有内容/疑难行在前，空白候选在后并保留物理行索引。核对所有空白候选，勿遗漏极淡字迹。不会自动放行检索。</p>'
        '<table><tr><th>序号</th><th>名称</th><th>规格</th><th>重量</th><th>厂家＋证书号</th><th>检验项目</th><th>检验结果</th><th>备注</th><th>解析结果</th></tr>'
        + ''.join(items) + '</table>', encoding="utf-8")
    print(f"输出: {target / 'result.json'} 与 review.html")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("render")
    p.add_argument("--pdf", required=True)
    p.add_argument("--page", type=int, required=True)
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--out", required=True)
    p.set_defaults(func=render)
    p = sub.add_parser("preview")
    p.add_argument("--run", required=True)
    p.add_argument("--template", default=str(rebar_config('inspection_11_rows.json')))
    p.add_argument("--rotation",type=int,choices=[0,90,180,270],default=0)
    p.set_defaults(func=preview)
    p = sub.add_parser("confirm")
    p.add_argument("--run", required=True)
    p.add_argument("--reviewer", required=True)
    p.set_defaults(func=confirm)
    p = sub.add_parser("extract")
    p.add_argument("--run", required=True)
    p.add_argument("--device", default="gpu:0")
    p.add_argument("--result-dir", default="ocr_v1")
    p.set_defaults(func=extract)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
