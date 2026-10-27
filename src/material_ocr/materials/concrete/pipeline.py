"""Concrete routing and business rules around the shared OCR execution primitives."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path

from material_ocr.materials.rebar.upstream_adapter import file_hash, read_json
from material_ocr.materials.rebar.upstream_pipeline import (
    check_reuse, seal_upstream, upstream_code_hash, write_json)
from .extraction import issue_text, parse_inspection_html
from .matching import SUBMISSION_LEDGER_FIELDS, _submission_document_no, classify_pages, match_forms
from .routing import ROUTING_ID
from .submission_number import is_complete_document_no, recover_submission_document_no
from .upstream_worker import run_worker

LEDGER_FIELDS = {
    '文件编号': 'document_no', '材料名称': 'material_name', '规格品种': 'specification',
    '生产单位': 'producer', '供应单位': 'supplier', '供货日期': 'supply_date',
    '使用部位': 'use_location', '炉批号': 'batch_no', '验收数量': 'quantity_m3',
    '质保书编号': 'warranty_no',
}


def utc():
    return datetime.now(timezone.utc).isoformat()


def upstream_identity(digest, model_dir, max_tokens):
    """A steel or older concrete classification cannot be reused as this route."""
    from material_ocr.materials.rebar import glm_production as prod

    code = hashlib.sha256(upstream_code_hash().encode())
    for name in ('routing.py', 'upstream_worker.py'):
        source = Path(__file__).with_name(name)
        code.update(name.encode())
        code.update(source.read_bytes())
    return dict(pdf_sha256=digest,
                model_fingerprint=prod._ocr_identity(Path(model_dir), 'local-glm')['fingerprint'],
                upstream_code_sha256=code.hexdigest(), material_type='concrete',
                header_classifier=ROUTING_ID, dpi=300,
                prompt='Table Recognition:', max_new_tokens=max_tokens)


def reuse_concrete_upstream(folder, identity):
    actual = read_json(Path(folder) / 'reuse_manifest.json')['identity']
    if actual != identity:
        raise ValueError('混凝土上游的材料、分类规则、PDF、模型或参数不同，请重新运行 inspection')
    check_reuse(folder, identity)


def upstream_pages(folder, digest, *, paddle_model_dir=None, device='cpu', submission_ocr_engine=None):
    """Use OCR observations, not the rebar parser's statuses or business rows."""
    folder = Path(folder)
    data = read_json(folder / 'records_all_pages.json')
    manifest = read_json(folder / 'page_index.json')
    if data['pdf_sha256'] != digest:
        raise ValueError('混凝土上游 PDF 摘要不一致')
    submissions = {form['page_no']: form for form in data.get('submission_forms', [])}
    pages, context = [], None
    for page in sorted(manifest['pages'], key=lambda p: p['page_no']):
        number = page['page_no']
        if page['decision'] == 'submission':
            context = submissions.get(number)
            fields = (context or {}).get('fields', {})
            raw_text = page.get('ocr_text', '') + '\n' + '\n'.join(
                str(f.get('raw_text', '')) for f in fields.values())
            known_no = fields.get('document_no', {})
            known_no = known_no.get('value', '') if isinstance(known_no, dict) else known_no
            number_ocr = None
            if (not is_complete_document_no(known_no) and not _submission_document_no(raw_text)
                    and (submission_ocr_engine is not None or paddle_model_dir)):
                number_ocr = recover_submission_document_no(
                    folder, page, model_dir=paddle_model_dir, device=device,
                    engine=submission_ocr_engine)
            submission_page = {'page_no': number, 'form_type': 'submission', 'raw_text': raw_text,
                               'submission_fields': deepcopy(fields),
                               'submission_pixel_size': (context or {}).get('page_pixel_size')}
            # Keep the existing header OCR geometry: a multiline location can
            # be emitted before the date instead of after 拟用于工程 in text order.
            header_path = folder / page.get('header_image', '')
            header_json = header_path.with_suffix('.json') if header_path.suffix else None
            if header_json and header_json.is_file():
                header = read_json(header_json)
                submission_page['submission_header_blocks'] = [
                    {'text': text, 'polygon': polygon, 'score': score}
                    for text, polygon, score in zip(
                        header.get('rec_texts', []), header.get('rec_polys', []),
                        header.get('rec_scores', [None] * len(header.get('rec_texts', []))))]
            if number_ocr:
                submission_page['submission_document_no_ocr'] = number_ocr
            pages.append(submission_page)
        elif page['decision'] == 'target':
            glm = folder / f'pages/page_{number:03d}/glm'
            # Missing or failed inference is an execution error, not an empty table.
            raw = (glm / 'raw.txt').read_text(encoding='utf-8')
            info, meta = read_json(glm / 'inference.json'), read_json(glm / 'input_meta.json')
            if info['input_image_sha256'] != meta['input_image_sha256']:
                raise ValueError(f'第 {number} 页 GLM 原图校验不一致')
            if (glm / 'input.png').is_file() and file_hash(glm / 'input.png') != meta['input_image_sha256']:
                raise ValueError(f'第 {number} 页 GLM 输入图已改变')
            try:
                page_title = page.get('ocr_text', '')
                parsed = parse_inspection_html(raw, number, inference=info, page_title=page_title)
            except ValueError as exc:
                parsed = {'records': [], 'issues': [str(exc)]}
            for row in parsed['records']:
                for evidence in row['field_evidence'].values():
                    evidence.update(raw_model_output=str(glm / 'raw.txt'), raw_output_sha256=file_hash(glm / 'raw.txt'),
                                    source_image=str(glm / 'input.png'), source_role='inspection')
                # Preserve the inspection observation until the matcher can
                # compare it with the corresponding submission, row by row.
                for key in ('document_no',):
                    field = (context or {}).get('fields', {}).get(key, {})
                    if field and not row.get(key):
                        row[key] = field.get('value') or ''
                        row['field_evidence'][key] = dict(field, page_no=context['page_no'], source_role='submission',
                                                         source_pixel_size=context.get('page_pixel_size'), source_field=key)
            pages.append({'page_no': number, 'form_type': 'inspection_record',
                          'raw_text': page.get('ocr_text', '') + '\n' + raw,
                          'inspection_records': parsed['records'], 'issues': parsed['issues'],
                          'source': str(glm / 'raw.txt')})
    return pages, manifest


def build_ledger(pages, *, pdf_name=''):
    result = match_forms(classify_pages(pages))
    records = []
    for record in result['records']:
        fields = {}
        values, evidence = record['fields'], record['field_evidence']
        diagnostics = []
        for column, key in LEDGER_FIELDS.items():
            source = evidence.get('quantity' if key == 'quantity_m3' else key, {})
            value = values.get(key, '')
            cell_issues = []
            if key in SUBMISSION_LEDGER_FIELDS and source.get('source_role') != 'submission':
                value, source = '', {}
            if not value:
                if key in SUBMISSION_LEDGER_FIELDS:
                    cell_issues.append({'code': 'concrete_submission_field_missing', 'field': column,
                                        'reason': '所属报审表的' + column + '未提取，请核对报审表并补全',
                                        'locations': [source] if source else []})
                else:
                    cell_issues.append({'code': 'concrete_field_missing', 'field': column,
                                        'reason': column + '未提取，请核对原页；原文无此项时保留空值',
                                        'locations': [source] if source else [{'page_no': record['inspection_page']}]})
            affected = {
                'material_name': ('material_name_not_concrete',),
                'specification': ('concrete_specification_missing_or_invalid',),
                'quantity_m3': ('concrete_volume_missing_or_invalid', 'invalid_volume_or_mass_unit',
                                'missing_volume_unit', 'volume_must_be_positive'),
                'use_location': ('use_location_from_remarks_check_source',),
            }
            for code in record['issues']:
                if code in affected.get(key, ()) or code == key + '_cell_missing_or_ambiguous':
                    cell_issues.append({'code': code, 'field': column, 'reason': issue_text(code),
                                        'locations': [source] if source else [{'page_no': record['inspection_page']}]})
            for code in record.get('field_review_issues', {}).get(key, []):
                if not any(issue.get('code') == code and issue.get('field') == column for issue in cell_issues):
                    cell_issues.append({'code': code, 'field': column, 'reason': issue_text(code),
                                        'locations': [source] if source else [{'page_no': record['inspection_page']}]})
            for conflict in record.get('field_conflicts', []):
                if conflict['field'] == key:
                    cell_issues.append(dict(conflict, code='concrete_field_conflict', field=column))
            if key == 'warranty_no' or key in record.get('deduplication_conflicts', {}):
                for issue in record['certificate_deduplication_diagnostics']:
                    issue = dict(deepcopy(issue), field=column)
                    if key != 'warranty_no':
                        source_key = 'quantity' if key == 'quantity_m3' else key
                        issue['locations'] = [dict(source['field_evidence'].get(source_key) or
                                                   {'page_no': source['page_no'], 'raw_text': source['values'].get(key, '')},
                                                   field=column, record_id=source['record_id'])
                                              for source in record['duplicate_source_records']]
                    cell_issues.append(issue)
            default_page = record.get('submission_page') if key in SUBMISSION_LEDGER_FIELDS else record['inspection_page']
            fields[column] = {'value': value, 'page': source.get('page_no', default_page),
                              'evidence': [source] if source else [], 'found_in_ocr': bool(value and source and not cell_issues),
                              'status': 'needs_review' if cell_issues else 'observed', 'diagnostics': cell_issues,
                              'review_required': True}
            diagnostics.extend(cell_issues)
        fields['验收数量']['unit'] = 'm3'
        diagnostics.extend({'code': 'concrete_review', 'reason': reason,
                            'locations': [{'page_no': record['inspection_page']}]} for reason in
                           [*[issue_text(code) for code in record['issues']], record['mix_review_reason'], record['delivery_review_reason']] if reason)
        candidates = []
        for group, candidate_type, default_reason in (
                ('mix_candidates', 'mix_design', '配比报告候选'),
                ('delivery_candidates', 'delivery_note', '送货单候选'),
                ('quality_candidates', 'quality_certificate', '质量证明书候选')):
            for candidate in record[group]:
                reason = (candidate.get('match_reason') or record.get('mix_review_reason') or default_reason
                          if candidate_type == 'mix_design' else default_reason)
                source_text = candidate.get('use_location') or candidate.get('warranty_no') \
                    or candidate.get('ticket_no') or ''
                candidates.append(dict(candidate, candidate_type=candidate_type, reason=reason,
                                       locations=[{'page_no': candidate['page_no'], 'raw_text': source_text}]))
        records.append(dict(record, fields=fields, diagnostics=diagnostics, match_candidates=candidates,
                            match_status='needs_review' if diagnostics else record['status']))
    return {'schema_version': 'concrete-ledger-v1', 'material_type': 'concrete', 'pdf_name': pdf_name,
            'columns': list(LEDGER_FIELDS), 'registration_version': 2,
            'records': records, 'material_matching': result,
            'certificate_number_deduplication': result['certificate_number_deduplication'],
            'matching_requires_human': result['matching_requires_human'],
            'review_stage': 'before_excel_export', 'quantity_unit': 'm3',
            'summary': f"混凝土表单分类 {len(result['forms'])} 页，进场检验记录 {len(result['records'])} 条；"
                       f"按质保书编号去重 {result['certificate_number_deduplication']['merged_record_count']} 条；数量单位 m³，结果待人工核验"}


def run_pipeline(pdf, output, *, model_dir, glm_python, mode='inspection', device='cuda:0',
                 cache_dir=None, reuse_upstream=None, max_tokens=16384,
                 force_attachments=False, progress=None, paddle_model_dir=None, attachment_rotations=(0,)):
    from material_ocr.materials.rebar import glm_production as prod
    from material_ocr.materials.rebar.paddle_attachments import run_attachments

    if mode not in ('inspection', 'full'):
        raise ValueError(mode)
    pdf, output = Path(pdf).resolve(), Path(output).resolve()
    if not pdf.is_file() or not Path(glm_python).is_file():
        raise FileNotFoundError('PDF 或 GLM Python 不存在')
    if output.exists():
        raise FileExistsError(f'请使用一个全新的结果目录：{output}')
    output.mkdir(parents=True)
    digest = file_hash(pdf)
    metadata = {'status': 'running', 'start_time_utc': utc(), 'material_type': 'concrete',
                'source_pdf_sha256': digest, 'new_ocr_executed': not bool(reuse_upstream),
                'config_reference': {'mode': mode, 'device': device, 'model_dir': str(model_dir),
                    'glm_python': str(glm_python), 'max_tokens': max_tokens, 'prompt': 'Table Recognition:',
                    'reuse_upstream': str(reuse_upstream) if reuse_upstream else None}}
    meta_path = output / 'experiment/metadata.json'
    write_json(meta_path, metadata)
    try:
        identity = upstream_identity(digest, model_dir, max_tokens)
        metadata['upstream_identity'] = identity
        write_json(meta_path, metadata)
        if reuse_upstream:
            # Strict existing seal verification; never alter an old experiment.
            folder = Path(reuse_upstream).resolve()
            reuse_concrete_upstream(folder, identity)
        else:
            folder = output / 'upstream'
            if progress:
                progress('upstream', 0, 1, '混凝土独立页分类、Paddle 报审表和 GLM 整页检验表 OCR')
            run_worker(pdf, output, model_dir=model_dir, glm_python=glm_python,
                       device=device, progress=progress, max_tokens=max_tokens)
            seal_upstream(folder, identity)
        pages, manifest = upstream_pages(
            folder, digest,
            paddle_model_dir=(paddle_model_dir or prod.PROJECT_ROOT / 'models/paddlex_cache/official_models'),
            device=device)
        page_count = prod._pdf_page_count(pdf)
        if sorted(p['page_no'] for p in manifest['pages']) != list(range(1, page_count + 1)):
            raise ValueError('筛页结果未覆盖 PDF 全部页面或存在重复页号')
        inspection_pages = [p for p in pages if p['form_type'] == 'inspection_record']
        summary = {'material_type': 'concrete', 'upstream': str(folder),
                   'target_pages': [p['page_no'] for p in inspection_pages],
                   'records': sum(len(p['inspection_records']) for p in inspection_pages),
                   'review_pages': [p['page_no'] for p in inspection_pages
                                    if p.get('issues') or any(r.get('issues') for r in p['inspection_records'])],
                   'full_pipeline_ready': bool(inspection_pages) and all(p['inspection_records'] and not p['issues'] for p in inspection_pages),
                   'attachment_processing_allowed': True,
                   'note': '混凝土字段缺失或结构不确定保留复核，附件继续识别；不使用钢筋直径/吨位 gate。'}
        write_json(output / 'upstream_summary.json', summary)
        if mode == 'inspection':
            metadata.update(status='completed_pending_review', end_time_utc=utc())
            write_json(meta_path, metadata)
            write_json(output / 'concrete_inspection.json', pages)
            return summary
        if progress:
            progress('entry_review', 1, 1, '按混凝土强度等级、m³ 方量与字段来源校验；缺失项保留复核')
        excluded = {p['page_no'] for p in pages}
        _, checkpoints, _, attachment_info = run_attachments(
            pdf, output, set(range(1, page_count + 1)) - excluded, manifest,
            cache_dir=cache_dir or prod.PROJECT_ROOT / 'runs/paddle-anchors-attachment-cache',
            model_dir=Path(paddle_model_dir or prod.PROJECT_ROOT / 'models/paddlex_cache/official_models'),
            device=device, force=force_attachments, rotations=attachment_rotations, progress=progress)
        write_json(output / 'work/attachments/attachment_index.json', {'pages': checkpoints})
        pages.extend(checkpoints)
        pages.sort(key=lambda p: p['page_no'])
        write_json(output / 'work/concrete/page_index.json', {'pages': pages})
        if progress:
            progress('matching', 0, 1, '合并检验表与附件，按混凝土编号/规格规则逐行匹配')
        ledger = build_ledger(pages, pdf_name=pdf.stem)
        backends = dict(entry='local-glm', submission='local-paddle', attachments='local-paddle')
        ledger['ocr_backends'] = backends
        write_json(output / 'automatic_ledger.json', ledger)
        metadata.update(status='completed_pending_review', end_time_utc=utc(), attachment_ocr=attachment_info,
                        upstream_reference=str(folder), ocr_backends=backends)
        metadata['new_ocr_executed'] |= bool(attachment_info['newly_recognized_pages'])
        write_json(meta_path, metadata)
        if progress:
            progress('completed', 1, 1, '混凝土 OCR 与匹配完成，数量单位 m³，结果待人工核验')
        return {'automatic_ledger': ledger, 'ocr_backends': backends}
    except BaseException as exc:
        metadata.update(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                        error=str(exc), end_time_utc=utc())
        write_json(meta_path, metadata)
        raise
