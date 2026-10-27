"""Recover observed entry rows, never manufacture missing cells or OCR values."""
from copy import deepcopy
from html import escape
import hashlib
import json
from pathlib import Path
import re

from material_ocr.upstream_legacy.extraction.glm_table import TableParser, analyze_html
from material_ocr.upstream_legacy.materials.rebar.entry_schema import FIELD_ALIASES, compact, header_matches


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def recover_html(raw, *, hit_token_limit=False, generation_stop=None, **kwargs):
    """Scope to a complete business grid; only empty repetitive tails may truncate."""
    early_stop = bool(generation_stop and generation_stop.get("triggered"))
    if early_stop:
        from material_ocr.upstream_legacy.extraction.glm_blank_stop import BlankRowDetector
        detector = BlankRowDetector()
        detector.feed(raw)
        if (not detector.triggered or generation_stop.get("reason") != "consecutive_blank_rows"
                or generation_stop.get("blank_row_limit") != detector.limit
                or generation_stop.get("stop_char_offset") != detector.stop_char_offset):
            raise ValueError("blank_stop_metadata_does_not_match_raw_output")
        # A token may include tags following the fifth closing row. Never discard text.
        suffix = raw[detector.stop_char_offset:]
        remainder = re.sub(r"<[^>]*>", "", suffix).strip().lower()
        if remainder and not any(tag.startswith(remainder) for tag in
                                 ("<tr>", "<td>", "<th>", "</tbody>", "</table>")):
            raise ValueError("nonempty_content_after_blank_stop")
        raw = raw[:detector.stop_char_offset]
    if len(raw) > 2_000_000 or len(re.findall(r"<table\b", raw, re.I)) != 1:
        raise ValueError("recovery_requires_one_bounded_table")
    matches = list(re.finditer(r"<tr\b[^>]*>.*?</tr\s*>", raw, re.I | re.S))
    if not matches:
        raise ValueError("recovery_no_complete_rows")
    if re.search(r"<(?:script|style|iframe)\b", raw, re.I):
        raise ValueError("unexpected_active_content_in_table")
    for left, right in zip(matches, matches[1:]):
        gap = raw[left.end():right.start()]
        if re.sub(r"</?(?:thead|tbody|tfoot)\b[^>]*>", "", gap, flags=re.I).strip():
            raise ValueError("unparsed_content_between_rows")
    rows = []
    for match in matches:
        parser = TableParser()
        parser.feed("<table>" + match.group() + "</table>")
        parser.close()
        if len(parser.tables) != 1 or len(parser.tables[0]) != 1:
            raise ValueError("recovery_invalid_row")
        rows.append(parser.tables[0][0])
    def empty(row):
        return bool(row) and all(not c["raw_text"].strip() and
                                 c["rowspan"] == c["colspan"] == 1 for c in row)
    blank_tail = 0
    for row in reversed(rows):
        if not empty(row):
            break
        blank_tail += 1
    tail = raw[matches[-1].end():]
    # Do not salvage a truncated material row, active content, or a second table.
    tail_tokens = re.findall(r"</?([a-zA-Z0-9]+)\b[^>]*>", tail)
    tail_empty = (not re.sub(r"<[^>]*>", "", tail).strip()
                  and all(t.lower() in {"tr", "td", "th", "tbody", "thead", "tfoot",
                                       "table", "body", "html"} for t in tail_tokens))
    unclosed = not re.search(r"</table\s*>", tail, re.I)
    if (hit_token_limit or unclosed) and not ((blank_tail >= 16 or early_stop) and tail_empty):
        raise ValueError("truncated_nonempty_or_unverified_tail")
    starts = [i for i, row in enumerate(rows)
              if any(compact(c["raw_text"]) == "序号" for c in row)
              and sum(any(header_matches(k, c["raw_text"], aliases) for c in row)
                      for k, aliases in FIELD_ALIASES.items()) >= 3]
    if len(starts) != 1:
        raise ValueError("recovery_business_header_missing_or_ambiguous")
    start = starts[0]
    stop = len(rows)
    for i in range(start + 1, len(rows)):
        nonempty = [compact(c["raw_text"]) for c in rows[i] if c["raw_text"].strip()]
        if nonempty and nonempty[0].startswith(("检验结论", "检查意见", "验收意见", "签字栏", "合计")):
            stop = i
            break
    repeated = (blank_tail >= 16 or early_stop) and stop == len(rows)
    if repeated:
        stop = len(rows) - blank_tail + 1  # one observed blank representative
    normalized = "<table>" + "".join(m.group() for m in matches[start:stop]) + "</table>"
    result = analyze_html(normalized, **kwargs)  # strict rectangular body validation stays
    scoped = True
    if not (early_stop or hit_token_limit or unclosed):
        # Valid HTML may have failed only because of the older field parser.
        # Preserve its complete grid and do not claim its footer was malformed.
        try:
            original = analyze_html(raw, **kwargs)
        except ValueError:
            original = None
        if original and original["records"] and not original["table_issues"]:
            result, normalized, scoped = original, raw, False
    if not result["records"] or result["table_issues"]:
        raise ValueError("recovered_business_grid_not_resolved")
    # Required business values must actually be present, including unit evidence.
    for record in result["records"]:
        missing = [k for k in ("material_name", "diameter_mm", "weight_t") if not record.get(k)]
        if missing:
            raise ValueError("recovered_business_values_incomplete: "
                f"page={record['page_no']} row={record.get('row_no')} missing={','.join(missing)}; "
                + "; ".join(record.get("issues", [])))
    warning = ("按已识别业务表头独立解析，保留完整业务行；请核对原图" if scoped else
               "使用更新后的字段解析规则重新解析原始 HTML；请核对原图")
    if repeated:
        warning += "；检测到连续空行循环，保留完整业务行，页面后续内容完整性未确认"
    if hit_token_limit or unclosed:
        warning += "；模型输出截断，只恢复截断前完整行，未补造后续内容"
    if early_stop:
        warning += "；连续 5 个空行触发主动停止，后续内容未识别，请核对原页"
    audit = dict(method="business_grid_scope_v1" if scoped else "validated_html_reparse_v1",
                 original_row_indices=list(range(start, stop)) if scoped else list(range(len(rows))),
                 observed_complete_rows=len(rows), trailing_blank_rows=blank_tail,
                 hit_token_limit=hit_token_limit, output_unclosed=unclosed,
                 generation_stop=generation_stop,
                 page_completeness_verified=False, warning=warning)
    result["parse_recovery"] = audit
    result["summary"].update(status="needs_review", parse_recovered=True)
    for record in result["records"]:
        record["issues"].append(warning)
        record["evidence_warnings"].append(warning)
        record["status"] = record["search_fields_status"] = "needs_review"
        record["parse_recovery"] = deepcopy(audit)
        for field in record["field_evidence"].values():
            field["html_recovery"] = deepcopy(audit)
            field["normalized_html"] = f"pages/page_{kwargs['page_no']:03d}/glm/recovered.html"
    return result, normalized


def repair_folder(folder):
    """Reparse failed pages in a NEW run/copy; retain all original raw outputs."""
    from material_ocr.upstream_legacy.extraction.batch import duplicate_candidates
    from material_ocr.upstream_legacy.extraction.glm_backend import write_review
    folder = Path(folder)
    manifest = read(folder / "page_index.json")
    data = read(folder / "records_all_pages.json")
    audits = []
    for page in manifest["pages"]:
        if page["decision"] != "target":
            continue
        number = page["page_no"]
        glm = folder / f"pages/page_{number:03d}/glm"
        if not all((glm / n).is_file() for n in ("raw.txt", "inference.json", "input_meta.json")):
            continue
        raw = (glm / "raw.txt").read_text(encoding="utf-8")
        info, meta = read(glm / "inference.json"), read(glm / "input_meta.json")
        if (page.get("extraction_status") == "draft_ready_pending_review"
                and not info.get("generation_stop", {}).get("triggered")):
            continue
        if info["input_image_sha256"] != meta["input_image_sha256"]:
            raise ValueError("GLM saved input image hash mismatch")
        try:
            result, normalized = recover_html(raw, hit_token_limit=info.get("hit_token_limit", False),
                generation_stop=info.get("generation_stop"),
                form_type=page.get("entry_form_type") or "inspection_record", page_no=number,
                pdf_sha=data["pdf_sha256"], input_size=meta["input_size"])
        except ValueError as exc:
            page.setdefault("pre_recovery_extraction_error", page.get("extraction_error"))
            page["extraction_error"] = str(exc)
            audits.append(dict(page_no=number, recovered=False, error=str(exc)))
            continue
        result["provenance"] = dict(pdf_sha256=data["pdf_sha256"], page_no=number,
            input_meta=meta, inference=info, raw_output_sha256=hashlib.sha256(raw.encode()).hexdigest())
        save(glm / "records.json", result)
        save(glm / "summary.json", result["summary"])
        (glm / "recovered.html").write_text(normalized, encoding="utf-8")
        write_review(glm, result)
        reference = f"pages/page_{number:03d}/glm/records.json"
        data["records"] = [r for r in data["records"] if r["page_no"] != number] + [
            dict(r, source_result=reference) for r in result["records"]]
        data["blank_candidates"] = [b for b in data.get("blank_candidates", []) if b["page_no"] != number] + result["blank_candidates"]
        # This status authorizes draft matching, never human approval or completeness.
        page["original_extraction_error"] = page.pop("extraction_error", None)
        page.update(extraction_status="draft_ready_pending_review", result_path=reference,
                    summary=result["summary"], parse_recovery=result["parse_recovery"])
        audits.append(dict(page_no=number, recovered=True, records=len(result["records"]), **result["parse_recovery"]))
    data["records"].sort(key=lambda r: (r["page_no"], r["grid_rows"][0]))
    data["duplicate_candidates"] = duplicate_candidates(data["records"])
    data["extraction_failures"] = [p for p in manifest["pages"] if p.get("extraction_error")]
    data["upstream_recovery"] = audits
    manifest["upstream_recovery"] = audits
    save(folder / "records_all_pages.json", data)
    save(folder / "page_index.json", manifest)
    save(folder / "recovery_report.json", dict(pages=audits, new_ocr_executed=False,
                                              note="恢复业务行供草稿匹配，不确认整页完整性"))
    rows = []
    for a in audits:
        n = a["page_no"]
        rows.append(f'<tr><td>{n}</td><td>{a.get("records", 0)}</td>'
                    f'<td>{escape(a.get("warning") or a.get("error", ""))}</td>'
                    f'<td><a href="pages/page_{n:03d}/glm/review.html">字段核对</a> · '
                    f'<a href="pages/page_{n:03d}/glm/raw.txt">原始输出</a></td></tr>')
    (folder / "recovery_report.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>进场表解析恢复报告</title>'
        '<style>body{font:16px/1.7 sans-serif;margin:30px}table{border-collapse:collapse}'
        'td,th{padding:12px;border:1px solid #ccc}th{background:#eaf1f7}</style>'
        '<h1>进场表解析恢复报告</h1><p>仅恢复已完整输出的业务行，不补造字段。'
        '空行循环或截断页仍需对照原图检查是否漏行。原页面分类报告保留历史失败状态，'
        '本次恢复情况以此报告和 page_index.json 为准。</p>'
        '<table><tr><th>页码</th><th>恢复记录数</th><th>待核验原因</th><th>证据</th></tr>'
        + ''.join(rows) + '</table>', encoding="utf-8")
    return audits
