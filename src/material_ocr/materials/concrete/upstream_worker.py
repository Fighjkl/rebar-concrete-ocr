"""Concrete-only routing around the shared Paddle/GLM execution primitives."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[4]


def run_worker(pdf, output, *, model_dir, glm_python, device, progress, max_tokens):
    from material_ocr.materials.rebar.upstream_pipeline import write_json

    output = Path(output)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1",
        MATERIAL_OCR_GLM_PYTHON=str(glm_python),
        MATERIAL_OCR_GLM_MODEL=str(model_dir),
        MATERIAL_OCR_GLM_MAX_TOKENS=str(max_tokens),
        PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]))
    command = [sys.executable, "-u", "-m", "material_ocr.materials.concrete.upstream_worker",
               "--pdf", str(pdf), "--out", str(output / "upstream"),
               "--device", device.replace("cuda:", "gpu:")]
    write_json(output / "upstream_command.json", command)
    with (output / "upstream.log").open("w", encoding="utf-8") as log:
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, encoding="utf-8", errors="replace", start_new_session=os.name == "posix")
        try:
            for line in child.stdout:
                log.write(line)
                log.flush()
                if line.startswith("UPSTREAM_PROGRESS "):
                    if progress:
                        progress(*json.loads(line.removeprefix("UPSTREAM_PROGRESS ")))
                elif progress and ("Loading" in line or "page " in line or "方向模型" in line):
                    print(line.rstrip(), flush=True)
            code = child.wait()
        finally:
            if child.poll() is None:
                from material_ocr.processes import stop_process
                stop_process(child, process_group=os.name == "posix")
    if code:
        raise RuntimeError(f"混凝土上游子进程退出 {code}，查看 {output / 'upstream.log'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", default="gpu:0")
    args = parser.parse_args()
    from material_ocr.upstream_legacy.extraction.batch import run
    from material_ocr.upstream_legacy.paths import rebar_config
    from .routing import classify_page_header

    def progress(stage, current, total, message):
        print("UPSTREAM_PROGRESS " + json.dumps(
            [stage, current, total, message], ensure_ascii=False), flush=True)

    # B.0.6 geometry and OCR execution remain reusable; classification is concrete-only.
    run(SimpleNamespace(pdf=args.pdf, out=args.out, device=args.device, scan_dpi=180,
        entry_backend="glm", extract_draft=True,
        template=str(rebar_config("inspection_11_rows.json")),
        submission_template=str(rebar_config("submission_b06.json"))),
        progress=progress, header_classifier=classify_page_header)


if __name__ == "__main__":
    main()
