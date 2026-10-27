"""Auditable field-specific acceptance, not OCR score inflation or correction."""
import json
import math
from decimal import Decimal

from material_ocr.upstream_legacy.paths import rebar_config
from material_ocr.upstream_legacy.materials.rebar.entry_schema import parse_specification, quantity_header


def load_policy():
    policy = json.loads(rebar_config('entry_confidence.json').read_text(encoding='utf-8'))
    for key in ('general_min_score', 'quantity_header_min_score', 'specification_min_score',
                'min_text_coverage', 'min_assignment_margin'):
        value = policy[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
            raise ValueError(f'Invalid entry confidence policy: {key}')
    if any(policy[k] > policy['general_min_score'] for k in
           ('quantity_header_min_score', 'specification_min_score')):
        raise ValueError('Field thresholds cannot exceed the general threshold')
    return policy


def field_confidence_decision(cell, role, profile, general_min_score, policy):
    """Return nonblocking acceptance evidence, or None (retain original issues).

    The caller must already have a unique header and valid row/column geometry.
    Explicit nondefault --min-score keeps its original global semantics.
    """
    if profile not in policy['profiles'] or not math.isclose(
            general_min_score, policy['general_min_score'], rel_tol=0, abs_tol=1e-9):
        return None
    if not cell or not cell['issues'] or any(not i.startswith('low_ocr_score:') for i in cell['issues']):
        return None
    # A multiline or multi-token cell requires ordinary confidence handling;
    # never average a weak identifier character away using other OCR tokens.
    if len(cell['tokens']) != 1:
        return None
    token = cell['tokens'][0]
    if (token['match_status'] != 'assigned' or token['assigned_cell_id'] != cell['cell_id']
            or token['best_text_coverage'] < policy['min_text_coverage']
            or token['best_text_coverage']-token['second_text_coverage'] < policy['min_assignment_margin']):
        return None
    if role == 'quantity_header':
        label, unit = quantity_header(cell['raw_text'])
        if not label or not unit:
            return None
        parsed = {'label': label, 'unit': unit}
        semantic_check = 'exact_quantity_header_with_explicit_unit'
    elif role == 'specification':
        parsed = parse_specification(cell['raw_text'])
        if not parsed or not parsed['grade'] or Decimal(parsed['diameter_mm']) <= 0:
            return None
        semantic_check = 'full_grade_and_positive_diameter_with_symbol_or_mm'
    else:
        return None
    threshold = policy[role+'_min_score']
    score = token['ocr_score']
    if not math.isfinite(score) or not threshold <= score < general_min_score:
        return None
    return {'policy_id': policy['policy_id'], 'decision': 'accepted_by_field_rule',
            'profile': profile, 'role': role, 'ocr_index': token['ocr_index'],
            'original_score': score, 'general_min_score': general_min_score,
            'effective_min_score': threshold, 'resolved_issues': list(cell['issues']),
            'checks': ['single_ocr_token', 'unique_cell_assignment', semantic_check],
            'text_coverage': token['best_text_coverage'],
            'assignment_margin': token['best_text_coverage']-token['second_text_coverage'],
            'parsed': parsed, 'raw_ocr_unchanged': True}
