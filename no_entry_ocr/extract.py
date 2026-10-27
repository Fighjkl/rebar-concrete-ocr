"""Reuse existing OCR as a read-only dependency, in isolated processes."""
import json
from pathlib import Path
from types import SimpleNamespace


def save(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')


def guard(manifest):
    pages=manifest['pages']
    if not pages or [p['page_no'] for p in pages]!=list(range(1,len(pages)+1)):
        raise ValueError('页面分类未完整覆盖整份 PDF')
    if any(p['decision']=='target' for p in pages):
        raise ValueError('检测到进场表：独立无进场表支路停止，请使用原流程')
    if any(p['decision']=='error' for p in pages):
        raise ValueError('页面分类出错，不能判定整份 PDF 无进场表')
    if any(p['decision'] not in ('submission','other','review') for p in pages):
        raise ValueError('页面分类状态未知，不能进入无进场表支路')


def upstream(pdf, folder, device):
    from material_ocr.upstream_legacy.extraction.batch import run
    from material_ocr.upstream_legacy.extraction.table import create_ocr
    from material_ocr.upstream_legacy.materials.rebar.submission import extract_submission
    from material_ocr.upstream_legacy.paths import rebar_config
    # Preview-only classification never constructs a GLM inference engine.
    data=run(SimpleNamespace(pdf=str(pdf),out=str(folder),device=device.replace('cuda:','gpu:'),
        scan_dpi=180,entry_backend='glm',extract_draft=False,
        template=str(rebar_config('inspection_11_rows.json')),
        submission_template=str(rebar_config('submission_b06.json'))))
    manifest=json.loads((folder/'page_index.json').read_text());guard(manifest)
    engine=create_ocr(device.replace('cuda:','gpu:')) if manifest['submission_pages'] else None
    forms=[]
    for page in manifest['pages']:
        if page['decision']!='submission':continue
        directory=folder/page['run_dir']
        try:
            if page.get('extraction_error'):raise ValueError(page['extraction_error'])
            form=extract_submission(directory,device.replace('cuda:','gpu:'),engine)
        except Exception as exc:
            # This branch supports other submission layouts through a saved
            # whole-page pass. The production submission template is untouched.
            form=dict(page_no=page['page_no'],fields={},issues=['whole_page_submission_fallback'],
                      template_error=str(exc),requires_whole_page=True)
            save(directory/'submission_ocr/result.json',form)
        page.update(result_path=str((directory/'submission_ocr/result.json').relative_to(folder)),
                    extraction_status='draft_ready_pending_review')
        forms.append(dict(form,source_result=page['result_path']))
    data['submission_forms']=forms
    save(folder/'records_all_pages.json',data);save(folder/'page_index.json',manifest)


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--pdf',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--device',default='cuda:0')
    a=p.parse_args();upstream(a.pdf,a.out,a.device)
