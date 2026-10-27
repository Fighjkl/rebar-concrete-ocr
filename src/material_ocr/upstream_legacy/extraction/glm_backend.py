"""Isolated GLM inference; no Paddle table model or fixed entry-table template."""
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from material_ocr.upstream_legacy.extraction.adaptive import image_read, image_write
from material_ocr.upstream_legacy.ocr.orientation import rotate_page
from material_ocr.upstream_legacy.extraction.glm_table import analyze_html
from material_ocr.upstream_legacy.processes import stop_process


def save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def settings():
    python = os.environ.get('MATERIAL_OCR_GLM_PYTHON', sys.executable)
    python = shutil.which(python) or python
    if not Path(python).is_file():
        raise ValueError('MATERIAL_OCR_GLM_PYTHON does not point to a Python executable: ' + python)
    model = Path(os.environ.get('MATERIAL_OCR_GLM_MODEL', 'models/GLM-OCR')).resolve()
    if not (model/'config.json').is_file():
        raise ValueError('Set MATERIAL_OCR_GLM_MODEL to your downloaded GLM-OCR model directory: ' + str(model))
    config = json.loads((model/'config.json').read_text(encoding='utf-8'))
    if config.get('model_type') != 'glm_ocr':
        raise ValueError('Configured model is not GLM-OCR')
    tokens = int(os.environ.get('MATERIAL_OCR_GLM_MAX_TOKENS', '16384'))
    timeout = int(os.environ.get('MATERIAL_OCR_GLM_TIMEOUT', '1800'))
    if not 256 <= tokens <= 32768 or timeout < 30:
        raise ValueError('Invalid GLM token limit (256..32768) or timeout (>=30 seconds)')
    return dict(python=str(Path(python).expanduser().absolute()), model=str(model), max_new_tokens=tokens, timeout=timeout)


def prepare_page(folder, rotation):
    folder = Path(folder)
    out = folder/'glm'
    out.mkdir(exist_ok=False)
    original = image_read(folder/'page.png')
    image, transform = rotate_page(original, rotation)
    image_write(out/'input.png', image)
    meta = dict(coarse_rotation_ccw=rotation, original_to_input=transform.tolist(),
        original_size=[original.shape[1],original.shape[0]], input_size=[image.shape[1],image.shape[0]],
        input_image_sha256=hashlib.sha256((out/'input.png').read_bytes()).hexdigest(),
        method='existing_page_direction_only', fixed_template_used=False, cell_pixel_coordinates_available=False,
        note='Full 300 DPI page; no grid detection, fixed crop, binarization or forced row count.')
    save(out/'input_meta.json', meta)
    return meta


def write_review(out, result):
    cells = result['logical_cells']
    rows = []
    for number in sorted({c['grid']['row_start'] for c in cells}):
        row = sorted((c for c in cells if c['grid']['row_start'] == number), key=lambda c:c['grid']['col_start'])
        rows.append('<tr>' + ''.join(f'<td rowspan="{c["rowspan"]}" colspan="{c["colspan"]}">{html.escape(c["raw_text"]).replace(chr(10), "<br>")}</td>' for c in row) + '</tr>')
    doc = ('<!doctype html><meta charset="utf-8"><title>GLM-OCR 进场表核对</title>'
        '<style>body{font:16px sans-serif;margin:20px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}img{width:100%}table{border-collapse:collapse}td{border:1px solid #777;padding:5px}pre{white-space:pre-wrap}</style>'
        '<h1>GLM-OCR 进场表核对</h1><p>左侧为模型输入原页，右侧为识别出的表格。行列为 HTML 逻辑结构，不代表像素坐标；所有结果仍需人工核对。</p>'
        '<a href="raw.txt">原始模型输出</a> · <a href="records.json">结构化记录</a>'
        '<div class="pair"><div><a href="input.png"><img src="input.png"></a></div><div><table>' + ''.join(rows) + '</table></div></div>'
        '<pre>' + html.escape(json.dumps(result['summary'],ensure_ascii=False,indent=2)) + '</pre>')
    (out/'review.html').write_text(doc, encoding='utf-8')


class GlmBackend:
    def __init__(self, device):
        self.config = settings()
        self.device = 'cuda:' + device.split(':',1)[1] if device.startswith('gpu:') else device
        if not self.device.startswith('cuda:'):
            raise ValueError('Use --device gpu:0 with the GLM backend')

    def extract(self, folder, page_no, pdf_sha, calibration, form_type='inspection_record'):
        out = Path(folder)/'glm'
        image = out/'input.png'
        if hashlib.sha256(image.read_bytes()).hexdigest() != calibration['input_image_sha256']:
            raise ValueError('GLM input image changed after preparation')
        command = [self.config['python'], '-u', str(Path(__file__).with_name('_glm_runner.py')),
            '--image', str(image.resolve()), '--out', str(out.resolve()), '--model', self.config['model'],
            '--device', self.device, '--max-new-tokens', str(self.config['max_new_tokens'])]
        save(out/'invocation.json', dict(command=command, timeout_seconds=self.config['timeout']))
        env = dict(os.environ, PYTHONUTF8='1', PYTHONUNBUFFERED='1')
        env.pop('PYTHONPATH', None)
        # A child per page isolates Torch from Paddle and releases its GPU memory before attachments.
        with (out/'inference.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env,
                                       start_new_session=os.name == 'posix')
            try:
                code = process.wait(timeout=self.config['timeout'])
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError('GLM inference timed out; see glm/inference.log') from exc
            finally:
                if process.poll() is None:
                    stop_process(process, process_group=os.name == 'posix')
        if code:
            raise RuntimeError(f'GLM inference exited {code}; see {out / "inference.log"}')
        info = json.loads((out/'inference.json').read_text(encoding='utf-8'))
        if info['input_image_sha256'] != calibration['input_image_sha256']:
            raise ValueError('GLM inference input hash mismatch')
        if info['hit_token_limit']:
            raise ValueError('GLM output reached token limit; raw.txt saved. Increase MATERIAL_OCR_GLM_MAX_TOKENS and rerun to a new directory.')
        raw = (out/'raw.txt').read_text(encoding='utf-8')
        try:
            result = analyze_html(raw, form_type=form_type or 'inspection_record', page_no=page_no,
                                  pdf_sha=pdf_sha, input_size=calibration['input_size'])
        except ValueError as exc:
            save(out/'parse_error.json', {'error':str(exc),'raw_output':'raw.txt'})
            raise
        result['provenance'] = dict(pdf_sha256=pdf_sha, page_no=page_no, input_meta=calibration,
            inference=info, raw_output_sha256=hashlib.sha256(raw.encode('utf-8')).hexdigest())
        save(out/'records.json', result)
        save(out/'summary.json', result['summary'])
        write_review(out, result)
        return result
