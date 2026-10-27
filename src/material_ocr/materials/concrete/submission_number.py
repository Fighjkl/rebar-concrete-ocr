"""Concrete-only recovery of handwritten numbers on submission form headers."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata

from PIL import Image


def is_complete_document_no(value):
    """Reject the short OCR prefixes commonly produced by handwritten IDs."""
    value = unicodedata.normalize('NFKC', str(value or '')).strip().upper()
    value = re.sub(r'\s*([-./])\s*', r'\1', value)
    return bool(re.fullmatch(r'[A-Z0-9]{1,16}(?:[-./][A-Z0-9]{1,16})+', value)
                or (len(value) >= 6 and value.isalnum() and any(ch.isdigit() for ch in value)))


def _number_candidates(text):
    value = unicodedata.normalize('NFKC', str(text or '')).upper()
    value = re.sub(r'\s*([-./])\s*', r'\1', value)
    pattern = re.compile(r'(?<![A-Z0-9])([A-Z0-9]{1,16}(?:[-./][A-Z0-9]{1,16})+|[A-Z]{1,8}\d{3,16})(?![A-Z0-9])')
    return list(dict.fromkeys(match.group(1) for match in pattern.finditer(value)
                              if is_complete_document_no(match.group(1))))


def _header_line(header_json):
    data = json.loads(header_json.read_text(encoding='utf-8'))
    boxes = data.get('rec_boxes') or []
    matches = []
    for index, text in enumerate(data.get('rec_texts') or []):
        if '编号' in str(text) and index < len(boxes):
            matches.append((str(text), boxes[index]))
    return data, matches


@lru_cache(maxsize=4)
def _paddle_engine(model_dir, device):
    from material_ocr.ocr.paddle_native import PaddleOcrEngine
    return PaddleOcrEngine(model_dir, device=device)


def recover_submission_document_no(folder, page, *, model_dir=None, device='cpu', engine=None):
    """Read a high-resolution ROI around the OCR-detected 编号 row.

    `folder` is the saved upstream directory. Coordinates are mapped from the
    top-cropped header render into a page render using the common DPI scale.
    Multiple OCR views are retained as candidates; an existing OCR prefix can
    disambiguate them, while all machine-read handwritten values stay reviewable.
    """
    page_no = page.get('page_no')
    header_relative = page.get('header_image')
    if not header_relative:
        return None
    root = Path(folder)
    header_path = root / header_relative
    header_json = header_path.with_suffix('.json')
    page_path = root / 'pages' / f'page_{int(page_no):03d}' / 'page.png'
    if not (header_path.is_file() and header_json.is_file() and page_path.is_file()):
        return None

    _, matches = _header_line(header_json)
    if len(matches) != 1:
        return None
    original_line, raw_box = matches[0]
    header_image = Image.open(header_path).convert('RGB')
    source_page = Image.open(page_path).convert('RGB')
    rotation = int(page.get('rotation_ccw') or 0) % 360
    rotated_page = source_page.rotate(rotation, expand=True) if rotation else source_page
    scale = rotated_page.width / header_image.width
    x1, y1, x2, y2 = [float(item) * scale for item in raw_box]
    line_height = max(1.0, y2 - y1)
    pad_y = max(24, round(line_height * 0.8))
    pad_left = max(30, round(line_height * 0.7))
    extend_right = max(300, round(line_height * 8))
    crop_box = (max(0, round(x1 - pad_left)), max(0, round(y1 - pad_y)),
                min(rotated_page.width, round(x2 + extend_right)),
                min(rotated_page.height, round(y2 + pad_y)))
    crop = rotated_page.crop(crop_box)
    if engine is None:
        if not model_dir:
            return None
        engine = _paddle_engine(str(Path(model_dir).resolve()), str(device))

    views = [('native', crop),
             ('upscaled_2x', crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.LANCZOS))]
    candidates = []
    for name, image in views:
        output = engine.recognize(image)
        texts = [output.get('raw_text', '')]
        texts.extend(block.get('text', '') for block in output.get('ocr_blocks', []))
        score_by_value = {}
        for text in texts:
            for value in _number_candidates(text):
                scores = [float(block['score']) for block in output.get('ocr_blocks', [])
                          if block.get('score') is not None and value in str(block.get('text', '')).upper()]
                score_by_value[value] = max(score_by_value.get(value, 0.0), max(scores, default=0.0))
        candidates.extend({'value': value, 'variant': name, 'score': score}
                          for value, score in score_by_value.items())

    distinct = list(dict.fromkeys(item['value'] for item in candidates))
    if not distinct:
        return None

    # The whole-page/header recognizer supplies a useful prefix, even when it
    # missed the suffix. Prefer the sole ROI candidate consistent with it.
    prefix_candidates = _number_candidates(original_line)
    prefix = ''
    if not prefix_candidates:
        prefix_match = re.search(r'(?:文件)?编号\s*[:：]?\s*([A-Z0-9]{1,8})',
                                 unicodedata.normalize('NFKC', original_line), re.I)
        prefix = prefix_match.group(1).upper() if prefix_match else ''
    compatible = [value for value in distinct if prefix and value.startswith(prefix)]
    if len(compatible) == 1:
        selected = compatible[0]
        selected_by = 'existing_header_prefix'
    else:
        enlarged = [item for item in candidates if item['variant'] == 'upscaled_2x']
        selected = enlarged[-1]['value'] if enlarged else candidates[-1]['value']
        selected_by = 'upscaled_roi_candidate'

    return {
        'value': selected,
        'raw_text': '局部 OCR 候选：' + ' / '.join(distinct) + (f'；页眉 OCR 前缀：{prefix}' if prefix else ''),
        'source_role': 'submission',
        'precision': 'page_roi_ocr',
        'pixel_coordinates_available': False,
        'roi_box': list(crop_box),
        'roi_coordinate_system': 'rotated_page_pixels_top_left',
        'rotation_ccw': rotation,
        'source_pixel_size': list(source_page.size),
        'ocr_candidates': candidates,
        'selected_by': selected_by,
        'issues': ['document_no_visual_review_required'],
        'header_ocr_text': original_line,
    }
