"""Short-lived stages release GPU predictors before another stage starts."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from material_ocr.workflow import configure_environment, write_json
from material_ocr.paths import rebar_config


def main():
    p=argparse.ArgumentParser()
    p.add_argument('stage',choices=['inspection','attachments'])
    p.add_argument('--pdf',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--device',required=True)
    p.add_argument('--model-cache',type=Path,required=True)
    p.add_argument('--layout',choices=['auto','6_rows','11_rows','adaptive'],default='auto')
    p.add_argument('--page-index',type=Path)
    p.add_argument('--pages',nargs='+',type=int)
    p.add_argument('--profile',default='batched_gpu')
    p.add_argument('--batch-size',type=int)
    p.add_argument('--dpi',type=int,default=180)
    args=p.parse_args()
    configure_environment(args.model_cache)
    if args.stage=='inspection':
        from material_ocr.extraction.batch import run
        templates=[rebar_config('inspection_6_rows.json'),rebar_config('inspection_11_rows.json')]
        if args.layout not in ('auto','adaptive'):
            templates=[rebar_config(f'inspection_{args.layout}.json')]
        def report(stage,current,total,message):
            write_json(args.out.parent/'stage_progress.json',dict(stage=stage,current=current,total=total,message=message))
        run(SimpleNamespace(pdf=str(args.pdf),out=str(args.out),template=str(templates[0]),templates=templates,
                            device=args.device,scan_dpi=180,extract_draft=True,
                            entry_backend='adaptive' if args.layout=='adaptive' else 'template'),progress=report)
    else:
        from material_ocr.ocr.attachments import build_attachment_index,load_rules
        rules=load_rules(rebar_config('attachment_rules.json'))
        rules['ocr_runtime'].update(profile=args.profile,recognition_batch_size=args.batch_size)
        build_attachment_index(args.pdf,args.out,page_index_path=args.page_index,device=args.device,dpi=args.dpi,
                               rules=rules,page_nos=args.pages)


if __name__=='__main__':
    main()
