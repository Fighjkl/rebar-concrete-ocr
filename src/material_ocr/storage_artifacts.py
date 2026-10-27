"""Keep OCR evidence; evict only explicitly registered, reproducible debug PNGs.

The source PDF/full-page PNG and OCR JSON are never candidates.  A small
``.artifact.json`` beside every derivative retains its transform and checksums.
``materialize_artifact(path)`` restores an evicted derivative on demand.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from collections import OrderedDict


def _sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


_SOURCE_HASHES = {}


def _source_sha(path):
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _SOURCE_HASHES:
        _SOURCE_HASHES[key] = _sha(path)
    return _SOURCE_HASHES[key]


def register_derivative(path, source, operations, *, kind, pdf_page=None, dpi=None):
    """Register an already-written diagnostic image without changing OCR input.

    Operations are ordered ``rotate`` (CCW), ``crop`` [x0,y0,x1,y1], and
    OpenCV ``resize`` (scale or explicit size and interpolation).  Source paths
    are relative to the derivative so an entire OCR volume remains portable.
    """
    path, source = Path(path).resolve(), Path(source).resolve()
    if not path.is_file() or not source.is_file() or path == source:
        raise ValueError('A diagnostic derivative requires a distinct existing source')
    if path.suffix.lower() != '.png':
        raise ValueError('Only diagnostic PNGs can be registered')
    value = dict(schema_version=1, kind=kind, source=os.path.relpath(source, path.parent),
                 source_sha256=_source_sha(source), sha256=_sha(path),
                 byte_size=path.stat().st_size, operations=operations)
    if pdf_page is not None:
        value.update(pdf_page=int(pdf_page), dpi=int(dpi))
    _write_json(Path(str(path) + '.artifact.json'), value)


def _reconstruct_bytes(path, value, *, allowed_root=None, visited=None, cache=None):
    source = (path.parent / value['source']).resolve()
    if allowed_root is not None and not source.is_relative_to(Path(allowed_root).resolve()):
        raise ValueError('Diagnostic source is outside the allowed volume')
    if not source.is_file():
        materialize_artifact(source, allowed_root=allowed_root, _visited=visited)
    if _source_sha(source) != value['source_sha256']:
        raise ValueError(f'Diagnostic source checksum changed: {source}')
    import cv2
    import numpy as np
    cache_key = (str(source), value.get('pdf_page'), value.get('dpi'))
    image = cache.get(cache_key) if cache is not None else None
    if image is None:
        if value.get('pdf_page') is not None:
            import pypdfium2 as pdfium
            from material_ocr.extraction.batch import pdf_image
            from material_ocr.pdf_runtime import PDF_RENDER_LOCK
            with PDF_RENDER_LOCK:
                pdf = pdfium.PdfDocument(str(source))
                try:
                    image = pdf_image(pdf, int(value['pdf_page']) - 1, int(value['dpi']))
                finally:
                    pdf.close()
        else:
            image = cv2.imdecode(np.fromfile(str(source), dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f'Cannot read diagnostic source: {source}')
        if cache is not None:
            cache[cache_key] = image
            if len(cache) > 2:
                cache.popitem(last=False)
    for operation in value['operations']:
        if 'rotate' in operation:
            rotation = int(operation['rotate'])
            if rotation not in (0, 90, 180, 270):
                raise ValueError('Invalid diagnostic rotation')
            image = np.ascontiguousarray(np.rot90(image, rotation // 90))
        elif 'crop' in operation:
            x0, y0, x1, y1 = operation['crop']
            image = image[y0:y1, x0:x1]
        elif 'resize' in operation:
            resize = operation['resize']
            interpolation = getattr(cv2, resize.get('interpolation', 'INTER_LINEAR'))
            if 'size' in resize:
                image = cv2.resize(image, tuple(resize['size']), interpolation=interpolation)
            else:
                image = cv2.resize(image, None, fx=resize['scale'], fy=resize['scale'],
                                   interpolation=interpolation)
        else:
            raise ValueError('Unknown diagnostic transform')
    ok, encoded = cv2.imencode('.png', image)
    if not ok or hashlib.sha256(encoded.tobytes()).hexdigest() != value['sha256']:
        raise ValueError(f'Diagnostic reconstruction checksum mismatch: {path}')
    return encoded


def materialize_artifact(path, *, allowed_root=None, _visited=None):
    """Return a present image, or reconstruct and verify its registered recipe.

    No OCR runs here. Existing files are returned unchanged. A stale source or
    incompatible image renderer fails without publishing a replacement.
    """
    path = Path(path)
    resolved = path.resolve()
    if allowed_root is not None and not resolved.is_relative_to(Path(allowed_root).resolve()):
        raise ValueError('Diagnostic target is outside the allowed volume')
    visited = set(_visited or ())
    if resolved in visited:
        raise ValueError('Cyclic diagnostic source recipe')
    visited.add(resolved)
    if path.is_file():
        return path
    recipe = Path(str(path) + '.artifact.json')
    if not recipe.is_file():
        raise FileNotFoundError(path)
    value = json.loads(recipe.read_text(encoding='utf-8'))
    encoded = _reconstruct_bytes(path, value, allowed_root=allowed_root, visited=visited)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.artifact-tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded.tobytes())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def compact_artifacts(output, *, policy='compact'):
    """Evict registered diagnostics in this successful output; never old runs.

    Callers decide success, and must not call this on a failed/interrupted run.
    Unknown images, broken sources, symlinked trees and business JSON are kept.
    ``full`` retains all files and still writes an observable policy report.
    """
    if policy not in ('compact', 'full'):
        raise ValueError('artifact policy must be compact or full')
    root = Path(output).resolve()
    report = dict(schema_version=1, policy=policy, removed_count=0, reclaimed_bytes=0,
                  retained_count=0, errors=[], artifacts=[])
    cache = OrderedDict()
    for recipe in sorted(root.rglob('*.png.artifact.json')):
        path = Path(str(recipe)[:-len('.artifact.json')])
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            continue
        try:
            value = json.loads(recipe.read_text(encoding='utf-8'))
            source = (path.parent / value['source']).resolve()
            # Every original source must still exist. Do not compact chained
            # derivatives or leave a recipe that depends on an evicted image.
            if not source.is_file() or Path(str(source) + '.artifact.json').exists():
                report['retained_count'] += int(path.is_file())
                continue
            if not path.is_file():
                continue
            if _source_sha(source) != value['source_sha256'] or _sha(path) != value['sha256']:
                raise ValueError('source or diagnostic checksum changed')
            size = path.stat().st_size
            item = dict(path=str(path.relative_to(root)), kind=value['kind'], byte_size=size,
                        recipe=str(recipe.relative_to(root)), status='retained')
            if policy == 'compact':
                # Confirm the actual installed decoder/renderer reproduces
                # the original bytes before evicting anything.
                _reconstruct_bytes(path, value, cache=cache)
                path.unlink()
                report['removed_count'] += 1
                report['reclaimed_bytes'] += size
                item['status'] = 'evicted_reproducible_diagnostic'
            else:
                report['retained_count'] += 1
            report['artifacts'].append(item)
        except Exception as exc:
            report['retained_count'] += int(path.is_file())
            report['errors'].append(dict(path=str(path.relative_to(root)), error=str(exc)))
    _write_json(root / 'storage_manifest.json', report)
    return report


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restore', type=Path, required=True,
                        help='Restore one diagnostic PNG from its .artifact.json recipe')
    arguments = parser.parse_args()
    print(materialize_artifact(arguments.restore))
