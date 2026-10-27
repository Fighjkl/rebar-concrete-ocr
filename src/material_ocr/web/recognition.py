"""Short-lived HTTP requests over durable, deduplicated local OCR jobs."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
import threading
import uuid

from material_ocr.workflow import write_json


ACTIVE = {'queued', 'running'}


def now():
    return datetime.now(timezone.utc).isoformat()


class RecognitionQueue:
    """One worker serializes volumes; the service's GPU lock also covers legacy jobs.

    Persist state rather than silently resuming work after a process restart. OCR
    subprocesses cannot safely be presumed alive after their owning process exits.
    """
    def __init__(self, root):
        self.root = root
        self.guard = threading.RLock()
        self.tasks = {}
        self.futures = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='library-ocr')

    def _load(self, name):
        if name in self.tasks:
            return self.tasks[name]
        path = self.root / name / 'recognition.json'
        if path.is_file():
            state = json.loads(path.read_text())
            if state.get('status') in ACTIVE:
                state.update(status='interrupted', stage='interrupted', updated_at=now(),
                             message='OCR服务已重启，上次任务已中断；可重新启动识别',
                             error='owning_process_restarted')
                write_json(path, state)
            self.tasks[name] = state
            return state
        return dict(pdf_name=name, task_id=None, status='idle', stage='idle',
                    current=0, total=0, message='尚未启动识别')

    def _save(self, name, state):
        self.tasks[name] = state
        write_json(self.root / name / 'recognition.json', state)

    def status(self, name):
        with self.guard:
            state = deepcopy(self._load(name))
            queued = [n for n, t in self.tasks.items() if t['status'] == 'queued']
            state['queue_position'] = queued.index(name) + 1 if name in queued else 0
            return state

    def active(self, name):
        return self.status(name)['status'] in ACTIVE

    def forget(self, name):
        with self.guard:
            self.tasks.pop(name, None)
            self.futures.pop(name, None)
            (self.root / name / 'recognition.json').unlink(missing_ok=True)

    def submit(self, name, runner, *, cached=False, force=False, model_id=None):
        with self.guard:
            previous = self._load(name)
            if previous['status'] in ACTIVE:
                return self.status(name)
            if cached and not force and previous['status'] == 'completed':
                return self.status(name)
            state = dict(pdf_name=name, task_id=uuid.uuid4().hex,
                         model_id=model_id,
                         status='completed' if cached and not force else 'queued',
                         stage='completed' if cached and not force else 'queued',
                         current=1 if cached and not force else 0, total=1,
                         message='复用已完成的OCR台账' if cached and not force else '已进入本地OCR队列',
                         created_at=now(), updated_at=now(), error=None,
                         cached=bool(cached and not force))
            self._save(name, state)
            self.futures.pop(name, None)
            if state['status'] == 'queued':
                self.futures[name] = self.executor.submit(self._run, name, state['task_id'], runner)
            return self.status(name)

    def _run(self, name, task_id, runner):
        def progress(stage, current, total, message):
            with self.guard:
                state = self.tasks[name]
                if state['task_id'] != task_id:
                    return
                # Pipeline completion precedes ledger normalization/persistence.
                # Keep this task running until its result really is available.
                state.update(status='running', stage='saving' if stage == 'completed' else stage,
                             current=current, total=total, message=message, updated_at=now())
                self._save(name, state)
        try:
            runner(progress)
        except BaseException as exc:
            with self.guard:
                state = self.tasks[name]
                state.update(status='failed', stage='failed', error=str(exc),
                             message='识别失败：' + str(exc), updated_at=now())
                self._save(name, state)
            raise
        else:
            with self.guard:
                state = self.tasks[name]
                state.update(status='completed', stage='completed', current=1, total=1,
                             message='OCR与自动匹配已完成，可核验关键字段', updated_at=now())
                self._save(name, state)

    def wait(self, name):
        with self.guard:
            future = self.futures.get(name)
        if future:
            future.result()

    def shutdown(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
