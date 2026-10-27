"""Experimental local GLM-OCR inference; no invented word boxes or cloud calls."""
from __future__ import annotations

import hashlib
import fcntl
import json
from pathlib import Path
import time


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def model_fingerprint(directory):
    """Pin actual local checkpoint content, not a moving remote model name."""
    directory = Path(directory).resolve()
    required = ['config.json', 'model.safetensors', 'processor_config.json',
                'preprocessor_config.json', 'tokenizer.json', 'tokenizer_config.json',
                'chat_template.jinja', 'generation_config.json']
    hashes = {name: sha256(directory / name) for name in required}
    return dict(directory=str(directory), files=hashes,
                fingerprint=hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest())


class GlmOcrEngine:
    """Load shared read-only weights once and release image tensors after each image."""

    def __init__(self, model_dir, *, device='cuda:0', max_new_tokens=4096,
                 prompt='Text Recognition:', attn_implementation='sdpa'):
        import torch
        from transformers import AutoProcessor, GlmOcrForConditionalGeneration

        self.model_dir = Path(model_dir).resolve()
        self.device = device
        self.prompt = prompt
        self.max_new_tokens = max_new_tokens
        self.torch = torch
        if device.startswith('cuda') and not torch.cuda.is_available():
            raise RuntimeError('GLM-OCR requires an accessible CUDA device; run outside the restricted GPU sandbox')
        self._model_lock = (self.model_dir / '.local-inference.lock').open('a+')
        try:
            fcntl.flock(self._model_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._model_lock.close()
            raise RuntimeError('GLM-OCR is already running with this local checkpoint; wait for the current job') from exc
        dtype = torch.bfloat16 if device.startswith('cuda') else torch.float32
        self.processor = AutoProcessor.from_pretrained(str(self.model_dir), local_files_only=True)
        self.model = GlmOcrForConditionalGeneration.from_pretrained(
            str(self.model_dir), local_files_only=True, dtype=dtype,
            attn_implementation=attn_implementation).to(device).eval()

    def recognize(self, image):
        started = time.monotonic()
        messages = [dict(role='user', content=[dict(type='image', image=image.convert('RGB')),
                                               dict(type='text', text=self.prompt)])]
        inputs = self.processor.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                                     return_dict=True, return_tensors='pt').to(self.device)
        input_tokens = int(inputs['input_ids'].shape[1])
        with self.torch.inference_mode():
            output = self.model.generate(**inputs, do_sample=False, max_new_tokens=self.max_new_tokens)
        generated = output[0, input_tokens:]
        generated_count = int(generated.shape[0])
        text = self.processor.decode(generated, skip_special_tokens=True)
        eos_ids = self.model.generation_config.eos_token_id
        eos_ids = [eos_ids] if isinstance(eos_ids, int) else eos_ids or []
        last_token = int(generated[-1]) if generated_count else None
        truncated = generated_count >= self.max_new_tokens and last_token not in eos_ids
        del output, generated, inputs
        # Keep model tensors; make temporary page allocations available to the next image.
        if self.device.startswith('cuda'):
            self.torch.cuda.empty_cache()
        return dict(raw_text=text, input_tokens=input_tokens, generated_tokens=generated_count,
                    elapsed_seconds=time.monotonic() - started, truncated=truncated,
                    task_prompt=self.prompt, polygon_page=None,
                    coordinate_note='GLM native text output has no word-level geometry; full-page source only')


def crop_region(image, region, expected_size=None):
    """Use only independently measured upright crop bounds, never text/table cell boxes."""
    if expected_size and list(image.size) != list(expected_size):
        raise ValueError(f'Geometry image size differs from rendered image: {expected_size} != {image.size}')
    box = region.get('bbox_upright')
    if not isinstance(box, list) or len(box) != 4 or any(not isinstance(x, (int, float)) for x in box):
        raise ValueError('Region requires measured bbox_upright [left, top, right, bottom]')
    left, top, right, bottom = [int(round(x)) for x in box]
    left, top, right, bottom = max(0, left), max(0, top), min(image.width, right), min(image.height, bottom)
    if right <= left or bottom <= top:
        raise ValueError('Region crop is empty or outside the rendered page')
    return image.crop((left, top, right, bottom)), [left, top, right, bottom]


def validate_geometry_document(manifest, pdf_sha256):
    if not manifest.get('geometry_only'):
        raise ValueError('Geometry manifest must explicitly declare geometry_only=true')
    matches = [document for document in manifest.get('documents', [])
               if document.get('pdf_sha256') == pdf_sha256]
    if len(matches) != 1:
        raise ValueError('Geometry manifest must contain exactly one document with matching source PDF SHA256')
    # Do not accidentally let future manifests pass saved OCR values to this model comparison.
    forbidden = {'text', 'raw_text', 'blocks', 'fields', 'extracted_fields', 'values', 'value'}

    def inspect(value):
        if isinstance(value, dict):
            if forbidden.intersection(value):
                raise ValueError('Geometry manifest contains saved OCR text or fields')
            for item in value.values():
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
    inspect(matches[0])
    return matches[0]
