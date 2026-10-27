#!/usr/bin/env python3
"""Prepare this NEW folder using read-only inputs from the existing installation."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--glm-python", type=Path)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--model-cache", type=Path)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("此准备脚本用于 AutoDL/Linux")
    base = args.base.resolve()
    if ROOT == base or ROOT.is_relative_to(base):
        parser.error("请将测试版放在原项目以外的独立文件夹中")
    if (ROOT / ".autodl-runtime.json").exists():
        parser.error("已经准备过，直接运行 autodl_test_upstream.py；不覆盖现有配置")
    python = (args.glm_python or base / ".venv-glm/bin/python").expanduser().absolute()
    model = (args.model_dir or base / "models/GLM-OCR").resolve()
    old_cache = (args.model_cache or base / "models/paddlex_cache").resolve()
    if not base.is_dir() or not python.is_file() or not (model / "config.json").is_file():
        parser.error("原项目、GLM Python 或模型路径不存在，请用参数指定实际位置")
    missing = [m for m in ("paddle", "paddleocr", "paddlex", "cv2", "numpy", "pypdfium2", "PIL", "xlsxwriter")
               if importlib.util.find_spec(m) is None]
    if missing:
        parser.error("当前 Python 缺少 " + ", ".join(missing) + "；先 conda activate material-ocr")
    subprocess.run([str(python), "-c", """
import torch, transformers, cv2, pypdfium2, PIL
from transformers import GlmOcrForConditionalGeneration
assert torch.cuda.is_available(), 'GLM Python cannot access CUDA'
assert torch.cuda.is_bf16_supported(), 'BF16 GPU required'
print('GLM:', torch.__version__, transformers.__version__, torch.cuda.get_device_name())
"""], check=True)
    new_model = ROOT / "models/GLM-OCR"
    new_model.mkdir(parents=True, exist_ok=False)
    # Individual read-only-use model links: lock files are created in the NEW dir.
    for path in model.iterdir():
        if path.is_file() and not path.name.startswith("."):
            (new_model / path.name).symlink_to(path)
    new_cache = ROOT / "models/paddlex_cache"
    new_cache.mkdir(parents=True, exist_ok=False)
    if (old_cache / "official_models").is_dir():
        # Actual copies isolate Paddle download/verification writes from the old run.
        print("复制已有 Paddle 模型到新缓存（GLM 大权重仅建立文件链接）", flush=True)
        shutil.copytree(old_cache / "official_models", new_cache / "official_models")
    else:
        print("未找到原 Paddle 缓存；首次运行会在新目录下载页头及方向模型", flush=True)
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(new_cache)
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    sys.path.insert(0,str(ROOT/"src"))
    from material_ocr.materials.rebar.paddle_attachment_orientation import create_classifier
    print("检查轻量方向模型（只初始化，不执行四方向 OCR）",flush=True)
    create_classifier(new_cache/"official_models","cpu")
    settings = dict(base=str(base), glm_python=str(python), model_dir=str(new_model),
                    model_cache=str(new_cache))
    (ROOT / ".autodl-runtime.json").write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    print("准备完成；无需 npm、前端或 pip install -e。\n" + json.dumps(settings, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
