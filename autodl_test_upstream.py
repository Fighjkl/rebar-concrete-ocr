#!/usr/bin/env python3
"""Standalone AutoDL entry. Never installs the package over the existing one."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
os.environ["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
os.environ.setdefault("PYTHONUTF8", "1")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--mode", choices=["inspection", "full"], default="inspection")
    parser.add_argument("--material", choices=["rebar", "concrete"], default="rebar")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--glm-python", type=Path)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--model-cache", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "runs/paddle-anchors-attachment-cache")
    parser.add_argument("--reuse-upstream", type=Path, help="已完成 inspection 结果中的 upstream 文件夹")
    parser.add_argument("--max-tokens", type=int, default=16384)
    parser.add_argument("--force-attachments", action="store_true")
    parser.add_argument("--attachment-rotations", nargs="+", type=int, choices=[0,90,180,270], default=[0],
                        help="默认快速方向分类+一次整页OCR，异常页按需重试；多个值强制相对选定方向整页复读，供铭牌融合")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("GPU 推理入口用于 AutoDL/Linux；本地可运行 tests_upstream 离线测试")
    runtime = ROOT / ".autodl-runtime.json"
    settings = json.loads(runtime.read_text(encoding="utf-8")) if runtime.exists() else {}
    python = args.glm_python or settings.get("glm_python")
    model = args.model_dir or settings.get("model_dir")
    cache = args.model_cache or settings.get("model_cache", ROOT / "models/paddlex_cache")
    if not python or not model:
        parser.error("请先运行 prepare_autodl.py --base 原项目目录，或指定 --glm-python 和 --model-dir")
    # Set BEFORE importing Paddle/PaddleX; applies only to this process tree.
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(Path(cache).resolve())
    os.environ["MATERIAL_OCR_PADDLE_MODELS_ROOT"] = str(Path(cache).resolve() / "official_models")
    os.environ["MATERIAL_OCR_GLM_PYTHON"] = str(Path(python).expanduser().absolute())
    os.environ["MATERIAL_OCR_GLM_MODEL_DIR"] = str(Path(model).resolve())
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    output = args.out or ROOT / "runs" / ("upstream-" + args.mode + "-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    print(f"输入 PDF：{args.pdf.resolve()}\n结果目录：{output.resolve()}", flush=True)
    if args.material == "concrete":
        from material_ocr.materials.concrete.pipeline import run_pipeline
    else:
        from material_ocr.materials.rebar.upstream_pipeline import run_pipeline

    def progress(stage, current, total, message):
        print(f"[{stage}] {current}/{total} {message}", flush=True)

    result = run_pipeline(args.pdf, output, mode=args.mode, model_dir=model,
        glm_python=python, device=args.device, cache_dir=args.cache_dir,
        reuse_upstream=args.reuse_upstream, max_tokens=args.max_tokens,
        force_attachments=args.force_attachments, progress=progress,
        paddle_model_dir=Path(cache).resolve() / "official_models",
        attachment_rotations=args.attachment_rotations)
    if args.mode == "inspection":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n完成。结果：{output.resolve()}", flush=True)
    return 0 if args.mode != "inspection" or result["full_pipeline_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
