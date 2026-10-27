"""Standalone inference process, run with the user's validated GLM Python environment."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import sys
import time


def follow_parent_lifetime():
    if sys.platform.startswith('linux'):
        import ctypes
        parent = os.getppid()
        if parent == 1:
            raise SystemExit('Parent exited before inference started')
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM) != 0:
            raise OSError('Cannot establish inference child lifetime')
        if os.getppid() != parent:
            raise SystemExit('Parent exited while inference started')


def main():
    follow_parent_lifetime()
    p = argparse.ArgumentParser()
    p.add_argument('--image', type=Path)
    p.add_argument('--out', type=Path)
    p.add_argument('--check', action='store_true')
    p.add_argument('--model', required=True)
    p.add_argument('--device', default='cuda:0')
    p.add_argument('--max-new-tokens', type=int, default=16384)
    args = p.parse_args()
    import torch
    from PIL import Image
    from transformers import AutoProcessor, GlmOcrForConditionalGeneration, StoppingCriteriaList
    if __package__:
        from .glm_blank_stop import BlankRowsStoppingCriteria
    else:
        from glm_blank_stop import BlankRowsStoppingCriteria
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA is not available in the GLM environment')
    torch.cuda.set_device(args.device)
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError('GLM inference requires BF16 GPU support in this release')
    if args.check:
        value = torch.ones((8,8), device=args.device, dtype=torch.bfloat16)
        result = (value @ value).sum().item()
        print(json.dumps(dict(check='passed', torch=torch.__version__, gpu=torch.cuda.get_device_name(),
                              executable=sys.executable, bf16_matmul_sum=result),ensure_ascii=False))
        return
    if args.image is None or args.out is None:
        p.error('Inference requires --image and --out')
    started = time.perf_counter()
    processor = AutoProcessor.from_pretrained(args.model, local_files_only=True)
    model = GlmOcrForConditionalGeneration.from_pretrained(args.model, dtype=torch.bfloat16,
        local_files_only=True, attn_implementation='sdpa').to(args.device).eval()
    torch.cuda.synchronize()
    load_seconds = time.perf_counter() - started
    with Image.open(args.image) as original:
        image = original.convert('RGB')
    prompt = 'Table Recognition:'
    messages = [{'role':'user','content':[{'type':'image','image':image},{'type':'text','text':prompt}]}]
    torch.cuda.reset_peak_memory_stats()
    started = time.perf_counter()
    inputs = processor.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
        return_dict=True, return_tensors='pt').to(model.device)
    blank_stop = BlankRowsStoppingCriteria(processor, inputs['input_ids'].shape[-1])
    with torch.inference_mode():
        output = model.generate(**inputs, max_new_tokens=args.max_new_tokens, do_sample=False,
                                stopping_criteria=StoppingCriteriaList([blank_stop]))
    torch.cuda.synchronize()
    seconds = time.perf_counter() - started
    tokens = output[0, inputs['input_ids'].shape[-1]:]
    raw = processor.decode(tokens, skip_special_tokens=True, clean_up_tokenization_spaces=False)
    (args.out/'raw.txt').write_text(raw, encoding='utf-8')
    info = dict(model=args.model, model_commit=getattr(model.config, '_commit_hash', None), prompt=prompt,
        python=sys.version, executable=sys.executable, device=args.device,
        gpu=torch.cuda.get_device_name(), input_image_sha256=hashlib.sha256(args.image.read_bytes()).hexdigest(),
        input_size=list(image.size), output_tokens=len(tokens), max_new_tokens=args.max_new_tokens,
        hit_token_limit=len(tokens) >= args.max_new_tokens, model_load_seconds=load_seconds,
        generation_stop=blank_stop.detector.metadata(),
        inference_seconds=seconds, peak_allocated_gib=torch.cuda.max_memory_allocated()/1024**3,
        packages={name:importlib.metadata.version(name) for name in ('torch','transformers','Pillow')})
    (args.out/'inference.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(info, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
