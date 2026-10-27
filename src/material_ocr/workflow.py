"""One material, one PDF, one isolated run. Material adapters own business rules."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from material_ocr.materials.base import ExtractedDocuments, RuntimeOptions
from material_ocr.materials.registry import MaterialRegistry


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def code_fingerprint():
    root = Path(__file__).parent
    result = hashlib.sha256()
    for source in sorted(p for p in root.rglob('*') if p.suffix in ('.py', '.json', '.html', '.js', '.css')):
        result.update(str(source.relative_to(root)).encode())
        result.update(source.read_bytes())
    return result.hexdigest()


def run_pipeline(pdf, output, material_id='rebar', options=None, *, progress=None,
                 replay_documents=None, registry=None, run_id=None):
    """Supports genuine OCR or explicit saved-evidence replay; never confuses the two."""
    pdf, output = Path(pdf).resolve(), Path(output).resolve()
    options = options or RuntimeOptions()
    if options.artifact_policy not in ('compact', 'full'):
        raise ValueError('artifact_policy须为compact或full')
    adapter = (registry or MaterialRegistry()).get(material_id)
    if options.inspection_layout not in adapter.info.layouts:
        raise ValueError('该材料不支持此检验表版式：' + options.inspection_layout)
    output.mkdir(parents=True, exist_ok=False)
    work = output / 'work'
    work.mkdir()
    run_id = run_id or ('run-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-pipeline')
    meta = dict(run_id=run_id, change_id='chg-20261009T085532Z-local-web-pipeline',
                status='running', start_time_utc=utc_now(), command=sys.argv,
                working_directory=str(Path.cwd()), code_version=code_fingerprint(),
                data_version={str(pdf): digest(pdf)}, config_reference=asdict(options),
                material_id=material_id, material_fingerprint=adapter.fingerprint(), seed=None,
                seed_reason='deterministic inference/replay, no random sampling configured',
                new_ocr_executed=replay_documents is None,
                environment=dict(python=sys.version, platform=platform.platform()),
                source_artifacts={k: str(v) for k, v in asdict(replay_documents).items() if v is not None} if replay_documents else None)
    for package in ['paddlepaddle-gpu', 'paddleocr', 'paddlex', 'numpy', 'fastapi']:
        try:
            meta['environment'][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            meta['environment'][package] = None
    try:
        probe = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=3)
        meta['git_commit'] = probe.stdout.strip() if probe.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        meta['git_commit'] = None
    write_json(output / 'experiment/metadata.json', meta)
    source_root = Path(__file__).parent
    sources = [p for p in source_root.rglob('*') if p.suffix in ('.py', '.json', '.html', '.css', '.js')]
    import shutil
    for source in sources:
        target = output / 'experiment/code-snapshot' / source.relative_to(source_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    progress_callback = progress

    def report(stage, current, total, message):
        write_json(output / 'progress.json', dict(stage=stage, current=current, total=total,
                                                 message=message, updated_at=utc_now()))
        if progress_callback:
            progress_callback(stage, current, total, message)

    try:
        report('starting', 0, 1, '准备识别任务' if replay_documents is None else '复用已有OCR证据，重放匹配')
        if replay_documents:
            for path in [replay_documents.records, replay_documents.attachment_index]:
                meta['data_version'][str(path)] = digest(path)
            documents = replay_documents
            write_json(output / 'experiment/metadata.json', meta)
        else:
            documents = adapter.extract(pdf, work, options, report)
        report('matching', 0, 1, '按材料规则关联表单并生成审核页')
        result = adapter.match(pdf, documents, output / 'review')
        statistics = adapter.statistics(result)
        write_json(output / 'statistics.json', statistics)
        write_json(output / 'summary.json', dict(material_id=material_id, pdf_name=pdf.name,
                   pdf_sha256=digest(pdf), metrics=result['metrics'], statistics=statistics,
                   review='review/index.html', new_ocr_executed=replay_documents is None,
                   write_allowed=False))
        from material_ocr.storage_artifacts import compact_artifacts
        report('archiving', 0, 1, '整理可再生诊断图片，保存OCR证据与台账')
        storage = compact_artifacts(output, policy=options.artifact_policy)
        meta.update(status='completed', end_time_utc=utc_now(), exit_code=0,
                    artifacts=dict(summary='summary.json', review='review/index.html', statistics='statistics.json'),
                    metrics=result['metrics'], storage=storage)
        report('completed', 1, 1, '匹配完成，可打开审核结果')
        return result
    except BaseException as exc:
        meta.update(status='interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'failed',
                    end_time_utc=utc_now(), error_type=type(exc).__name__, error=str(exc), exit_code=1)
        report(meta['status'], 0, 1, str(exc))
        raise
    finally:
        write_json(output / 'experiment/metadata.json', meta)
        (output / 'experiment/result.md').write_text(
            f"# 任务运行 {run_id}\n- 状态：{meta['status']}\n- 模式：{'真实OCR' if replay_documents is None else '保存OCR回放'}\n"
            f"- 材料：{material_id}\n- 起止：{meta['start_time_utc']} / {meta.get('end_time_utc')}\n"
            f"- 原始数据、配置和环境：metadata.json\n- 产物：summary.json、statistics.json、review/；失败保留work/。\n"
            f"- 错误：{meta.get('error', '无')}\n- 无人工真值，候选不等于准确率；正式写入禁用。\n", encoding='utf-8')


def configure_environment(model_cache):
    os.environ['PADDLE_PDX_CACHE_HOME'] = str(Path(model_cache).resolve())
    os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
    if Path('/usr/lib/wsl/lib').is_dir():
        os.environ['LD_LIBRARY_PATH'] = '/usr/lib/wsl/lib:' + os.environ.get('LD_LIBRARY_PATH', '')
