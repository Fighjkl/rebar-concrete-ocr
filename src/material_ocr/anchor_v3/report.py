"""Self-contained review HTML: original raster + actual OCR geometry + draft pairs."""
import base64
import csv
import html
import json
from pathlib import Path

ISSUES = {'anchor_value_missing':'找到字段名但未找到值', 'anchor_value_ambiguous':'字段有多个接近的值',
          'low_or_missing_ocr_score':'文字识别分数低或未知', 'low_or_missing_length_ocr_score':'长度识别分数低或未知',
          'unusual_diameter':'非常见直径', 'length_ambiguous':'长度存在多个候选',
          'multiple_field_labels_in_one_ocr_block':'一个文字框含多个编号标签',
          'rotation_disagreement':'不同方向识别值不一致', 'pairing_ambiguous':'配对存在接近的备选',
          'batch_unmatched':'未匹配到编号', 'spec_unmatched':'未匹配到规格',
          'length_unit_unspecified':'长度单位未明确', 'anchor_ocr_variant':'字段名疑似 OCR 错字，按受限变体识别',
          'length_out_of_range':'长度数值或单位异常，未自动修正',
          'low_or_missing_anchor_ocr_score':'标签字 OCR 分数低或未知',
          'low_or_missing_value_ocr_score':'值所在文字框 OCR 分数低或未知',
          'length_not_extracted':'未提取到明确长度'}

REVIEW_STATUS = {'matched':'已配对', 'matched_with_notes':'已配对，有提示',
                 'value_review':'字段值待复核', 'pairing_review':'配对待复核'}
CHECK_LABELS = {'pairing':'配对', 'diameter':'直径值', 'number':'编号值',
                'spec_label':'规格标签', 'batch_label':'编号标签', 'length':'长度'}


def score_text(score):
    return '未知' if score is None else f'{score:.3f}'


def review_reasons(record):
    if 'checks' not in record:
        return '；'.join(ISSUES.get(k,k) for k in record['issues'])
    reasons=[]
    for key in CHECK_LABELS:
        check=record['checks'][key]
        for issue in check['issues']:
            reason=CHECK_LABELS[key]+'：'+ISSUES.get(issue,issue)
            if issue in ('low_or_missing_anchor_ocr_score','low_or_missing_value_ocr_score'):
                reason+='（'+score_text(check.get('score'))+'）'
            reasons.append(reason)
    return '；'.join(reasons)


def summary_text(s):
    if not s:return '未完成'
    if s.get('status')=='no_anchors_detected':return '没有找到字段锚点，请检查原图与 OCR 是否漏识别。'
    if 'matched_with_notes_count' in s:
        return (f'规格锚点 {s["spec_anchor_count"]} 个 · 编号锚点 {s["batch_anchor_count"]} 个 · '
                f'配对 {s["pair_count"]} 组 · 配对待复核 {s["pairing_review_count"]} 条 · '
                f'字段值待复核 {s["value_review_count"]} 条 · 仅标签/长度提示 {s["matched_with_notes_count"]} 条')
    return (f'规格锚点 {s["spec_anchor_count"]} 个 · 编号锚点 {s["batch_anchor_count"]} 个 · '
            f'配对 {s["pair_count"]} 组 · 规则通过 {s["rule_pass_count"]} 组 · 待复核记录 {s["needs_review_count"]} 条')


def esc(x):
    return html.escape(str(x), quote=True)


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def review(path, image_path, width, height, result, blocks):
    encoded=base64.b64encode(Path(image_path).read_bytes()).decode('ascii')
    fields={f['id']:f for f in result['fields']}
    overlays=[];rows=[]
    for b in blocks:
        pts=' '.join(f'{x:.2f},{y:.2f}' for x,y in b['polygon'])
        overlays.append(f'<polygon class="ocr" points="{pts}"><title>{esc(b["text"])} | {esc(b.get("score"))}</title></polygon>')
    for r in result['records']:
        color=({'matched':'#059669','matched_with_notes':'#2563eb'}.get(r.get('review_status'),'#d97706')
               if 'review_status' in r else ('#d97706' if r['issues'] else '#059669'))
        shapes=[];centers=[]
        for key in ('spec_id','batch_id'):
            f=fields.get(r[key])
            if not f:continue
            centers.append(f['g']['center'])
            for polykey in ('anchor_polygon','value_polygon'):
                if polykey in f:
                    pts=' '.join(f'{x:.2f},{y:.2f}' for x,y in f[polykey])
                    shapes.append(f'<polygon points="{pts}"><title>{esc(f.get("raw",f["anchor_text"]))}</title></polygon>')
        if len(centers)==2:
            (x1,y1),(x2,y2)=centers
            shapes.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"/>')
        if centers:
            x,y=centers[0]
            shapes.append(f'<text x="{x}" y="{y-12}" fill="{color}" stroke="white" stroke-width="1" paint-order="stroke" font-size="{max(16,width/65):.1f}">{esc(r["id"])}</text>')
        overlays.append(f'<g class="pair" id="{r["id"]}" stroke="{color}">'+''.join(shapes)+'</g>')
        s=r['specification'] or {}
        checks=r.get('checks',{})
        diameter_score=score_text(checks.get('diameter',{}).get('score'))
        number_score=score_text(checks.get('number',{}).get('score'))
        status=REVIEW_STATUS.get(r.get('review_status'),'待复核' if r['issues'] else '规则通过，待人工确认')
        rows.append(f'<tr data-pair="{r["id"]}"><td>{r["id"]}</td><td>{esc(s.get("diameter_mm","—"))}</td>'
                    f'<td>{esc(s.get("length_mm") or "—")}</td><td>{esc(r["batch_label"] or "—")}</td>'
                    f'<td class="mono">{esc(r["batch_number"] or "—")}<small class="score">编号框分数 {number_score}</small></td>'
                    f'<td>{esc(status)}<small>{esc(review_reasons(r))}</small>'
                    f'<small class="score">规格值框分数 {diameter_score}</small></td></tr>')
    evidence=[]
    for f in result['fields']:
        evidence.append({'field_id':f['id'],'anchor':f['anchor_text'],'raw':f.get('raw'),
                         'value':f['value'],'issues':f['issues'],'candidates':f['candidates'],
                         'other_views':f.get('other_views',[]),'anchor_score':f.get('anchor_score'),
                         'value_ocr_score':f.get('value_ocr_score'),
                         'value_score_scope':f.get('value_score_scope'),
                         'value_source_block_ids':f.get('value_source_block_ids'),
                         'reconstructed_label':f.get('reconstructed_label'),
                         'anchor_source_block_ids':f.get('anchor_source_block_ids')})
    content='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>铭牌字段配对核对</title>
<style>body{font:15px system-ui,sans-serif;margin:0;color:#172534;background:#f3f6fa}header{padding:18px 24px;background:white;border-bottom:1px solid #ccc}h1{font-size:22px;margin:0 0 8px}p{margin:8px 0;line-height:1.6}.layout{display:grid;grid-template-columns:55% 45%;height:calc(100vh - 180px)}.image,.results{overflow:auto;padding:14px}svg{width:100%;height:auto;background:white}polygon{fill:none;stroke-width:3;vector-effect:non-scaling-stroke}line{stroke-width:2;vector-effect:non-scaling-stroke}.ocr{stroke:#64748b;stroke-width:1;display:none}.show-ocr .ocr{display:block}.dim{opacity:.12}.active polygon{stroke-width:5}.results{background:white}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:10px 6px;border-bottom:1px solid #ddd;text-align:left}tr[data-pair]{cursor:pointer}tr.selected{background:#e0f2fe}small{display:block;color:#925010;max-width:220px;overflow-wrap:anywhere}.mono{font-family:monospace;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}button{padding:6px 12px;margin-right:10px} @media(max-width:900px){.layout{display:block;height:auto}.image,.results{max-height:75vh}}</style>
<style>.score{color:#64748b}</style>
<header><h1>整页 OCR · 铭牌规格与编号配对</h1><p>绿色：已配对。蓝色：已配对，有标签或长度提示。橙色：配对或字段值需复核。提示不等于程序报错。</p>
<p>分数来自 OCR 文字框，不是字符正确率；高分也可能漏字。所有结果尚未人工确认，请逐条对照原图。点击结果行可定位。</p>
<button id="all">显示全部</button><label><input id="boxes" type="checkbox">显示全部 OCR 文字框（悬停查看文字）</label>
<label style="margin-left:15px">图像缩放 <input id="zoom" type="range" min="100" max="300" value="100" step="25"></label></header>
<main class="layout"><section class="image"><svg id="canvas" viewBox="0 0 WIDTH HEIGHT"><image width="WIDTH" height="HEIGHT" href="data:image/png;base64,IMAGE"/>OVERLAYS</svg></section>
<section class="results"><p>SUMMARY</p><table><thead><tr><th>记录</th><th>直径 mm</th><th>长度 mm</th><th>编号原标签</th><th>编号</th><th>状态</th></tr></thead><tbody>ROWS</tbody></table>
<details><summary>字段原文、备选与问题详情</summary><pre>EVIDENCE</pre></details></section></main>
<script>const pairs=[...document.querySelectorAll('.pair')],rows=[...document.querySelectorAll('tr[data-pair]')];
rows.forEach(r=>r.onclick=()=>{rows.forEach(x=>x.classList.toggle('selected',x===r));pairs.forEach(p=>{p.classList.toggle('dim',p.id!==r.dataset.pair);p.classList.toggle('active',p.id===r.dataset.pair)})});
document.querySelector('#all').onclick=()=>{rows.forEach(r=>r.classList.remove('selected'));pairs.forEach(p=>p.classList.remove('dim','active'))};
document.querySelector('#boxes').onchange=e=>document.querySelector('#canvas').classList.toggle('show-ocr',e.target.checked);
document.querySelector('#zoom').oninput=e=>document.querySelector('#canvas').style.width=e.target.value+'%';</script></html>'''
    substitutions={'WIDTH':str(width),'HEIGHT':str(height),'IMAGE':encoded,'OVERLAYS':''.join(overlays),
                   'SUMMARY':esc(summary_text(result['summary'])), 'ROWS':''.join(rows),
                   'EVIDENCE':esc(json.dumps(evidence,ensure_ascii=False,indent=2))}
    # Single substitution pass: OCR strings cannot become template placeholders.
    import re
    content=re.sub('|'.join(substitutions),lambda m:substitutions[m.group(0)],content)
    Path(path).write_text(content,encoding='utf-8')


def safe_csv(value):
    if value is None:return ''
    text=str(value)
    return "'"+text if text.lstrip().startswith(('=','+','-','@')) else text


def write_csv(path, pages):
    with Path(path).open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(['文件','页码','记录','直径mm','长度mm','规格原文','编号标签','编号（文本）','状态','问题',
                                             '分项状态','编号值框分数','规格值框分数','编号标签分数','规格标签分数','分项说明'])
        for p in pages:
            for r in p.get('result',{}).get('records',[]):
                s=r['specification'] or {}
                checks=r.get('checks',{})
                writer.writerow([safe_csv(v) for v in [p['source'],p['page_no'],r['id'],s.get('diameter_mm'),s.get('length_mm'),
                    r['specification_raw'],r['batch_label'],r['batch_number'],r['status'],','.join(r['issues']),
                    REVIEW_STATUS.get(r.get('review_status'),r['status']),checks.get('number',{}).get('score'),
                    checks.get('diameter',{}).get('score'),checks.get('batch_label',{}).get('score'),
                    checks.get('spec_label',{}).get('score'),review_reasons(r)]])


def index(path, pages):
    rows=[]
    for p in pages:
        s=p.get('result',{}).get('summary',{})
        link=f'<a href="{esc(p["directory"])}/review.html">打开核对页面</a>' if 'result' in p else '失败'
        rows.append(f'<tr><td>{esc(p["source"])}</td><td>{p["page_no"]}</td><td>{link}</td><td>{esc(summary_text(s))}</td><td>{esc(p.get("error",""))}</td></tr>')
    Path(path).write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>铭牌测试结果</title><style>body{font:16px system-ui;margin:30px}td,th{padding:12px;border-bottom:1px solid #ddd;text-align:left}</style><h1>铭牌整页 OCR 测试结果</h1><p>所有结果均为待确认草稿；无字段锚点不代表页面没有铭牌。打开每页结果检查漏识别和串配。</p><table><tr><th>输入</th><th>页码</th><th>核对</th><th>汇总</th><th>错误</th></tr>'+''.join(rows)+'</table></html>',encoding='utf-8')
