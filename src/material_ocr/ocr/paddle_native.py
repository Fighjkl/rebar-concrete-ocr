"""Offline PP-OCRv5 transcription for the current production OCR contract.

Only transcription lives here. Table geometry, field parsing and matching stay
in their existing modules. Raw boxes are retained in crop-image coordinates;
they are never represented as original PDF page coordinates.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import time

from .glm import sha256

MODEL_NAMES = ("PP-OCRv5_server_det", "PP-OCRv5_server_rec")
REQUIRED_FILES = ("inference.json", "inference.pdiparams", "inference.yml")
ADAPTER_VERSION = "paddle-native-v1"


@lru_cache(maxsize=4)
def _fingerprint(directory, signature):
    root = Path(directory)
    hashes = {name: sha256(root / name) for name, _, _ in signature}
    payload = dict(adapter=ADAPTER_VERSION, files=hashes)
    return dict(directory=str(root), files=hashes,
                fingerprint=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())


def model_fingerprint(directory):
    root = Path(directory).resolve()
    names = [str(Path(model) / name) for model in MODEL_NAMES for name in REQUIRED_FILES]
    try:
        signature = tuple((name, (root / name).stat().st_size,
                           (root / name).stat().st_mtime_ns) for name in names)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"本地 Paddle OCR 模型文件缺失，禁止自动下载：{exc.filename}") from exc
    return dict(_fingerprint(str(root), signature))


def reading_text(blocks):
    """Read rows top-to-bottom and left-to-right, without inferring fields."""
    rows = []
    def bounds(block):
        polygon = block["polygon_ocr"]
        xs, ys = zip(*polygon)
        return min(xs), min(ys), max(xs), max(ys)
    for block in sorted(blocks, key=lambda b: (bounds(b)[1], bounds(b)[0])):
        x1, y1, x2, y2 = bounds(block)
        center = (y1 + y2) / 2
        height = max(1, y2 - y1)
        candidates = [row for row in rows if abs(row["center"] - center) <= .45 * min(row["height"], height)]
        if candidates:
            row = min(candidates, key=lambda row: abs(row["center"] - center))
            row["blocks"].append(block)
        else:
            rows.append(dict(center=center, height=height, blocks=[block]))
    return "\n".join(" ".join(str(b["text"]) for b in sorted(row["blocks"], key=lambda b: bounds(b)[0]))
                     for row in sorted(rows, key=lambda row: row["center"]))


class PaddleOcrEngine:
    def __init__(self, model_dir, *, device="cpu", prompt="Text Recognition:", **unused):
        model_fingerprint(model_dir)  # Fail before Paddle can select/download defaults.
        root = Path(model_dir).resolve()
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(root.parent))
        from paddleocr import PaddleOCR
        paddle_device = device.replace("cuda", "gpu", 1) if device.startswith("cuda") else device
        self.device = paddle_device
        self.prompt = prompt
        self.engine = PaddleOCR(
            text_detection_model_name=MODEL_NAMES[0], text_detection_model_dir=str(root / MODEL_NAMES[0]),
            text_recognition_model_name=MODEL_NAMES[1], text_recognition_model_dir=str(root / MODEL_NAMES[1]),
            device=paddle_device, enable_mkldnn=False, use_doc_orientation_classify=False,
            use_doc_unwarping=False, use_textline_orientation=False)

    def recognize(self, image):
        import numpy as np
        started = time.monotonic()
        blocks = []
        for item in self.engine.predict(np.asarray(image.convert("RGB"))[:, :, ::-1].copy()):
            data = getattr(item, "json", item)
            if callable(data):
                data = data()
            if isinstance(data, str):
                data = json.loads(data)
            data = data.get("res", data)
            texts = data.get("rec_texts", [])
            # rec_polys correspond to filtered rec_texts; dt_polys may include
            # rejected detections and must never be zipped against rec_texts.
            polygons = data.get("rec_polys", [])
            scores = data.get("rec_scores", [])
            for index, text in enumerate(texts):
                if index >= len(polygons):
                    raise ValueError("Paddle OCR result has no matching recognition polygon")
                polygon = [[float(x), float(y)] for x, y in polygons[index]]
                blocks.append(dict(text=str(text), score=float(scores[index]) if index < len(scores) else None,
                                   polygon_ocr=polygon, coordinate_system="ocr_input_image_pixels_top_left"))
        return dict(raw_text=reading_text(blocks), ocr_blocks=blocks,
                    inference_model="local-paddle", truncated=False,
                    elapsed_seconds=time.monotonic() - started, task_prompt=self.prompt,
                    polygon_page=None, coordinate_note="Raw Paddle boxes use this OCR input image; page/ROI geometry is stored separately")
