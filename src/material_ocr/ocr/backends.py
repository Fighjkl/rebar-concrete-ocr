"""Local OCR backends; importing the catalog never loads inference libraries.

All backends implement recognize(PIL.Image) -> {raw_text, truncated, ...}.
Only OCR/runtime selection belongs here; business rules remain in production.
"""
from dataclasses import dataclass
from importlib import import_module
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OCR_MODEL = "local-glm"


@dataclass(frozen=True)
class OcrBackend:
    model_id: str
    label: str
    cache_namespace: str
    engine_module: str
    engine_name: str
    model_directory: str
    model_environment: str
    python_environment: str
    python_candidates: tuple[str, ...] = ()

    def model_dir(self, explicit=None, *, paddle_cache=None):
        configured = explicit or os.environ.get(self.model_environment)
        if configured:
            return Path(configured).resolve()
        if self.model_id == "local-paddle" and paddle_cache:
            return (Path(paddle_cache) / "official_models").resolve()
        return (PROJECT_ROOT / self.model_directory).resolve()

    def python(self):
        explicit = os.environ.get(self.python_environment)
        if self.model_id == "local-glm":
            explicit = explicit or os.environ.get("GLM_OCR_PYTHON")
        if explicit:
            # Resolving a venv symlink invokes the base interpreter and loses
            # that environment's site-packages. Preserve the configured path.
            return Path(explicit).expanduser().absolute()
        for candidate in self.python_candidates:
            path = PROJECT_ROOT / candidate
            if path.is_file():
                return path
        return Path(sys.executable)

    def engine_class(self):
        return getattr(import_module(self.engine_module), self.engine_name)

    def fingerprint(self, directory):
        return import_module(self.engine_module).model_fingerprint(directory)


BACKENDS = (
    OcrBackend("local-glm", "本地 GLM-OCR", "glm", "material_ocr.ocr.glm", "GlmOcrEngine",
               "models/GLM-OCR", "MATERIAL_OCR_GLM_MODEL_DIR", "MATERIAL_OCR_GLM_PYTHON",
               (".venv-glm/bin/python", "venv-glm/bin/python")),
    OcrBackend("local-paddle", "本地 Paddle OCR（PP-OCRv5）", "paddle", "material_ocr.ocr.paddle_native",
               "PaddleOcrEngine", "models/paddlex_cache/official_models",
               "MATERIAL_OCR_PADDLE_MODEL_DIR", "MATERIAL_OCR_PADDLE_PYTHON"),
)


def model_ids():
    return tuple(backend.model_id for backend in BACKENDS)


def get_backend(model_id=None):
    selected = (model_id or "").strip() or DEFAULT_OCR_MODEL
    for backend in BACKENDS:
        if backend.model_id == selected:
            return backend
    raise ValueError("不支持的本地OCR模型：" + selected)


def model_catalog():
    return dict(default_model=DEFAULT_OCR_MODEL,
                models=[dict(id=b.model_id, label=b.label) for b in BACKENDS],
                shared_processing=True)
