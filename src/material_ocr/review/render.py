"""Replay reviewed OCR into a separately versioned, same-page match review."""
import argparse, json, csv, hashlib, re
from pathlib import Path
from PIL import Image
from material_ocr.review.images import prepare_review_images
from material_ocr.paths import review_template


def build(out, *, inputs, pdf, base, matcher, matching_options=None, result_enricher=None):
    """Render saved inputs without fixture-specific paths."""
    out, pdf, base = Path(out), Path(pdf), Path(base)
    data = {k: json.loads(Path(p).read_text()) for k, p in inputs.items()}
    sha = data['baseline']['pdf_sha256']
    for key in ['index', 'classification', 'records']:
        if data[key]['pdf_sha256'] != sha:
            raise ValueError(f'{key} PDF mismatch')
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != sha:
        raise ValueError('PDF hash does not match saved evidence')
    if [e['record'] for e in data['baseline']['entries']] != data['records']['records']:
        raise ValueError('Original inspection records changed')
    ids = data['baseline']['metrics']['certificate_identifiers']
    result = matcher(data['baseline'], data['catalog']['keyword_catalog']['items'],
                     data['classification'], ids, **(matching_options or {}))
    result.update(pdf_name=pdf.name, status='pending_user_review',
                  source_files={k: {'path': str(p), 'sha256': hashlib.sha256(Path(p).read_bytes()).hexdigest()}
                                for k, p in inputs.items()},
                  unmatched_certificate_identifiers=[c for c in result['certificate_inventory'] if not c['exact_record_ids']])
    for entry in result['entries']:
        entry['pages'] = dict(inspection_record=[entry['record']['page_no']],
                              quality_certificate=sorted({c['page_no'] for c in entry['quality_certificate_candidates']}),
                              delivery_note=sorted({c['page_no'] for c in entry['delivery_note_candidates']}),
                              nameplate=sorted({p['nameplate_page'] for p in entry['nameplate_candidates']}))
    result['metrics']['distinct_content_groups'] = len({json.dumps({k: e['record'].get(k) for k in
        ['certificate_no', 'weight_t', 'diameter_mm', 'manufacturer', 'material_name']}, sort_keys=True)
        for e in result['entries']})
    if result_enricher:
        result_enricher(result)
    write_review(out, result, data, base)
    return result

def write_review(out, result, data, base):
    out.mkdir(parents=True, exist_ok=False)
    (out/'match_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    (out/'metrics.json').write_text(json.dumps(result['metrics'],ensure_ascii=False,indent=2))
    (out/'certificate_inventory.json').write_text(json.dumps(result['certificate_inventory'],ensure_ascii=False,indent=2))
    for name in ('certificate_page_inventory', 'nameplate_inventory', 'delivery_inventory', 'four_form_bundles'):
        (out/(name+'.json')).write_text(json.dumps(result.get(name, []),ensure_ascii=False,indent=2))
    (out/'review_queue.json').write_text(json.dumps(result['review_queue'],ensure_ascii=False,indent=2))
    with (out/'match_results.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['记录','检验页','原证书号','进场数量_t','原数量候选页','保留送货页','排除送货页','铭牌炉批号','证书候选页','补查证书号','状态','冲突','未解决'])
        for e in result['entries']:
            r=e['record'];writer.writerow([e['record_id'],r['page_no'],r['certificate_no'],r['weight_t'],
                ','.join(str(d['page_no']) for d in e['original_candidates']['delivery_note_candidates']),
                ','.join(map(str,e['pages']['delivery_note'])),','.join(str(d['page_no']) for d in e['rejected_delivery_note_candidates']),
                ','.join(p['heat_lot_no'] or '炉批号未核验' for p in e['nameplate_candidates']),','.join(map(str,e['pages']['quality_certificate'])),
                ','.join(c['value'] for c in e['recovered_certificate_candidates']), e['status'],
                json.dumps(e['conflicts'],ensure_ascii=False), ','.join(e['unresolved_reasons'])])
    lines=[f"# {result['pdf_name']}：同页铭牌约束匹配结果",'',
           f"原始{len(result['entries'])}条检验记录（{result['metrics']['distinct_content_groups']}组内容）全部保留。复用{len(data['index']['pages'])}页OCR。唯一且无待核验项的四表自动捆绑，其余交由人工补全确认。", '',
           '数量重复时按同页铭牌炉批号与已命中证书炉批号交集筛选；缺证书时按送货单同页铭牌→炉批号→证书补查。多铭牌保留独立炉批号和坐标证据，补查编号矛盾及同行规格冲突均明示。', '',
           '|记录|原证书号|数量(t)|送货页|铭牌炉批号|证书页|结果|','|---|---|---:|---|---|---|---|']
    for e in result['entries']:
        r=e['record'];lines.append('|'+ '|'.join(map(str,[e['record_id'],r['certificate_no'],r['weight_t'],
          ','.join(map(str,e['pages']['delivery_note'])) or '—',','.join(p['heat_lot_no'] or '炉批号未核验' for p in e['nameplate_candidates']) or '—',
          ','.join(map(str,e['pages']['quality_certificate'])) or '—',e['status']]))+'|')
    lines += ['', '## 实际统计', '', '```json', json.dumps(result['metrics'],ensure_ascii=False,indent=2), '```', '',
              'four_form_bundles.json保存初始自动捆绑，review_queue.json仅含待核验记录。网页人工确认后扣除已捆绑附件，下载JSON/CSV得到更新后的捆绑清单与剩余候选；原OCR保持不变。',
              '', '原文、坐标、候选来源、被排除的数量候选和补查冲突见match_results.json；图片在index.html内可直接放大。观察仅来自本文件，不代表总体准确率。']
    (out/'MATCH_RESULTS.md').write_text('\n'.join(lines)+'\n')
    import shutil
    for name in ('review_state.js', 'review_ui.js', 'filling_ui.js'):
        shutil.copy2(Path(__file__).with_name(name), out/name)
    preview=out/'page_previews';preview.mkdir()
    for pg in data['classification']['pages']:
        image_path=base/'pages'/f"page_{pg['page_no']:03d}_upright.png"
        if not image_path.is_file():
            continue
        im=Image.open(image_path).convert('RGB')
        im.thumbnail((800,800));im.save(preview/f"page_{pg['page_no']:03d}.jpg",quality=79,optimize=True)
    payload=json.dumps(result,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    template=review_template().read_text()
    import html
    template=template.replace('__PDF_NAME_HTML__', html.escape(result['pdf_name']))
    template=template.replace('__PDF_STEM_HTML__', html.escape(Path(result['pdf_name']).stem))
    filename=json.dumps(Path(result['pdf_name']).stem + '_匹配审核.json', ensure_ascii=False).replace('<', '\\u003c')
    template=template.replace('__EXPORT_FILENAME_JSON__', filename)
    template=template.replace('全部24条记录',f"全部{len(result['entries'])}条记录")
    template=template.replace('24条原记录保留',"${m.record_count}条原记录保留")
    (out/'index.html').write_text(template.replace('__DATA__',payload).replace('__RUN__',out.name))
    image_status=prepare_review_images(out/'index.html',base/'pages')
    print(json.dumps({'output':str(out),'metrics':result['metrics'],'images':image_status},ensure_ascii=False,indent=2))
