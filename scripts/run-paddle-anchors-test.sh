#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

# Reuse this checkout's local model/runtime configuration when available.
# AutoDL environments instead rely on the active Python and .autodl-runtime.json.
if [[ -f "$REPO_ROOT/runs/local/env.sh" ]]; then
  # shellcheck source=/dev/null
  source "$REPO_ROOT/runs/local/env.sh"
  exec "$OCR_PYTHON" -u "$REPO_ROOT/autodl_test_paddle_anchors.py" \
    --glm-python "$MATERIAL_OCR_GLM_PYTHON" \
    --model-dir "$MATERIAL_OCR_GLM_MODEL_DIR" \
    --model-cache "$PADDLE_PDX_CACHE_HOME" "$@"
fi

exec "${OCR_PYTHON:-python}" -u "$REPO_ROOT/autodl_test_paddle_anchors.py" "$@"
