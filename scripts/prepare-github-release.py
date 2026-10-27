#!/usr/bin/env python3
"""Copy a minimal runnable source tree without Git history or private OCR data."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    '.gitignore', '.gitattributes', 'README.md', 'pyproject.toml',
    'requirements.txt', 'requirements-glm.txt',
    'autodl_test_paddle_anchors.py', 'autodl_test_upstream.py',
    'prepare_autodl.py',
)
FIXED_FILES = (
    'tools/__init__.py', 'tools/compare_ocr_ledgers.py',
    'scripts/run-paddle-anchors-test.sh', 'scripts/prepare-github-release.py',
    'docs/DEPLOYMENT.md', 'docs/MATERIAL_ROUTING.md', 'docs/MINIMAL_RELEASE.md',
    '前端/.gitignore', '前端/README.md', '前端/bin/start-local.sh',
    '前端/java/business/pom.xml', '前端/sql/init.sql',
    '前端/vue/.nvmrc', '前端/vue/index.html', '前端/vue/package.json',
    '前端/vue/package-lock.json', '前端/vue/vite.config.js',
)
TREES = {
    'src/material_ocr': {'.py', '.json', '.html', '.js', '.css'},
    'no_entry_ocr': {'.py'},
    '前端/java/business/src/main': {'.java', '.yml', '.yaml', '.xml', '.properties'},
    '前端/vue/src': {'.js', '.vue', '.css'},
}


def selected_files():
    names = set(ROOT_FILES + FIXED_FILES)
    for directory, suffixes in TREES.items():
        for path in (ROOT / directory).rglob('*'):
            if '__pycache__' in path.parts or not path.is_file():
                continue
            if path.name.startswith('test_') or path.suffix not in suffixes:
                continue
            names.add(path.relative_to(ROOT).as_posix())
    for name in sorted(names):
        source = ROOT / name
        if source.is_symlink() or not source.is_file():
            raise ValueError(f'Required source is missing or is a symlink: {name}')
        yield name, source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dest', type=Path, required=True,
                        help='New, nonexistent directory for the source snapshot')
    args = parser.parse_args()
    dest = args.dest.expanduser().resolve()
    if dest.exists():
        parser.error('Destination already exists; choose a new path. Nothing is overwritten.')
    sources = list(selected_files())
    for _, source in sources:
        if dest == source or dest in source.parents:
            parser.error('Destination must not contain the source project.')
    dest.mkdir(parents=True)
    manifest = {}
    for name, source in sources:
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        manifest[name] = {
            'bytes': target.stat().st_size,
            'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
        }
    try:
        revision = subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True,
            stderr=subprocess.DEVNULL).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        revision = None
    report = {
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'source_base_commit': revision,
        'source': 'Current working files, including uncommitted runtime code',
        'file_count': len(manifest),
        'total_bytes': sum(item['bytes'] for item in manifest.values()),
        'excluded': [
            'Git history and remote configuration',
            'PDFs, business spreadsheets, slides and scanned images',
            'OCR text, checkpoints, ledgers, reports and databases',
            'Model weights, model caches, virtualenvs and dependency caches',
            'Test fixtures, development tests, experiments and local settings',
            'Logs, build outputs, credentials and editor/agent settings',
        ],
        'files': manifest,
    }
    (dest / 'SOURCE_MANIFEST.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'directory': str(dest), 'files': len(manifest),
                      'bytes': report['total_bytes']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
