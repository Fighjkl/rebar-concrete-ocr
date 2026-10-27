"""Self-contained draft review: visible values, source pages and unresolved notes."""
from html import escape
from pathlib import Path


def write_report(path, ledger):
    def esc(value):
        return escape(str(value if value is not None else ""))
    records = ledger.get("records", [])
    columns = list(records[0]["fields"]) if records else []
    body = []
    for row in records:
        cells = []
        for col in columns:
            field = row["fields"][col]
            pages = field.get("source_pages") or ([field["page"]] if field.get("page") else [])
            note = "；".join(str(d.get("message", d)) if isinstance(d, dict) else str(d)
                            for d in field.get("diagnostics", []))
            cells.append('<td class="%s" title="%s">%s<small>%s</small></td>' % (
                "draft" if field.get("draft_filled") else "", esc(note), esc(field.get("value")),
                esc("来源页：" + ", ".join(map(str, pages))) if pages else ""))
        notes = row.get("draft_notes", [])
        body.append("<tr><td>%s</td>%s<td>%s<small>%s</small></td></tr>" % (
            esc(row.get("record_id")), "".join(cells), "<br>".join(map(esc, notes)),
            esc(row.get("match_status", ""))))
    count = sum(bool(r["fields"].get("炉批号", {}).get("value")) for r in records)
    html = """<!doctype html><html lang="zh"><meta charset="utf-8">
<title>附件修复版 · 草稿台账</title><style>
body{font:16px/1.6 system-ui;margin:28px;color:#172b42;background:#f5f7fa}
h1{margin-bottom:8px}p{max-width:1100px}.wrap{overflow:auto;background:white}
table{border-collapse:collapse;width:100%%}th,td{border:1px solid #d7dfe7;padding:12px;min-width:110px;text-align:left}
th{background:#183d60;color:white}td:last-child{min-width:360px}.draft{background:#fff2ca}
small{display:block;color:#68798a;font-size:12px}summary{cursor:pointer}
</style><h1>附件修复版 · 草稿台账</h1>
<p>炉批号非空：%s / %s。黄色单元格是新增草稿补填。填写覆盖率不等于识别准确率；
所有记录仍需核验。字符差异保留两种读数；多个不同候选不会任选一个。鼠标悬停字段可查看提示。
此文件用于查看，不替代正式核验与导出流程。</p><div class="wrap"><table>
<thead><tr><th>记录</th>%s<th>待核验说明</th></tr></thead><tbody>%s</tbody></table></div></html>""" % (
        count, len(records), "".join("<th>%s</th>" % esc(c) for c in columns), "".join(body))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
