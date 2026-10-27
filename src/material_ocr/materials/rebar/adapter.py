"""Rebar pipeline assembled from the validated algorithms."""
import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys
import time

from material_ocr.materials.base import ExtractedDocuments, MaterialAdapter, MaterialInfo, RuntimeOptions
from material_ocr.paths import rebar_config
from material_ocr.processes import stop_process


class RebarAdapter(MaterialAdapter):
    def __init__(self):
        self.config = json.loads(rebar_config('material.json').read_text())
        self.info = MaterialInfo('rebar', self.config['name'], True, self.config['description'],
                                 ('auto', '6_rows', '11_rows', 'adaptive'))

    def fingerprint(self):
        digest = hashlib.sha256()
        for name in ['material.json', self.config['attachment_rules'], 'submission_b06.json',
                     'entry_confidence.json', *self.config['inspection_templates']]:
            digest.update(rebar_config(name).read_bytes())
        return digest.hexdigest()

    def extract(self, pdf, output, options: RuntimeOptions, progress):
        from material_ocr.ocr.attachments import build_index_from_pages, load_rules, write_json
        inspection = output / 'inspection'
        model_cache = os.environ.get('PADDLE_PDX_CACHE_HOME', str(output / 'models'))
        base_command = [sys.executable, '-m', 'material_ocr.worker', '--pdf', str(pdf),
                        '--device', options.device, '--model-cache', model_cache]
        self._run_stage(base_command+['inspection','--out',str(inspection),'--layout',options.inspection_layout],
                        output/'inspection_stage.log', progress, stage_file=output/'stage_progress.json')
        page_index = inspection / 'page_index.json'
        manifest = json.loads(page_index.read_text())
        records = inspection / 'records_all_pages.json'
        failures = inspection_failures(manifest)
        extraction_issues = []
        for page in manifest['pages']:
            if page['page_no'] not in failures:
                continue
            error = page.get('extraction_error')
            reason = ('进场表提取失败：' + error) if error else '进场表几何定位或字段提取未通过，需核对原页'
            extraction_issues.append(dict(code='inspection_extraction_failed', page_no=page['page_no'],
                reason=reason, locations=[dict(page_no=page['page_no'], source_role='inspection', polygon_page=None)]))
        progress('attachments', 0, len(manifest['pages']), '识别附件与重点字段')
        rules = load_rules(rebar_config(self.config['attachment_rules']))
        rules['ocr_runtime'].update(profile=options.ocr_profile,
            recognition_batch_size=32 if options.ocr_profile == 'batched_gpu' else None)
        attachment_dir = output / 'attachments'
        attachment_dir.mkdir()
        total = len(manifest['pages'])
        page_numbers = list(range(1, total+1))
        stage_runs = []
        # Short-lived, serial GPU workers keep high-water allocations out of the next shard.
        for offset in range(0,total,5):
            shard = page_numbers[offset:offset+5]
            checkpoint_dir = attachment_dir / 'checkpoints'
            batch_size = 32 if options.ocr_profile == 'batched_gpu' else None
            for attempt in range(2):
                pending = [p for p in shard if not (checkpoint_dir / f'page_{p:03d}.json').exists()]
                if not pending:
                    break
                command = base_command+['attachments','--out',str(attachment_dir),'--page-index',str(page_index),
                    '--profile',options.ocr_profile,'--dpi',str(options.dpi),'--pages',*map(str,pending)]
                if batch_size is not None:
                    command += ['--batch-size', str(batch_size)]
                log = output / f'attachment_{shard[0]:03d}_{shard[-1]:03d}_attempt{attempt+1}.log'
                stage = dict(pages=pending,recognition_batch_size=batch_size,command=command,log=log.name)
                stage_runs.append(stage)
                write_json(output/'stage_runs.json',stage_runs)
                try:
                    self._run_stage(command,log,progress,checkpoints=checkpoint_dir,total=total)
                    stage['status']='completed'
                    break
                except RuntimeError as exc:
                    stage.update(status='failed',error=str(exc))
                    write_json(output/'stage_runs.json',stage_runs)
                    text=log.read_text(errors='replace')
                    if attempt == 0 and batch_size == 32 and ('out of memory' in text.lower() or 'ResourceExhausted' in text):
                        batch_size=6
                        progress('attachments',len(list(checkpoint_dir.glob('*.json'))),total,'显存峰值超限，保留已完成页，以小批量重试')
                    else:
                        raise
            write_json(output/'stage_runs.json',stage_runs)
        pages = [json.loads((attachment_dir/'checkpoints'/f'page_{p:03d}.json').read_text()) for p in page_numbers]
        index=build_index_from_pages(pages,pdf_sha256=manifest['pdf_sha256'],pdf_name=pdf.name,rules=rules)
        index['runtime_execution']=dict(isolated_stages=True,shard_pages=5,stage_runs=stage_runs)
        write_json(attachment_dir/'attachment_index.json',index)
        from .quantity_list import extract_quantity_lists
        saved = json.loads(records.read_text())
        saved['records'].extend(extract_quantity_lists(index,
            excluded_pages={r['page_no'] for r in saved['records']}))
        saved['extraction_issues'] = extraction_issues + [dict(code='attachment_ocr_failed',
            page_no=p['page_no'], reason='附件OCR执行失败') for p in index['pages'] if p.get('ocr_status') == 'failed']
        write_json(records, saved)
        failures = [p['page_no'] for p in index['pages'] if p.get('ocr_status') == 'failed']
        return ExtractedDocuments(records, output / 'attachments/attachment_index.json', page_index)

    @staticmethod
    def _run_stage(command, log, progress, *, stage_file=None, checkpoints=None, total=0):
        last = None
        with log.open('w') as stream:
            process=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,env=dict(os.environ,PYTHONUNBUFFERED='1'))
            try:
                while process.poll() is None:
                    observed=None
                    try:
                        if stage_file and stage_file.is_file():
                            observed=json.loads(stage_file.read_text())
                        elif checkpoints and checkpoints.is_dir():
                            count=len(list(checkpoints.glob('*.json')))
                            observed=dict(stage='attachments',current=count,total=total,message=f'附件OCR {count}/{total}')
                    except (OSError, json.JSONDecodeError):
                        pass
                    if observed and observed != last:
                        progress(observed['stage'],observed['current'],observed['total'],observed['message'])
                        last=observed
                    time.sleep(.5)
            finally:
                if process.poll() is None:
                    stop_process(process)
        print(log.read_text(errors='replace'),flush=True)
        if process.returncode:
            raise RuntimeError(f'识别阶段进程退出（{process.returncode}），日志：{log.name}')

    def match(self, pdf, documents, output):
        from material_ocr.materials.rebar.review import generate_review
        from material_ocr.ocr.attachments import load_rules
        return generate_review(pdf, documents.attachment_index, documents.records, output,
            rules=load_rules(rebar_config(self.config['attachment_rules'])),
            matching_options=self.config['matching'])

    def statistics(self, result):
        from decimal import Decimal
        records = [e['record'] for e in result['entries']]
        quantity = self.config['statistics']['quantity_field']
        known = [Decimal(str(r[quantity])) for r in records if r.get(quantity) is not None]
        return dict(record_count=len(records), content_group_count=result['metrics']['distinct_content_groups'],
                    observed_entry_total=str(sum(known, Decimal(0))), quantity_unit=self.config['statistics']['unit'],
                    quantity_missing_records=len(records)-len(known), duplicates_included=True,
                    total_note='按原始记录累计，包含重复来源，不能当作去重进场总量',
                    matching=result['metrics'])

    def form_writer(self):
        from material_ocr.filling.excel import RebarLedgerWriter
        return RebarLedgerWriter()


def inspection_failures(manifest):
    """OCR warnings remain reviewable; incomplete/uncertain geometry blocks stage two."""
    failures = []
    for page in manifest['pages']:
        if page['decision'] != 'target':
            continue
        status = page.get('extraction_status')
        summary = page.get('summary') or {}
        warnings_only = (status == 'adaptive_needs_review' and summary.get('product_count',0) > 0
                         and summary.get('table_issue_count') == 0
                         and summary.get('business_issue_record_count') == 0
                         and not page.get('calibration_requires_review',True))
        if page.get('extraction_error') or (status != 'draft_ready_pending_review' and not warnings_only):
            failures.append(page['page_no'])
    return failures
