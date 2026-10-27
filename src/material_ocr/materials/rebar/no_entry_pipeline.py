"""Web adapter for the independently tested nameplate-index pipeline.

The entry-table matcher is never called here. Classification is consumed from
its existing manifest, and the original upstream files remain immutable.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .upstream_adapter import file_hash
from .upstream_pipeline import write_json
from .paddle_attachments import run_attachments
from .glm_web_contract import adapt_glm_ledger

BRANCH = 'no-entry-nameplate-v1'


def submission_checkpoints(data, manifest, folder, output):
    forms = {f['page_no']: f for f in data.get('submission_forms', [])}
    result = []
    for page in manifest['pages']:
        if page['decision'] != 'submission':
            continue
        number = page['page_no']
        form = deepcopy(forms.get(number))
        if not form or page.get('extraction_error'):
            form = dict(page_no=number, fields={}, requires_whole_page=True,
                        template_error=page.get('extraction_error'),
                        issues=['whole_page_submission_fallback'])
            source = output / 'no_entry_submission' / f'page_{number:03d}.json'
            write_json(source, form)
        else:
            source = folder / page['result_path']
        result.append(dict(page_no=number, pdf_sha256=data['pdf_sha256'],
            upstream_role='submission', status='completed', dpi=300,
            pixel_size=form.get('page_pixel_size'), rotation_ccw=page.get('rotation_ccw', 0),
            submission_form=form, regions=[],
            raw_text='\n'.join(str(f.get('raw_text', '')) for f in form['fields'].values()),
            checkpoint_reference=str(source), checkpoint_sha256=file_hash(source)))
    return result


def web_ledger(raw, resources, pdf, digest, manifest):
    ledger = deepcopy(raw)
    ledger['branch_summary'] = deepcopy(raw['summary'])
    ledger['processing_branch'] = BRANCH
    ledger['page_count'] = len(manifest['pages'])
    objects = {obj['candidate_id']: obj for key in ('nameplates', 'deliveries', 'certificates')
               for obj in resources.get(key, [])}

    def source(identity):
        obj = objects.get(identity, {})
        return dict(candidate_id=identity, page_no=obj.get('page_no'), evidence=obj.get('evidence', {}))

    for row in ledger['records']:
        original = row['sources']
        row['index_sources'] = deepcopy(original)
        row['sources'] = {'nameplate': source(original['nameplate'])}
        for role, plural in [('delivery', 'deliveries'), ('certificate', 'certificates')]:
            row['sources'][plural] = [source(i) for i in original[plural]]
            if len(original[plural]) == 1:
                row['sources'][role] = source(original[plural][0])
        row['page_no'] = row['nameplate_page']
        row['match_route'] = BRANCH
        row['match_status'] = 'needs_review' if row['review_required'] else 'matched'
        # Expose the measured word boxes to the existing page overlay.
        for field in row['fields'].values():
            located = []
            for evidence in field.get('evidence', []):
                located.append(evidence)
                if not evidence.get('polygon_page'):
                    for box in evidence.get('boxes', []):
                        if box.get('polygon_page'):
                            located.append(dict(evidence, boxes=[], polygon_page=box['polygon_page'],
                                raw_text=box['text'], ocr_score=box.get('score'), precision='word'))
            field['evidence'] = located
    ledger['diagnostics'] = [dict(code='no_entry_unmatched_attachment', reason=item['reason'],
        page_no=item.get('page_no'), locations=[dict(page_no=item['page_no'], precision='page')])
        for item in raw['unmatched_attachments'] if item.get('page_no')]
    ledger['diagnostics'].extend(dict(code='header_classification_review', page_no=n,
        reason='页头分类待复核；本卷按铭牌建立索引，请确认未遗漏进场表',
        locations=[dict(page_no=n, precision='page')]) for n in manifest.get('review_pages', []))
    return adapt_glm_ledger(ledger, pdf.name, digest, ocr_model='local-paddle')


def write_page_index(output, pdf, digest, checkpoints, manifest):
    from no_entry_ocr.parsers import blocks
    types = {p['page_no']: p['decision'] for p in manifest['pages']}
    pages = []
    for cp in sorted(checkpoints, key=lambda c: c['page_no']):
        words = blocks(cp) if cp.get('ocr_passes') else []
        pages.append(dict(page_no=cp['page_no'], pdf_sha256=digest,
            document_type=types[cp['page_no']], rotation_ccw=cp.get('rotation_ccw', 0),
            pixel_size=cp.get('original_pixel_size') or cp.get('pixel_size'), dpi=cp.get('dpi', 300),
            ocr_status='completed', raw_text=cp.get('raw_text', ''),
            regions=[dict(region_id=f'p{cp["page_no"]:03d}_full', blocks=words)]))
    write_json(output / 'work/attachments/attachment_index.json',
               dict(schema_version='paddle-nameplate-index-v1', pdf_name=pdf.name,
                    pdf_sha256=digest, pages=pages, processing_branch=BRANCH))


def run(pdf, output, *, data, manifest, folder, config, cache_dir, paddle_model_dir,
        device, force_attachments=False, attachment_rotations=(0,), progress=None):
    from no_entry_ocr.extract import guard
    from no_entry_ocr.parsers import adapt
    from no_entry_ocr.pipeline import build

    guard(manifest)
    digest = file_hash(pdf)
    if data['pdf_sha256'] != digest:
        raise ValueError('无进场表支路的分类 PDF 摘要不一致')
    if progress:
        progress('no_entry', 0, 1, '未检测到进场表，切换到铭牌索引支路')
    submissions = submission_checkpoints(data, manifest, folder, output)
    supplied = {cp['page_no'] for cp in submissions if not cp['submission_form'].get('requires_whole_page')}
    pages = {p['page_no'] for p in manifest['pages']} - supplied
    resources, checkpoints, paths, info = run_attachments(pdf, output, pages, manifest,
        cache_dir=cache_dir, model_dir=paddle_model_dir, device=device,
        force=force_attachments, rotations=attachment_rotations, progress=progress)
    # A missed entry-table classification must not be silently treated as a card.
    if resources.get('records'):
        raise ValueError('附件 OCR 发现疑似进场表，请核对页面分类；未生成铭牌台账')
    if progress:
        progress('nameplate_index', 0, 1, '汇总全部铭牌，按炉批号、牌号和规格匹配附件')
    enriched = adapt(resources, checkpoints)
    raw = build(enriched, checkpoints, submissions, manifest)
    write_json(output / 'no_entry_ledger.json', raw)
    write_json(output / 'no_entry_resources.json', enriched)
    write_json(output / 'no_entry_match_trace.json', raw['match_trace'])
    write_json(output / 'upstream_checkpoints.json', submissions)
    if not raw['records']:
        raise ValueError('整卷未识别到可建立索引的铭牌，请核对铭牌页面；未生成空台账')
    ledger = web_ledger(raw, enriched, pdf, digest, manifest)
    merged = checkpoints + [cp for cp in submissions if cp['page_no'] in supplied]
    write_page_index(output, pdf, digest, merged, manifest)
    write_json(output / 'automatic_ledger.json', ledger)
    write_json(output / 'review/automatic_ledger.json', ledger)
    backends = dict(entry=None, submission='local-paddle', attachments='local-paddle',
                    index='nameplate', glm_calls=0)
    routing = dict(processing_branch=BRANCH, target_pages=[],
                   submission_pages=manifest.get('submission_pages', []),
                   review_pages=manifest.get('review_pages', []),
                   classification_reference=str(folder / 'page_index.json'))
    write_json(output / 'routing.json', routing)
    write_json(output / 'upstream_summary.json', dict(routing, records=len(raw['records']),
        full_pipeline_ready=True, blocking_reason=None, upstream=str(folder)))
    config = dict(config, processing_branch=BRANCH, ocr_model='local-paddle',
                  upstream_reference=str(folder), attachment_ocr=info, glm_calls=0)
    write_json(output / 'experiment/metadata.json', dict(status='completed_pending_review',
        config_reference=config, ocr_backends=backends, routing=routing,
        end_time_utc=datetime.now(timezone.utc).isoformat()))
    write_json(output / 'checkpoint_sources.json', dict(pdf_sha256=digest,
        references=[dict(path=str(p), sha256=file_hash(p)) for p in paths]))
    result = dict(automatic_ledger=ledger, ocr_backends=backends, routing=routing,
                  metrics=raw['summary'])
    write_json(output / 'summary.json', dict(routing, metrics=raw['summary'], ocr_backends=backends))
    write_json(output / 'statistics.json', raw['summary'])
    if progress:
        progress('completed', 1, 1, '铭牌索引与附件匹配完成，可核验关键字段')
    return result
