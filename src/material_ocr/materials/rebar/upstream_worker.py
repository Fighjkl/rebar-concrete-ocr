"""Short-lived Paddle routing/submission + isolated GLM entry inference."""
import argparse
import json
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", default="gpu:0")
    args = parser.parse_args()
    from material_ocr.upstream_legacy.extraction.batch import run
    from material_ocr.upstream_legacy.paths import rebar_config

    def progress(stage, current, total, message):
        print("UPSTREAM_PROGRESS " + json.dumps(
            [stage, current, total, message], ensure_ascii=False), flush=True)

    run(SimpleNamespace(pdf=args.pdf, out=args.out, device=args.device, scan_dpi=180,
        entry_backend="glm", extract_draft=True,
        template=str(rebar_config("inspection_11_rows.json")),
        submission_template=str(rebar_config("submission_b06.json"))), progress=progress)


if __name__ == "__main__":
    main()
