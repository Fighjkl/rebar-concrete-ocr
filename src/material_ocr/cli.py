"""Stable public commands for the local web app, OCR and evidence replay."""
import argparse
import json
import os
from pathlib import Path

from material_ocr.materials.base import ExtractedDocuments, RuntimeOptions
from material_ocr.workflow import configure_environment, run_pipeline


def parser():
    p = argparse.ArgumentParser(description='材料表单OCR、匹配与人工审核')
    sub = p.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve', help='启动本地上传网页')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', default=8080, type=int)
    serve.add_argument('--data-dir', type=Path, default=Path('runs/web'))
    serve.add_argument('--model-cache', type=Path, default=Path('models/paddlex_cache'))
    serve.add_argument('--device', default='gpu:0')
    serve.add_argument('--profile', choices=['batched_gpu', 'complete'], default='batched_gpu')
    serve.add_argument('--review-library', type=Path, help='包含已生成审核结果的本地目录，如runs')
    serve.add_argument('--artifact-policy', choices=['compact', 'full'],
                       default=os.environ.get('OCR_ARTIFACT_POLICY', 'compact'),
                       help='compact按需恢复诊断图片；full永久保留所有调试图片')
    analyze = sub.add_parser('analyze', help='从PDF开始完整OCR与匹配')
    replay = sub.add_parser('replay', help='复用同PDF的已保存OCR与原记录，仅重放匹配')
    for command in [analyze, replay]:
        command.add_argument('--pdf', type=Path, required=True)
        command.add_argument('--out', type=Path, required=True)
        command.add_argument('--material', default='rebar')
        command.add_argument('--device', default='gpu:0')
        command.add_argument('--profile', choices=['batched_gpu', 'complete'], default='batched_gpu')
        command.add_argument('--layout', default='auto', help='材料适配器支持的检验表版式；钢筋为auto/6_rows/11_rows/adaptive')
        command.add_argument('--model-cache', type=Path, default=Path('models/paddlex_cache'))
        command.add_argument('--run-id')
        command.add_argument('--artifact-policy', choices=['compact', 'full'],
                             default=os.environ.get('OCR_ARTIFACT_POLICY', 'compact'))
    replay.add_argument('--records', type=Path, required=True)
    replay.add_argument('--index', type=Path, required=True)
    replay.add_argument('--page-index', type=Path)
    sub.add_parser('materials', help='列出材料与已实现能力')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'materials':
        from material_ocr.materials.registry import MaterialRegistry
        print(json.dumps(MaterialRegistry().list(), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'serve':
        if args.profile == 'batched_gpu' and not args.device.startswith('gpu'):
            raise SystemExit('CPU服务使用 --device cpu --profile complete')
        import uvicorn
        from material_ocr.web.app import create_app
        app = create_app(args.data_dir, model_cache=args.model_cache, device=args.device,
                         profile=args.profile, review_library=args.review_library,
                         artifact_policy=args.artifact_policy)
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    if args.profile == 'batched_gpu' and not args.device.startswith('gpu'):
        raise SystemExit('CPU运行使用 --device cpu --profile complete')
    configure_environment(args.model_cache)
    documents = ExtractedDocuments(args.records.resolve(), args.index.resolve(),
                                   args.page_index.resolve() if args.page_index else None) if args.command == 'replay' else None
    run_pipeline(args.pdf, args.out, args.material,
                 RuntimeOptions(args.device, args.profile, inspection_layout=args.layout,
                                artifact_policy=args.artifact_policy),
                 replay_documents=documents, run_id=args.run_id)
    return 0
