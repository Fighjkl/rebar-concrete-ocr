"""Web entry for the same mixed pipeline as autodl_test_paddle_anchors --mode full."""
from pathlib import Path

from material_ocr.ocr.backends import get_backend
from material_ocr.materials.rebar import upstream_pipeline
from material_ocr.materials.rebar.glm_production import get_glm_cache_identity, PROJECT_ROOT
from material_ocr.workflow import code_fingerprint, configure_environment


MODEL_ID = 'rebar-hybrid-v3'
LABEL = '页面分类 → 有进场表沿原流程 / 无进场表按铭牌索引'
MAX_TOKENS = 16384


def catalog():
    return dict(default_model=MODEL_ID, models=[dict(id=MODEL_ID, label=LABEL)],
                shared_processing=True)


def validate_model(model):
    if model and model != MODEL_ID:
        raise ValueError('识别流程已更新，请刷新页面后使用“GLM 进场表 + Paddle 附件”')
    return MODEL_ID


def cache_identity(model_cache):
    glm = get_backend('local-glm')
    paddle = get_backend('local-paddle')
    paddle_root = paddle.model_dir(paddle_cache=model_cache)
    glm_identity = get_glm_cache_identity(glm.model_dir())
    paddle_identity = paddle.fingerprint(paddle_root)
    orientation = paddle_root / 'PP-LCNet_x1_0_doc_ori'
    orientation_hashes = {name: upstream_pipeline.file_hash(orientation / name)
                          for name in ('inference.json', 'inference.pdiparams', 'inference.yml')}
    if not glm.python().is_file():
        raise FileNotFoundError(f'GLM Python 不存在：{glm.python()}')
    # A distinct ID and both model fingerprints prevent reuse of old whole-volume GLM ledgers.
    return dict(model_id=MODEL_ID, entry_model=glm_identity['model_fingerprint'],
                paddle_model=paddle_identity['fingerprint'], orientation_model=orientation_hashes,
                rules_fingerprint=code_fingerprint(),
                matcher_sha256=upstream_pipeline.file_hash(PROJECT_ROOT / 'tools/compare_ocr_ledgers.py'),
                no_entry_branch='no-entry-nameplate-v1',
                no_entry_code={str(p.relative_to(PROJECT_ROOT)): upstream_pipeline.file_hash(p)
                    for p in sorted((PROJECT_ROOT / 'no_entry_ocr').glob('*.py'))},
                mode='full', scan_dpi=180, entry_dpi=300, attachment_dpi=300,
                prompt='Table Recognition:', max_new_tokens=MAX_TOKENS,
                attachment_rotations=[0], attachment_crops=False, glm_attachment_calls=0)


def run(pdf, output, *, model_cache, cache_dir, device, progress, force_ocr_refresh=False):
    configure_environment(model_cache)
    glm = get_backend('local-glm')
    paddle = get_backend('local-paddle')

    def report(stage, current, total, message):
        if stage == 'upstream':
            stage = 'starting'
            message = '准备页面分类；GLM 仅用于进场表，报审表和附件使用 Paddle'
        elif stage == 'recover':
            stage = 'entry_review'
        elif stage == 'extract':
            message = f'报审表 Paddle / 进场表 GLM 提取 {current}/{total}'
        if progress:
            progress(stage, current, total, message)

    return upstream_pipeline.run_pipeline(
        Path(pdf), Path(output), mode='full', model_dir=glm.model_dir(),
        glm_python=glm.python(), device=device, cache_dir=Path(cache_dir),
        max_tokens=MAX_TOKENS, force_attachments=force_ocr_refresh,
        paddle_model_dir=paddle.model_dir(paddle_cache=model_cache),
        attachment_rotations=(0,), progress=report, allow_no_entry=True)
