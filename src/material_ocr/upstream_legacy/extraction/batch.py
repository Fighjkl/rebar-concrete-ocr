"""整份PDF：扫描标题 -> 候选页精扫 -> 几何检查 -> 可选OCR草稿。"""
import argparse
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import html
import os
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from material_ocr.upstream_legacy.extraction.table import create_ocr, digest, extract, ocr_cell, preview, read_json, render, write_json
from material_ocr.upstream_legacy.materials.rebar.routing import classify_page_header as classify_header
from material_ocr.upstream_legacy.materials.rebar.submission import preview_submission, extract_submission
from material_ocr.upstream_legacy.materials.rebar.submission_duplicates import submission_duplicate_candidates
from material_ocr.upstream_legacy.ocr.orientation import create_orientation, predict_orientation, rotate_page
from material_ocr.upstream_legacy.paths import rebar_config


def pdf_image(pdf, page_index, dpi):
    page = pdf[page_index]
    try:
        bitmap = page.render(scale=dpi/72)
        try:
            pil = bitmap.to_pil()
            try:
                return cv2.cvtColor(np.array(pil.convert("RGB")),cv2.COLOR_RGB2BGR)
            finally:
                pil.close()
        finally:
            bitmap.close()
    finally:
        page.close()


def duplicate_candidates(records):
    groups = defaultdict(list)
    for record in records:
        keys = [record.get(k) for k in ("diameter_mm","weight_t","manufacturer","certificate_no")]
        if any(v is None or v == "" for v in keys):
            continue
        try:
            for i in (0,1):
                value=Decimal(str(keys[i]))
                if not value.is_finite() or value<=0:
                    raise ValueError('invalid numeric business key')
                keys[i]=str(value.normalize())
        except (InvalidOperation,ValueError):
            continue
        # 只在同PDF内列候选；不改证书号大小写或前导零，不自动删行。
        groups[(record.get('pdf_sha256'),*keys)].append(record["record_id"])
    return [{"record_ids":ids,"match_fields":["diameter_mm","weight_t","manufacturer","certificate_no"],
             "row_number_used":False,"action":"review_do_not_sum_or_delete_automatically"}
            for ids in groups.values() if len(ids)>1]


def run(args, *, progress=None, header_classifier=None):
    # Resolve per invocation; a material worker must never replace global routing.
    classify_page = classify_header if header_classifier is None else header_classifier
    import pypdfium2 as pdfium
    pdf_path = Path(args.pdf).resolve()
    # 先检查输入，避免路径错误也留下输出目录。
    pdf_sha = digest(pdf_path)
    backend = getattr(args, 'entry_backend', 'glm')
    if backend not in ('template', 'adaptive', 'glm'):
        raise ValueError('entry_backend must be glm, template or adaptive')
    config = read_json(args.template) if backend == 'template' else {'entry_backend':backend,'fixed_template_used':False}
    glm_engine = None
    templates = getattr(args, 'templates', [args.template])
    out = Path(args.out)
    out.mkdir(parents=True,exist_ok=False)
    (out/"headers").mkdir()
    engine = create_ocr(args.device)
    orientation_model = None
    orientation_error = None
    try:
        orientation_model = create_orientation(args.device)
    except Exception as exc:
        orientation_error = str(exc)
        print(f'方向模型不可用，改用四方向页头OCR检查: {exc}',flush=True)
    manifest = {"pdf_name":pdf_path.name,"pdf_sha256":pdf_sha,"template":config,
                "entry_backend":backend,"status":"running",
                "dpi_scan":args.scan_dpi,"extract_draft":args.extract_draft,
                "notes":["all_pages_scanned_no_early_stop","unknown_pages_retained_for_review",
                         "no_automatic_deduplication","geometry_is_not_automatically_human_verified"],
                "orientation_model_error":orientation_error,"pages":[]}
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        for index in range(len(pdf)):
            page_no = index+1
            entry = {"page_no":page_no,"attempts":[]}
            try:
                image = pdf_image(pdf,index,args.scan_dpi)
                angle,score = 0,0.
                if orientation_model is not None:
                    try:
                        angle,score = predict_orientation(orientation_model,image,out/'headers'/f'p{page_no:03d}_orientation.json')
                    except Exception as exc:
                        entry['orientation_error'] = str(exc)
                entry['orientation_prediction'] = {'rotation_ccw':angle,'score':score}
                # 首选轻量模型预测方向；OCR仍不确定时，再试其他方向。
                angles = [angle]+[a for a in (0,90,180,270) if a!=angle]
                attempts = []
                for rotation in angles:
                    upright,_ = rotate_page(image,rotation)
                    crop = upright[:round(upright.shape[0]*.40)]
                    path = out/"headers"/f"p{page_no:03d}_{args.scan_dpi}_rot{rotation}.png"
                    _,blocks = ocr_cell(engine,crop,path)
                    result = classify_page(blocks,crop.shape[0])
                    attempt = {"dpi":args.scan_dpi,"rotation_ccw":rotation,"header_image":str(path.relative_to(out)),**result}
                    entry['attempts'].append(attempt)
                    attempts.append(attempt)
                    if result['decision'] != 'review' and (score >= .90 or result['decision'] in ('target','submission')):
                        break
                targets = [a for a in attempts if a['decision'] in ('target','submission')]
                others = [a for a in attempts if a['decision']=='other']
                decisive = targets or others
                if decisive:
                    selected = decisive[0]
                else:
                    selected = max(attempts,key=lambda a:(len(a['header_hits']),len(a['ocr_text'])))
                    image = pdf_image(pdf,index,300)
                    upright,_ = rotate_page(image,selected['rotation_ccw'])
                    crop = upright[:round(upright.shape[0]*.40)]
                    path = out/'headers'/f"p{page_no:03d}_300_rot{selected['rotation_ccw']}.png"
                    _,blocks = ocr_cell(engine,crop,path)
                    result = classify_page(blocks,crop.shape[0])
                    selected = {'dpi':300,'rotation_ccw':selected['rotation_ccw'],'header_image':str(path.relative_to(out)),**result}
                    entry['attempts'].append(selected)
                result = dict(selected)
                if targets and others or len(targets)>1:
                    result.update(decision='review',reasons=['conflicting_orientation_evidence'])
                entry.update(result)
            except Exception as exc:
                entry.update({"decision":"error","error":str(exc)})
            manifest["pages"].append(entry)
            write_json(out/"page_index.json",manifest)
            print(f"page {page_no}/{len(pdf)}: {entry['decision']}",flush=True)
            if progress:
                progress('classify', page_no, len(pdf), f'页面分类 {page_no}/{len(pdf)}')
    finally:
        pdf.close()
    records = []
    submissions = []
    blank_candidates = []
    adaptive_engine = None
    for entry in manifest["pages"]:
        if entry["decision"] not in ("target", "submission"):
            continue
        page_no = entry["page_no"]
        original = out/"pages"/f"page_{page_no:03d}"
        folder = original
        entry["run_dir"] = str(original.relative_to(out))
        try:
            render(SimpleNamespace(pdf=str(pdf_path),page=page_no,dpi=300,out=str(original)))
            if entry['decision'] == 'submission':
                submission_template = getattr(args, 'submission_template', rebar_config('submission_b06.json'))
                preview_submission(original, submission_template, entry.get('rotation_ccw', 0))
                if args.extract_draft:
                    result = extract_submission(original, args.device, engine)
                    entry['result_path'] = str((original/'submission_ocr'/'result.json').relative_to(out))
                    entry['extraction_status'] = 'draft_ready_pending_review'
                    entry['summary'] = {'fields':4, 'issues':result['issues']}
                    submissions.append({**result, 'source_result':entry['result_path']})
                else:
                    entry['extraction_status'] = 'preview_ready'
                write_json(out/'page_index.json', manifest)
                if progress:
                    done = sum('extraction_status' in p for p in manifest['pages'] if p['decision'] in ('target','submission'))
                    total = sum(p['decision'] in ('target','submission') for p in manifest['pages'])
                    progress('extract', done, total, f'报审与检验表提取 {done}/{total}')
                continue
            if backend == 'glm':
                from material_ocr.upstream_legacy.extraction.glm_backend import prepare_page
                calibration = prepare_page(original, entry.get('rotation_ccw', 0))
                entry['input_meta_path'] = str((original/'glm'/'input_meta.json').relative_to(out))
                if not args.extract_draft:
                    entry['extraction_status'] = 'glm_input_ready'
                else:
                    if glm_engine is None:
                        from material_ocr.upstream_legacy.extraction.glm_backend import GlmBackend
                        glm_engine = GlmBackend(args.device)
                        config['glm_runtime'] = glm_engine.config
                    result = glm_engine.extract(original, page_no, pdf_sha, calibration, entry.get('entry_form_type'))
                    entry['result_path'] = str((original/'glm'/'records.json').relative_to(out))
                    entry['summary'] = result['summary']
                    entry['extraction_status'] = ('draft_ready_pending_review'
                        if result['summary']['status'] == 'rule_pass_review_required' else 'glm_needs_review')
                    records.extend({**r, 'source_result':entry['result_path']} for r in result['records'])
                    blank_candidates.extend(result['blank_candidates'])
                    print(f"page {page_no}: GLM products={len(result['records'])}, status={result['summary']['status']}",flush=True)
                write_json(out/'page_index.json',manifest)
                if progress:
                    done = sum('extraction_status' in p for p in manifest['pages'] if p['decision'] in ('target','submission'))
                    total = sum(p['decision'] in ('target','submission') for p in manifest['pages'])
                    progress('extract',done,total,f'报审与检验表提取 {done}/{total}')
                continue
            if backend == 'adaptive':
                from material_ocr.upstream_legacy.extraction.adaptive import AdaptiveBackend, calibrate_page
                calibration = calibrate_page(original,entry.get('rotation_ccw',0),
                                             deskew=not getattr(args,'no_deskew',False))
                entry['calibration_path'] = str((original/'adaptive'/'calibration.json').relative_to(out))
                entry['calibration_requires_review'] = calibration['requires_review']
                if not args.extract_draft:
                    entry['extraction_status'] = 'adaptive_calibration_ready'
                else:
                    if adaptive_engine is None:
                        adaptive_engine = AdaptiveBackend(args.device)
                    result = adaptive_engine.extract(original,page_no,pdf_sha,calibration)
                    entry['result_path'] = str((original/'adaptive'/'records.json').relative_to(out))
                    entry['summary'] = result['summary']
                    entry['extraction_status'] = ('draft_ready_pending_review'
                        if result['summary']['status'] == 'rule_pass_review_required' else 'adaptive_needs_review')
                    records.extend({**r,'source_result':entry['result_path']} for r in result['records'])
                    blank_candidates.extend(result['blank_candidates'])
                    print(f"page {page_no}: adaptive products={len(result['records'])}, blanks={len(result['blank_candidates'])}, status={result['summary']['status']}",flush=True)
                write_json(out/'page_index.json',manifest)
                if progress:
                    done = sum('extraction_status' in p for p in manifest['pages'] if p['decision'] in ('target','submission'))
                    total = sum(p['decision'] in ('target','submission') for p in manifest['pages'])
                    progress('extract',done,total,f'报审与检验表提取 {done}/{total}')
                continue
            entry['template_attempts'] = []
            geometry = None
            for template in templates:
                template_config = read_json(template)
                trial = original / template_config['template_id']
                trial.mkdir()
                for name in ['page.png', 'page_meta.json']:
                    os.link(original / name, trial / name)
                try:
                    preview(SimpleNamespace(run=str(trial),template=str(template),rotation=entry.get('rotation_ccw',0)))
                    geometry = read_json(trial/'geometry.json')
                    entry['template_attempts'].append(dict(template_id=template_config['template_id'],
                        errors=geometry['geometry_errors'], run_dir=str(trial.relative_to(out))))
                    if not geometry['geometry_errors']:
                        folder = trial
                        entry['run_dir'] = str(folder.relative_to(out))
                        break
                except ValueError as exc:
                    entry['template_attempts'].append(dict(template_id=template_config['template_id'],
                        errors=[str(exc)], run_dir=str(trial.relative_to(out))))
            else:
                raise ValueError('所有支持的检验表模板几何检查均未通过，需人工核验')
            if geometry["geometry_errors"]:
                entry["extraction_status"] = "geometry_failed"
                entry["geometry_errors"] = geometry["geometry_errors"]
            elif not args.extract_draft:
                entry["extraction_status"] = "preview_ready"
            else:
                extract(SimpleNamespace(run=str(folder),device=args.device,result_dir="ocr_draft",allow_draft=True),engine=engine)
                result_path = folder/"ocr_draft"/"result.json"
                result = read_json(result_path)
                entry["result_path"] = str(result_path.relative_to(out))
                entry["extraction_status"] = "draft_ready_pending_review"
                entry["summary"] = result["summary"]
                for r in result["records"]:
                    r = dict(r)
                    r.update({"page_no":page_no,"pdf_sha256":pdf_sha,
                        "source_result":entry["result_path"],"geometry_review_status":result["geometry_review_status"]})
                    records.append(r)
        except ValueError as exc:
            entry.update({"extraction_status":"geometry_or_validation_failed","extraction_error":str(exc)})
        except Exception as exc:
            entry.update({"extraction_status":"error","extraction_error":str(exc)})
        write_json(out/"page_index.json",manifest)
        if progress:
            done = sum('extraction_status' in p for p in manifest['pages'] if p['decision'] in ('target','submission'))
            total = sum(p['decision'] in ('target','submission') for p in manifest['pages'])
            progress('extract', done, total, f'报审与检验表提取 {done}/{total}')
    manifest["target_pages"] = [p["page_no"] for p in manifest["pages"] if p["decision"]=="target"]
    manifest['submission_pages'] = [p['page_no'] for p in manifest['pages'] if p['decision']=='submission']
    manifest["review_pages"] = [p["page_no"] for p in manifest["pages"] if p["decision"] in ("review","error")]
    manifest['extraction_review_pages'] = [p['page_no'] for p in manifest['pages']
        if p.get('extraction_error') or p.get('extraction_status') in ('adaptive_needs_review','glm_needs_review','geometry_failed')
        or p.get('calibration_requires_review')]
    manifest['status'] = ('completed_with_review' if manifest['review_pages'] or manifest['extraction_review_pages']
                          else 'completed_pending_human_review')
    if not manifest['target_pages']:
        manifest['status'] = 'no_entry_pages_found_review_required'
    write_json(out/"page_index.json",manifest)
    submission_duplicates = submission_duplicate_candidates(submissions)
    merged = {"schema_version":"v7_glm_v1" if backend=='glm' else "v7_adaptive_v1" if backend=='adaptive' else "v7","pdf_sha256":pdf_sha,"records":records,
              "entry_backend":backend,"blank_candidates":blank_candidates,"status":manifest['status'],
              "target_pages":manifest['target_pages'],"extraction_review_pages":manifest['extraction_review_pages'],
              "extraction_failures":[p for p in manifest['pages'] if p.get('extraction_error')],
              'submission_forms':submissions, 'submission_duplicate_candidates':submission_duplicates,
              "duplicate_candidates":duplicate_candidates(records),
              "review_pages":manifest["review_pages"],"ready_for_search":False,
              "note":"草稿未人工复核；重复候选不合计、不自动删除；查看page_index中的全部失败项"}
    write_json(out/"records_all_pages.json",merged)
    write_json(out/'submission_forms.json',{'schema_version':'submission_v1','pdf_sha256':pdf_sha,
        'forms':submissions,'submission_pages':manifest['submission_pages'],'duplicate_candidates':submission_duplicates,
        'failures':[p for p in manifest['pages'] if p['decision']=='submission' and p.get('extraction_error')],
        'review_pages':manifest['review_pages'],'ready_for_search':False})
    rows=[]
    for entry in manifest["pages"]:
        thumbnails=''.join(f'<a href="{a["header_image"]}"><img src="{a["header_image"]}" width="360"></a>' for a in entry["attempts"])
        links=''
        if "run_dir" in entry:
            preview_name = ('submission_page_preview.png' if entry['decision']=='submission' else
                            'glm/input.png' if backend=='glm' else 'adaptive/input.png' if backend=='adaptive' else 'grid_preview.png')
            links+=f'<a href="{entry["run_dir"]}/{preview_name}">几何预览</a>'
        if "result_path" in entry:
            if backend=='glm' and entry['decision']=='target':
                links+=f' | <a href="{entry["run_dir"]}/glm/review.html">GLM 字段核对</a>'
            elif backend=='adaptive' and entry['decision']=='target':
                links+=f' | <a href="{entry["result_path"]}">产品JSON</a>'
            else:
                review_dir = 'submission_ocr' if entry['decision']=='submission' else 'ocr_draft'
                links+=f' | <a href="{entry["run_dir"]}/{review_dir}/review.html">字段核对</a>'
        reasons=entry.get("reasons",[]) + [entry.get("extraction_status", ""),entry.get("extraction_error", ""),entry.get("error", "")]
        rows.append(f'<tr><td>{entry["page_no"]}</td><td>{html.escape(entry["decision"])}</td><td>{thumbnails}</td><td>{html.escape(str(reasons))}<br>{links}</td></tr>')
    (out/"index.html").write_text('<!doctype html><meta charset="utf-8"><title>整份PDF进场表筛选</title>'
        '<style>body{font-family:sans-serif;padding:24px}td,th{border:1px solid #ccc;padding:12px;vertical-align:top}table{border-collapse:collapse}</style>'
        '<h1>页面筛选与提取草稿</h1><p>target=进场检验记录或进场数量清单；submission=报审表；other=其他明确标题；review/error必须复核。命中不保证模板适用，草稿不自动放行。</p>'
        f'<h2>报审表重复候选：{len(submission_duplicates)}组</h2><pre>{html.escape(str(submission_duplicates))}</pre>'
        '<table><tr><th>页码</th><th>判定</th><th>OCR页头证据</th><th>状态与链接</th></tr>'+''.join(rows)+'</table>',encoding="utf-8")
    print(f"target_pages={manifest['target_pages']}; submission_pages={manifest['submission_pages']}; review_pages={manifest['review_pages']}")
    print(f"查看 {out/'index.html'} 与 {out/'page_index.json'}")
    return merged


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf",required=True)
    parser.add_argument("--out",required=True)
    parser.add_argument("--template",default=str(rebar_config('inspection_11_rows.json')))
    parser.add_argument('--submission-template',default=str(rebar_config('submission_b06.json')))
    parser.add_argument("--device",default="gpu:0")
    parser.add_argument("--scan-dpi",type=int,default=180,choices=[150,180,200])
    parser.add_argument("--extract-draft",action="store_true",help="几何检查通过后自动OCR草稿，仍标记未人工确认")
    parser.add_argument('--entry-backend',choices=['glm','template','adaptive'],default='glm')
    parser.add_argument('--no-deskew',action='store_true',help='自适应模式跳过小角度纠偏，并标记复核')
    run(parser.parse_args())
