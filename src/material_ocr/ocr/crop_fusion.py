"""Run unchanged region OCR on a selected device and fuse scoped evidence.

The previous cu126 build produced incorrect elementary GPU arithmetic on
SM120. Validate the backend before OCR; empty detections alone cannot diagnose
crop quality. No device-specific thresholds or image retries are applied.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_gpu_backend(device: str) -> dict:
    """Reject a backend that transfers tensors but computes wrong values."""
    import numpy as np
    import paddle
    paddle.set_device(device)
    x = np.arange(1, 5, dtype='float32').reshape(2, 2)
    a = paddle.to_tensor(x)
    actual = {'transfer': a.numpy(), 'addition': (a + 1).numpy(),
              'matmul': paddle.matmul(a, a).numpy(),
              'ones': paddle.ones([2, 2], dtype='float32').numpy()}
    expected = {'transfer': x, 'addition': x + 1, 'matmul': x @ x,
                'ones': np.ones_like(x)}
    failures = [k for k in actual if not np.allclose(actual[k], expected[k])]
    report = {'paddle_version': paddle.__version__, 'device': device,
              'values': {k: v.tolist() for k, v in actual.items()}, 'failures': failures}
    if failures:
        raise RuntimeError('Untrustworthy GPU arithmetic: ' + json.dumps(report))
    return report


def run_gpu_crops(crops: dict[str, str], out_dir: str | Path, *, device: str = 'gpu:0',
                  provenance: dict | None = None, model_cache: str | Path | None = None) -> dict:
    """Use the shared OCR factory, preserving raw crop pixels and polygons."""
    import os
    import cv2
    if model_cache:
        os.environ['PADDLE_PDX_CACHE_HOME'] = str(Path(model_cache).resolve())
        os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
    from material_ocr.ocr.attachments import _paddle_ocr
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    health = validate_gpu_backend(device) if device.startswith('gpu') else None
    engine = _paddle_ocr(device)
    result = {'device': device, 'provenance': provenance or {},
              'model_cache': str(Path(model_cache).resolve()) if model_cache else None,
              'backend_health': health, 'preprocessing': 'unchanged_original_crops', 'crops': {}}
    for rid, raw in crops.items():
        path = Path(raw).resolve(); attempts = []
        source = cv2.imread(str(path))
        if source is None:
            raise ValueError(f'Unreadable crop: {path}')
        h, w = source.shape[:2]
        start = time.perf_counter()
        try:
            predictions = engine.predict(str(path))
            blocks = []; det_count = 0
            for item in predictions:
                det_count += len(item['dt_polys'])
                # rec_polys follows retained recognition texts; dt_polys can
                # include boxes discarded by the recognition score filter.
                polygons = item.get('rec_polys', item['dt_polys'])
                texts = list(item['rec_texts']); scores = list(item['rec_scores'])
                if len(polygons) != len(texts) or len(scores) != len(texts):
                    raise ValueError('Recognition text/polygon/score alignment failed')
                for text, score, polygon in zip(texts, scores, polygons):
                    blocks.append({'text': text, 'score': float(score),
                                   'polygon_crop': [[float(x), float(y)] for x, y in polygon]})
            attempts.append({'path': str(path), 'attempt_kind': 'baseline',
                             'texts': [b['text'] for b in blocks], 'blocks': blocks,
                             'scores': [b['score'] for b in blocks], 'det_count': det_count,
                             'status': 'completed', 'elapsed_seconds': time.perf_counter()-start})
        except Exception as exc:
            attempts.append({'path': str(path), 'status': 'failed',
                             'error_type': type(exc).__name__, 'error': str(exc),
                             'elapsed_seconds': time.perf_counter()-start})
        result['crops'][rid] = {'source': str(path), 'source_sha256': file_sha256(path),
                               'crop_size': [w, h], 'attempts': attempts,
                               'best': next((a for a in attempts if a.get('blocks')), None),
                               'fallback_required': not any(a.get('blocks') for a in attempts)}
        (out/'gpu_crop_results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _map_crop_blocks(item: dict, region: dict) -> list[dict]:
    """Map actual crop pixels to upright and original page coordinates."""
    width, height = item['crop_size']
    if width <= 0 or height <= 0:
        raise ValueError('Invalid crop dimensions')
    def mapped(poly, point):
        if not poly or len(poly) != 4:
            raise ValueError('Missing four-corner crop geometry')
        u, v = point[0]/width, point[1]/height
        return [poly[0][i] + u*(poly[1][i]-poly[0][i]) +
                v*(poly[3][i]-poly[0][i]) for i in (0, 1)]
    blocks = []
    for block in item['best']['blocks']:
        polygon = block.get('polygon_crop')
        if not polygon or len(polygon) != 4:
            raise ValueError('Missing OCR polygon')
        if any(x < 0 or x > width or y < 0 or y > height for x, y in polygon):
            raise ValueError('OCR polygon outside source crop')
        blocks.append({**block, 'polygon_ocr': [mapped(region.get('crop_polygon_ocr'), p) for p in polygon],
                       'polygon_page': [mapped(region.get('crop_polygon_page'), p) for p in polygon],
                       'extraction_method': 'device_crop_ocr', 'ocr_device': item.get('device'),
                       'template_path': item['source']})
    return blocks


def _focused_observations(item: dict, region: dict, page_no: int, rules: dict | None) -> list[dict]:
    from material_ocr.ocr.attachments import _build_spec_regions
    previous = region.get('field_observations', [])
    evidence = next((o.get('evidence', {}) for o in previous if o.get('evidence')), {})
    if not evidence:
        evidence = next((o.get('evidence', {}) for values in region.get('rejected_fields', {}).values()
                         for o in values if o.get('evidence')), {})
    field = region['priority_field']
    # Mass columns must preserve their header unit; no default ton guessing.
    if field in {'quantity_t', 'weight_t'} and not evidence.get('label'):
        raise ValueError('Missing original mass column label')
    spec = {k: copy.deepcopy(region[k]) for k in ('priority_field', 'entity_key', 'entity_role',
             'row_windows', 'crop_polygon_ocr', 'crop_polygon_page') if k in region}
    spec.update(region_id_suffix=region['region_id'].removeprefix(f'p{page_no:03d}_'),
                document_type=region['document_type'], blocks=_map_crop_blocks(item, region),
                label_text=evidence.get('label'), label_polygon_page=evidence.get('label_polygon_page'))
    observations = _build_spec_regions(spec, page_no=page_no, source_image=region.get('source_image'),
                                      rotation_ccw=region.get('rotation_ccw', 0), rules=rules)[0]['field_observations']
    for observation in observations:
        observation['extraction_method'] = 'device_crop_ocr'
        observation['evidence'].update(ocr_device=item['device'], crop_path=item['source'],
                                       crop_sha256=item['source_sha256'])
    return observations


def rebase_image_references(value: Any, index_path: str | Path) -> Any:
    """Make image evidence portable to another output directory."""
    base = Path(index_path).resolve().parent
    if isinstance(value, list):
        return [rebase_image_references(x, index_path) for x in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {'source_image', 'upright_image', 'image_path', 'template_path', 'crop_path'} and isinstance(item, str):
                path = Path(item)
                if not path.is_absolute():
                    path = (base/path) if (base/path).exists() else (Path.cwd()/path if Path(item).exists() else base/path)
                result[key] = str(path.resolve())
            else:
                result[key] = rebase_image_references(item, index_path)
        return result
    return value


def fuse_region_index(index_path: str | Path, gpu_result_path: str | Path, out_path: str | Path) -> dict:
    """Retain all validated, row-scoped evidence; disagreements require review."""
    from material_ocr.matching.catalog import _field_key
    index_path = Path(index_path)
    index = json.loads(index_path.read_text())
    gpu = json.loads(Path(gpu_result_path).read_text())
    provenance = gpu.get('provenance', {})
    if not index.get('pdf_sha256') or provenance.get('pdf_sha256') != index['pdf_sha256']:
        raise ValueError('PDF provenance mismatch or missing')
    if provenance.get('index_sha256') != file_sha256(index_path):
        raise ValueError('Source index checksum mismatch')
    targets = {r['region_id']: (p, r) for p in index.get('pages', []) for r in p.get('regions', [])
               if r.get('priority_field')}
    unknown = set(gpu.get('crops', {}))-set(targets)
    if unknown:
        raise ValueError(f'Unknown crop regions: {sorted(unknown)}')
    for rid, item in gpu.get('crops', {}).items():
        page, region = targets[rid]
        if not item.get('source_sha256') or file_sha256(item['source']) != item['source_sha256']:
            raise ValueError(f'Crop checksum mismatch: {rid}')
        audit = {'device': gpu.get('device'), 'attempts': item.get('attempts', [])}
        region['gpu_ocr'] = audit
        region['ocr_sources'] = ['refined_region_index']
        if not item.get('best'):
            audit['status'] = 'empty_fallback_to_refined_index'
            region.setdefault('review_reasons', []).append('device_crop_ocr_empty_or_failed')
            continue
        item['device'] = gpu.get('device')
        try:
            observations = _focused_observations(item, region, int(page['page_no']), index.get('rules'))
        except (ValueError, KeyError, TypeError) as exc:
            audit.update(status='invalid_geometry_or_context', error=str(exc))
            region.setdefault('review_reasons', []).append('device_crop_geometry_or_context_invalid')
            continue
        audit['field_observations'] = observations
        previous = region.setdefault('field_observations', [])
        previous.extend(observations)
        region['ocr_sources'].append('device_crop_ocr')
        accepted = [o for o in observations if o.get('validation_status') == 'accepted']
        scopes = {}
        for o in previous:
            if o.get('validation_status') == 'accepted':
                scopes.setdefault((o.get('row_id'), o['field']), set()).add(_field_key(o['field'], o))
        conflicts = [dict(row_id=k[0], field=k[1], values=sorted(v)) for k, v in scopes.items() if len(v)>1]
        audit['conflicts'] = conflicts
        rejected = any(o.get('validation_status') == 'rejected' for o in observations)
        audit['status'] = ('conflict_requires_review' if conflicts else
                           'invalid_or_missing_field_requires_review' if rejected or not accepted else
                           'validated_evidence_fused')
        if audit['status'] != 'validated_evidence_fused':
            region.setdefault('review_reasons', []).append(audit['status'])
    index['gpu_crop_fusion'] = {'device': gpu.get('device'), 'source': str(Path(gpu_result_path).resolve()),
                              'provenance': provenance, 'backend_health': gpu.get('backend_health'),
                              'policy': 'same_parser_same_entity_same_row_conflicts_require_review'}
    index = rebase_image_references(index, index_path)
    out = Path(out_path); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(index, ensure_ascii=False, indent=2))
    return index


def crop_map_from_index(index: dict[str, Any], pages_root: str | Path) -> dict[str, str]:
    root = Path(pages_root); result = {}
    for page in index.get('pages', []):
        for region in page.get('regions', []):
            if not region.get('priority_field'):
                continue
            rid = str(region['region_id']); prefix = f"p{int(page['page_no']):03d}_"
            path = root/f"page_{int(page['page_no']):03d}_upright_{rid.removeprefix(prefix)}.png"
            if not path.is_file():
                from material_ocr.storage_artifacts import materialize_artifact
                try:
                    materialize_artifact(path)
                except FileNotFoundError:
                    raise FileNotFoundError(f'Missing priority crop: {rid}: {path}') from None
            result[rid] = str(path.resolve())
    if not result:
        raise ValueError('No priority crops in index')
    return result


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', required=True)
    parser.add_argument('--pages-root', required=True)
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--device', default='gpu:0')
    parser.add_argument('--fused-index', required=True)
    parser.add_argument('--model-cache', default='models/paddlex_cache')
    args = parser.parse_args(argv)
    index = json.loads(Path(args.index).read_text())
    crops = crop_map_from_index(index, args.pages_root)
    gpu = run_gpu_crops(crops, args.out_dir, device=args.device, model_cache=args.model_cache,
                        provenance={'pdf_sha256': index['pdf_sha256'], 'index_sha256': file_sha256(args.index)})
    fused = fuse_region_index(args.index, Path(args.out_dir)/'gpu_crop_results.json', args.fused_index)
    summary = {'crop_count': len(crops), 'nonempty': sum(bool(x['best']) for x in gpu['crops'].values()),
               'fallback': sum(x['fallback_required'] for x in gpu['crops'].values()),
               'statuses': {r['region_id']: r['gpu_ocr']['status'] for p in fused['pages']
                            for r in p['regions'] if r.get('gpu_ocr')}, 'fused_index': args.fused_index}
    (Path(args.out_dir)/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
