"""报审表重复候选；可离线更新已有汇总，无需重新跑OCR。

python -m material_ocr.upstream_legacy.materials.rebar.submission_duplicates --records runs/pdf_draft_v7/records_all_pages.json \
  --out runs/pdf_draft_v7/records_with_submission_duplicates.json
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path

MATCH_FIELDS = ('supply_date', 'use_location', 'material_name')


def submission_duplicate_candidates(forms):
    groups = defaultdict(list)
    for form in forms:
        values = form.get('values', {})
        keys = [values.get(k) for k in MATCH_FIELDS]
        sha = form.get('pdf_sha256')
        if not sha or not form.get('record_id') or any(not isinstance(v,str) or not v.strip() for v in keys):
            continue
        # 三个字段都通过其解析/置信度规则才参与；页级编号问题不阻止候选。
        fields = form.get('fields', {})
        if any(k not in fields or fields[k].get('issues') or
               fields[k].get('value') != values[k] or
               fields[k].get('min_score') is None or fields[k]['min_score'] < .95 for k in MATCH_FIELDS):
            continue
        # 正文锚点等页级问题仍应阻止；只容许编号相关问题。
        if any(not issue.startswith('document_no:') for issue in form.get('issues', [])):
            continue
        groups[(sha, form.get('template_id'), *keys)].append(form)
    result = []
    for (sha, template, *keys), group in groups.items():
        if len(group) < 2:
            continue
        numbers = []
        for form in group:
            f = form.get('fields', {}).get('document_no', {})
            score = f.get('min_score')
            number = form.get('values', {}).get('document_no')
            numbers.append({'record_id':form['record_id'], 'page_no':form.get('page_no'),
                'value':number, 'min_score':score, 'issues':f.get('issues', []),
                'source_result':form.get('source_result'),
                'high_confidence_candidate':bool(number and score is not None and score >= .95
                    and not any(i != 'document_no_visual_review_required' for i in f.get('issues', [])))})
        nonempty = {n['value'] for n in numbers if n['value']}
        high_confidence = {n['value'] for n in numbers if n['high_confidence_candidate']}
        if len(high_confidence) > 1:
            number_status = 'conflicting_high_confidence_numbers'
        elif len(nonempty) > 1:
            number_status = 'different_ocr_numbers'
        elif all(n['value'] for n in numbers):
            number_status = 'same_ocr_number'
        else:
            number_status = 'missing_ocr_number'
        result.append({'pdf_sha256':sha, 'template_id':template,
            'record_ids':[f['record_id'] for f in group], 'page_nos':[f.get('page_no') for f in group],
            'match_fields':list(MATCH_FIELDS), 'matched_values':dict(zip(MATCH_FIELDS,keys)),
            'document_no_used_for_matching':False, 'document_numbers':numbers,
            'document_number_status':number_status, 'status':'duplicate_candidate_pending_review',
            'action':'review_do_not_merge_delete_or_copy_number_automatically',
            'note':'日期、使用部位和材料名称一致可能是重复报审，也可能是同日多批；编号差异保留供核对。'})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records',required=True)
    parser.add_argument('--out',required=True)
    args = parser.parse_args()
    data = json.loads(Path(args.records).read_text(encoding='utf-8'))
    data['submission_duplicate_candidates'] = submission_duplicate_candidates(data.get('submission_forms', []))
    output = Path(args.out)
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as stream:
        json.dump(data,stream,ensure_ascii=False,indent=2)
    print(f'报审表重复候选: {len(data["submission_duplicate_candidates"])}; 输出: {output}')


if __name__ == '__main__':
    main()
