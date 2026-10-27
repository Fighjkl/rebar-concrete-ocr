"""Run the repository's Paddle Anchors entry point from web recognition jobs."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from material_ocr.materials.rebar.glm_production import get_glm_cache_identity, PROJECT_ROOT
from material_ocr.ocr.backends import get_backend
from material_ocr.processes import stop_process
from material_ocr.workflow import code_fingerprint


PIPELINE_ID = "paddle-anchors-hybrid"
ENTRYPOINT = PROJECT_ROOT / "autodl_test_paddle_anchors.py"
PROGRESS_RE = re.compile(r"^\[([^]]+)\]\s+(\d+)/(\d+)\s+(.*)$")


def glm_runtime():
    python = Path(os.environ.get("MATERIAL_OCR_GLM_PYTHON") or
                  PROJECT_ROOT / ".venv-glm/bin/python").expanduser().absolute()
    model = Path(os.environ.get("MATERIAL_OCR_GLM_MODEL_DIR") or
                 PROJECT_ROOT / "models/GLM-OCR").expanduser().resolve()
    return python, model


def paddle_cache_fingerprint(model_dir):
    root = Path(model_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"本机 Paddle 模型缓存不存在：{root}")
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".cache" in path.parts:
            continue
        stat = path.stat()
        files.append((path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns))
    if not files:
        raise FileNotFoundError(f"本机 Paddle 模型缓存为空：{root}")
    return hashlib.sha256(json.dumps(files, separators=(",", ":")).encode()).hexdigest()


def pipeline_identity(model_cache, device):
    model_cache = Path(model_cache).resolve()
    paddle_model = model_cache / "official_models"
    glm_python, glm_model = glm_runtime()
    if not glm_python.is_file():
        raise FileNotFoundError(f"本机 GLM Python 不存在：{glm_python}")
    paddle_identity = get_backend("local-paddle").fingerprint(paddle_model)
    paddle_inventory = paddle_cache_fingerprint(paddle_model)
    glm_identity = get_glm_cache_identity(glm_model, ocr_model="local-glm")
    files = {}
    for path in (ENTRYPOINT, PROJECT_ROOT / "autodl_test_upstream.py",
                 PROJECT_ROOT / "tools/compare_ocr_ledgers.py"):
        files[str(path.relative_to(PROJECT_ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(pipeline=PIPELINE_ID, entrypoint_files=files,
                source_fingerprint=code_fingerprint(), glm=glm_identity,
                glm_python=str(glm_python), paddle=paddle_identity,
                paddle_model_inventory=paddle_inventory,
                paddle_model_cache=str(model_cache), device=str(device),
                mode="full", max_tokens=16384, attachment_rotations=[0])


def build_command(pdf, output, model_cache, device, *, force_attachments=False, material_type='rebar'):
    if material_type not in ('rebar', 'concrete'):
        raise ValueError('无效材料类别：' + str(material_type))
    glm_python, glm_model = glm_runtime()
    device = str(device)
    if device.startswith("gpu"):
        device = device.replace("gpu", "cuda", 1)
    command = [sys.executable, "-u", str(ENTRYPOINT), "--pdf", str(Path(pdf).resolve()),
               "--mode", "full", "--out", str(Path(output).resolve()),
               "--device", device, "--glm-python", str(glm_python),
               "--model-dir", str(glm_model), "--model-cache", str(Path(model_cache).resolve()),
               "--cache-dir", str((PROJECT_ROOT / "runs/web/ocr-cache/"
                                    "paddle-anchors-attachments").resolve())]
    if force_attachments:
        command.append("--force-attachments")
    if material_type == 'concrete':
        command.extend(['--material', 'concrete'])
    return command


def run_entrypoint(pdf, output, model_cache, device, *, force_attachments=False, progress=None, material_type='rebar'):
    """Run the exact CLI entry point and keep its stdout beside the run output."""
    output = Path(output).resolve()
    command = build_command(pdf, output, model_cache, device,
                            force_attachments=force_attachments, material_type=material_type)
    log_path = output.parent / (output.name + ".paddle-anchors.log")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1",
               PADDLE_PDX_CACHE_HOME=str(Path(model_cache).resolve()),
               MATERIAL_OCR_PADDLE_MODELS_ROOT=str(Path(model_cache).resolve() / "official_models"),
               PYTHONPATH=os.pathsep.join([str(PROJECT_ROOT / "src"), str(PROJECT_ROOT)]))
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1,
            start_new_session=os.name == "posix")
        try:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                log.flush()
                match = PROGRESS_RE.match(line.rstrip())
                if match and progress:
                    progress(match.group(1), int(match.group(2)), int(match.group(3)), match.group(4))
            code = process.wait()
        finally:
            if process.poll() is None:
                stop_process(process, process_group=os.name == "posix")
    if code:
        raise RuntimeError(f"{ENTRYPOINT.name} 退出码 {code}，查看日志 {log_path}")
    ledger_path = output / "automatic_ledger.json"
    if not ledger_path.is_file():
        raise RuntimeError(f"{ENTRYPOINT.name} 未生成台账，查看日志 {log_path}")
    return json.loads(ledger_path.read_text(encoding="utf-8")), log_path
