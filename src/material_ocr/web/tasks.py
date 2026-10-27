"""Persistent task state and a single process queue for GPU inference."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid

from material_ocr.materials.registry import MaterialRegistry
from material_ocr.processes import stop_process
from material_ocr.workflow import code_fingerprint, utc_now, write_json


class TaskService:
    def __init__(self, data_dir, model_cache, device='gpu:0', profile='batched_gpu', registry=None, runner=None,
                 *, artifact_policy='compact'):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.model_cache = Path(model_cache).resolve()
        self.device, self.profile = device, profile
        if artifact_policy not in ('compact', 'full'):
            raise ValueError('artifact_policy须为compact或full')
        self.artifact_policy = artifact_policy
        self.registry = registry or MaterialRegistry()
        self.runner = runner or self.run_document
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='material-ocr')
        self.jobs = {}
        self.process = None
        self.closing = False
        self.code_hash = code_fingerprint()
        for folder in sorted(self.root.glob('job-*')):
            state = folder / 'job.json'
            if state.is_file():
                job = json.loads(state.read_text())
                if job['status'] in ('queued', 'running'):
                    job.update(status='interrupted', ended_at=utc_now(), message='服务重启，任务未完成，可重新上传')
                    for document in job['documents']:
                        if document['status'] in ('queued', 'running'):
                            document.update(status='interrupted', error='服务重启')
                    write_json(state, job)
                for document in job['documents']:
                    if document['status'] in ('failed', 'interrupted'):
                        self.finalize_execution(job['id'], document, document['status'],
                                                error=document.get('error', job.get('message')))
                self.jobs[job['id']] = job

    def finalize_execution(self, job_id, document, status, *, code=None, error=None):
        """Supplement stale running records after a native crash or service stop."""
        output = self.root / job_id / 'documents' / document['id']
        paths = [self.root / job_id / 'executions' / (document['id']+'.json'),
                 output / 'experiment/metadata.json']
        for path in paths:
            if not path.is_file():
                continue
            observed = json.loads(path.read_text())
            if observed.get('status') != 'running':
                continue
            observed.update(status=status, end_time_utc=document.get('ended_at') or utc_now(),
                            error=error, reconciled_at=utc_now(), status_source='persistent job/process exit')
            if code is not None:
                observed['exit_code'] = code
            write_json(path, observed)
            if path.name == 'metadata.json':
                with (output / 'experiment/result.md').open('a') as stream:
                    stream.write(f"\n- 状态补记：{status}；退出码{code if code is not None else '未取得'}；"
                                 f"依据持久任务状态/进程退出。已有日志和版本保留。{error or ''}\n")

    def new_id(self):
        return 'job-' + time.strftime('%Y%m%dT%H%M%S', time.gmtime()) + '-' + uuid.uuid4().hex[:8]

    def create(self, job_id, material_id, layout, documents):
        adapter = self.registry.get(material_id)
        if layout not in adapter.info.layouts:
            raise ValueError('该材料不支持此检验表版式')
        job = dict(id=job_id, material_id=material_id, material_name=adapter.info.name,
                   layout=layout, created_at=utc_now(), status='queued', message='等待识别', documents=documents,
                   options=dict(device=self.device, profile=self.profile, artifact_policy=self.artifact_policy),
                   filling_available=adapter.form_writer() is not None)
        with self.lock:
            if self.closing:
                raise ValueError('服务正在停止')
            self.jobs[job_id] = job
            self.save(job)
            self.executor.submit(self.process_job, job_id)
        return self.public(job)

    def save(self, job):
        write_json(self.root / job['id'] / 'job.json', job)

    def public(self, job):
        value = json.loads(json.dumps(job))
        for document in value['documents']:
            document.pop('input_path', None)
            output = self.root / job['id'] / 'documents' / document['id']
            progress = output / 'progress.json'
            if progress.is_file():
                try:
                    document['progress'] = json.loads(progress.read_text())
                except (OSError, json.JSONDecodeError):
                    pass
            if document['status'] == 'completed':
                document['review_url'] = f"/artifacts/{job['id']}/documents/{document['id']}/review/index.html"
                document['summary_url'] = f"/artifacts/{job['id']}/documents/{document['id']}/summary.json"
                document['statistics_url'] = f"/artifacts/{job['id']}/documents/{document['id']}/statistics.json"
            if (output / 'work/inspection/index.html').is_file():
                document['inspection_url'] = f"/artifacts/{job['id']}/documents/{document['id']}/work/inspection/index.html"
            if (self.root / job['id'] / 'logs' / (document['id']+'.log')).is_file():
                document['log_url'] = f"/artifacts/{job['id']}/logs/{document['id']}.log"
        return value

    def list(self):
        with self.lock:
            return [self.public(job) for job in sorted(self.jobs.values(), key=lambda j: j['created_at'], reverse=True)]

    def get(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return self.public(self.jobs[job_id])

    def process_job(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            if self.closing:
                return
            job.update(status='running', started_at=utc_now(), message='正在识别和匹配')
            self.save(job)
        for document in job['documents']:
            with self.lock:
                if self.closing:
                    document.update(status='interrupted', error='服务停止')
                    self.save(job)
                    continue
                document.update(status='running', started_at=utc_now())
                self.save(job)
            output = self.root / job_id / 'documents' / document['id']
            try:
                self.runner(job, document, output)
                summary = json.loads((output / 'summary.json').read_text())
                if not (output / 'review/index.html').is_file():
                    raise ValueError('审核页面未生成')
                with self.lock:
                    document.update(status='completed', ended_at=utc_now(), metrics=summary['metrics'],
                                    statistics=summary['statistics'])
            except Exception as exc:
                with self.lock:
                    document.update(status='interrupted' if self.closing else 'failed', ended_at=utc_now(), error=str(exc))
            with self.lock:
                self.save(job)
        with self.lock:
            states = {d['status'] for d in job['documents']}
            status = 'completed' if states == {'completed'} else ('interrupted' if 'interrupted' in states else
                     ('partial_failed' if 'completed' in states else 'failed'))
            job.update(status=status, ended_at=utc_now(), message={'completed':'全部文件匹配完成',
                'partial_failed':'部分文件失败，其余结果可审核', 'failed':'任务失败，请查看文件日志',
                'interrupted':'任务已中断'}[status])
            self.save(job)

    def cache_key(self, job, document):
        payload = dict(pdf=document['sha256'], material=job['material_id'], layout=job['layout'],
                       options=job['options'], material_version=self.registry.get(job['material_id']).fingerprint(),
                       code=self.code_hash)
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def find_cached(self, key, name):
        with self.lock:
            for job in self.jobs.values():
                for doc in job['documents']:
                    if doc.get('cache_key') == key and doc['status'] == 'completed' and doc['name'] == name:
                        candidate = self.root / job['id'] / 'documents' / doc['id']
                        if (candidate / 'review/index.html').is_file() and (candidate / 'summary.json').is_file():
                            return candidate, job['id'], doc['id']
        return None

    def run_document(self, job, document, output):
        key = self.cache_key(job, document)
        document['cache_key'] = key
        cached = self.find_cached(key, document['name'])
        if cached:
            source, job_id, document_id = cached
            output.mkdir(parents=True)
            shutil.copytree(source / 'review', output / 'review', copy_function=shutil.copy2)
            document['reused_from'] = dict(job_id=job_id, document_id=document_id)
            summary = json.loads((source / 'summary.json').read_text())
            summary.update(new_ocr_executed=False, reused_from=document['reused_from'])
            write_json(output / 'summary.json', summary)
            shutil.copy2(source / 'statistics.json', output / 'statistics.json')
            write_json(output / 'progress.json', dict(stage='completed', current=1, total=1,
                       message='相同文件及规则已有结果，直接复用', updated_at=utc_now()))
            write_json(output / 'experiment/metadata.json', dict(run_id=job['id']+'-'+document['id'], status='completed',
                       change_id='chg-20261009T085532Z-local-web-pipeline', new_ocr_executed=False,
                       command=['reuse_cached_result'], working_directory=str(self.root), code_version=self.code_hash,
                       data_version=document['sha256'], config_reference=job['options'], seed=None,
                       start_time_utc=document['started_at'], end_time_utc=utc_now(), source=str(source)))
            (output / 'experiment/result.md').write_text(f'# 缓存复用\n- 状态：completed\n- 未运行OCR；来源{source}\n')
            return
        command = [sys.executable, '-m', 'material_ocr', 'analyze', '--pdf', document['input_path'],
                   '--out', str(output), '--material', job['material_id'], '--layout', job['layout'],
                   '--device', self.device, '--profile', self.profile, '--model-cache', str(self.model_cache),
                   '--artifact-policy', self.artifact_policy,
                   '--run-id', job['id']+'-'+document['id']]
        execution = dict(run_id=job['id']+'-'+document['id'], status='running', start_time_utc=utc_now(),
                         command=command, working_directory=str(Path.cwd()), code_version=self.code_hash,
                         data_version=document['sha256'], config_reference=job['options'], seed=None,
                         material_id=job['material_id'], change_id='chg-20261009T085532Z-local-web-pipeline')
        execution_path = self.root / job['id'] / 'executions' / (document['id']+'.json')
        write_json(execution_path, execution)
        log = self.root / job['id'] / 'logs' / (document['id']+'.log')
        log.parent.mkdir(exist_ok=True)
        env = dict(os.environ, PYTHONUNBUFFERED='1')
        with log.open('w') as stream:
            with self.lock:
                if self.closing:
                    raise RuntimeError('服务停止')
                process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, env=env,
                                           start_new_session=os.name == 'posix')
                self.process = process
            try:
                code = process.wait()
            finally:
                with self.lock:
                    self.process = None
        if code != 0:
            execution.update(status='interrupted' if self.closing else 'failed', exit_code=code, end_time_utc=utc_now())
            write_json(execution_path, execution)
            self.finalize_execution(job['id'], document, execution['status'], code=code,
                                    error='OCR子进程在运行期间退出')
            progress = output / 'progress.json'
            observed = json.loads(progress.read_text()) if progress.is_file() else {}
            message = observed.get('message') if observed.get('stage') in ('failed', 'interrupted') else None
            raise RuntimeError(message or f'OCR子进程退出（{code}），请查看日志')
        execution.update(status='completed', exit_code=0, end_time_utc=utc_now())
        write_json(execution_path, execution)

    def close(self):
        with self.lock:
            self.closing = True
            process = self.process
        if process is not None:
            stop_process(process, process_group=True)
        self.executor.shutdown(wait=True, cancel_futures=True)
        with self.lock:
            for job in self.jobs.values():
                if job['status'] in ('queued', 'running'):
                    job.update(status='interrupted', ended_at=utc_now(), message='服务停止，任务未完成')
                    for document in job['documents']:
                        if document['status'] in ('queued', 'running'):
                            document.update(status='interrupted', error='服务停止')
                    self.save(job)
