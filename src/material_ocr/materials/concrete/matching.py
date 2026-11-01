"""Explainable concrete form classification and link resolution."""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation

from .entry_duplicates import deduplicate_certificate_records
from .extraction import ALIASES, entry_date, inspection_location_from_remarks, inspection_manufacturer_alias, issue_text, normalized, parse_inspection_html, specification, submission_date, use_location_text, validate_fields, value_text, volume
from .submission_number import is_complete_document_no

FORM_LABELS = {
    'quality_certificate': '质量证明书', 'delivery_note': '送货单',
    'submission': '报审表单', 'inspection_record': '进场检验记录', 'mix_design': '配比报告',
}
_FORM_TERMS = {
    'quality_certificate': ('质量证明书', '质保书'),
    'delivery_note': ('送货单', '发货单', '小票'),
    'submission': ('工程材料报审表', '材料报审表', '报审表'),
    'inspection_record': ('进场检验记录', '进场验收记录', '进场检验', '混凝土材料进货清单'),
    'mix_design': ('配合比报告', '配合比设计报告', '配比报告', '混凝土配合比'),
}
EXTRA_LABELS = {
    'ticket_no': ('小票号', '小票编号', '送货单号', '发货单号'),
    'cumulative_delivery': ('累计发货量', '累计方量', '累计供应量'),
    'task_no': ('生产任务单编号', '生产任务编号', '任务单号'),
    'report_no': ('报告编号',),
}
SUBMISSION_LEDGER_FIELDS = ('material_name', 'supply_date')
SUBMISSION_EXTRACTED_FIELDS = (*SUBMISSION_LEDGER_FIELDS, 'use_location')
DOCUMENT_NO_CONFIDENCE_THRESHOLD = 0.95


def _is_cumulative_delivery_label(text):
    """Accept an exact known heading or one unique OCR character substitution."""
    candidate = normalized(text).strip(':：')
    aliases = {normalized(alias) for alias in EXTRA_LABELS['cumulative_delivery']}
    if candidate in aliases:
        return True
    # Keep typo tolerance narrow: only same-length, one-character substitutions
    # against known cumulative-delivery headings are eligible. Insertions,
    # deletions and ambiguous near-matches remain unclassified.
    near = {alias for alias in aliases if len(alias) == len(candidate)
            and sum(left != right for left, right in zip(alias, candidate)) == 1}
    return len(near) == 1


def classify_form(text: str) -> str | None:
    # A test report may mention certificates, grades or producers in its body.
    # Only the five business form families supply concrete matching fields.
    heading_lines = [normalized(line) for line in str(text or '').splitlines()[:8]]
    if any('检测报告' in line or ('试验报告' in line and '专用章' not in line) for line in heading_lines):
        return None
    text = normalized(text)
    for kind in ('mix_design', 'inspection_record', 'delivery_note', 'quality_certificate', 'submission'):
        if any(term in text for term in _FORM_TERMS[kind]):
            return kind
    return None


def _label_value(text, labels):
    # Stop at another label: a blank field must not consume its neighbour.
    all_labels = sorted({a for values in {**ALIASES, **EXTRA_LABELS}.values() for a in values}, key=len, reverse=True)
    for label in labels:
        match = re.search(re.escape(label) + r'\s*[:：]?\s*([^\s，,；;。]{1,100})', text)
        if match:
            value = match[1].strip('：:。;；')
            if any(value.startswith(other) for other in all_labels):
                continue
            return value_text(value)
    return ''


def _fields(text):
    result = {key: _label_value(text, aliases) for key, aliases in {**ALIASES, **EXTRA_LABELS}.items()}
    # These are separate source identifiers, never relabel report/task as warranty.
    task = re.search(r'(?:生产任务单编号|生产任务编号|任务单号)\s*[:：]?\s*(?:第|号)?\s*([A-Za-z0-9][A-Za-z0-9()-]*)', text)
    result['task_no'] = task[1] if task else ''
    strength = re.search(r'(?:混凝土强度等级|砼强度等级|强度等级(?:及名称)?|规格品种|规格型号|规格)\s*[:：]?\s*((?:自密实)?C\d{2,3}(?:P\d{1,2})?(?:自密实)?)', text, re.I)
    if strength:
        spec = strength[1]
        # Impermeability is sometimes a separate labelled value in the same form.
        impermeability = re.search(r'抗渗\s*[:：]?\s*(P\d{1,2})', text, re.I)
        if impermeability and 'P' not in spec.upper():
            spec = spec.replace('自密实', '') + impermeability[1] + ('自密实' if '自密实' in spec else '')
        result['specification'] = specification(spec) or spec
    result['entry_date'] = entry_date(result['entry_date']) if result['entry_date'] else ''
    date_match = re.search(r'(?:供货日期|供应日期|发货日期|送货日期|发货时间)\s*[:：]?\s*(\d{4}[年./-]\d{1,2}[月./-]\d{1,2})', text)
    result['supply_date'] = entry_date(date_match[1]) if date_match else ''
    return result


def _submission_document_no(text):
    """Recover the B.0.6-style standalone 编号 when crop alignment failed.

    This fallback is deliberately limited to a whole OCR line on a submission
    form. Other form families keep treating bare 编号 as an unrelated field.
    """
    values = []
    for line in str(text or '').splitlines():
        compact = normalized(line).strip('：:')
        match = re.fullmatch(r'(?:文件)?编号[:：]?([A-Za-z0-9][A-Za-z0-9./·•\-]*)', compact)
        if match:
            value = match[1].replace('－', '-').replace('—', '-').replace('–', '-')
            if is_complete_document_no(value):
                values.append(value)
    unique = list(dict.fromkeys(values))
    return unique[0] if len(unique) == 1 else ''


def _document_no_token(value):
    """Return a normalized identifier from a labelled OCR value.

    The template permits short numeric values such as ``001``.  This helper
    is intentionally more permissive than ``is_complete_document_no`` (which
    decides whether a value is sufficient to trigger a high-resolution ROI
    retry), because a template field is still useful evidence when it is
    short.
    """
    compact = normalized(value).strip('：:')
    match = re.fullmatch(r'(?:文件)?编号[:：]?([A-Za-z0-9][A-Za-z0-9./·•\-]*)', compact)
    if match:
        compact = match[1]
    compact = compact.replace('－', '-').replace('—', '-').replace('–', '-')
    return compact if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9./·•\-]*', compact) else ''


def _field_min_score(field):
    """Get the lowest score available in a structured OCR field."""
    if not isinstance(field, dict):
        return None
    try:
        if field.get('min_score') is not None:
            return float(field['min_score'])
    except (TypeError, ValueError):
        pass
    scores = []
    for block in field.get('blocks') or []:
        try:
            scores.append(float(block['score']))
        except (KeyError, TypeError, ValueError):
            continue
    return min(scores) if scores else None


def _document_no_review_issues(source, *, candidate_conflict=False):
    """Apply one confidence policy to all submission-number evidence paths."""
    issues = list(source.get('issues') or [])
    score = _field_min_score(source)
    if score is None:
        try:
            score = float(source['score'])
        except (KeyError, TypeError, ValueError):
            score = None
    if score is not None:
        source['min_score'] = score
        if score < DOCUMENT_NO_CONFIDENCE_THRESHOLD:
            if 'low_ocr_confidence' not in issues:
                issues.append('low_ocr_confidence')
        else:
            # The old upstream parser added this generic issue to every
            # document number, including clear printed values.  Keep it only
            # when there is an actual candidate ambiguity to review.
            issues = [code for code in issues
                      if code not in {'low_ocr_confidence', 'document_no_visual_review_required'}]
    elif not candidate_conflict and 'document_no_visual_review_required' not in issues:
        # A page-text fallback has no score or crop geometry.  Keep the
        # uncertainty visible instead of silently treating it as confirmed.
        issues.append('document_no_visual_review_required')
    if candidate_conflict and 'document_no_visual_review_required' not in issues:
        issues.append('document_no_visual_review_required')
    source['issues'] = list(dict.fromkeys(issues))
    return source


def _submission_document_no_field(page, form, page_no):
    """Choose one addressable number source and normalize its review issues."""
    structured = (page.get('submission_fields') or {}).get('document_no', {})
    if not isinstance(structured, dict):
        structured = {'value': structured}
    template_value = _document_no_token(structured.get('value') or structured.get('raw_text'))

    header_candidates = []
    for block in page.get('submission_header_blocks') or []:
        raw = normalized(block.get('text', ''))
        match = re.search(r'(?:文件)?编号[:：]?\s*([A-Za-z0-9][A-Za-z0-9./·•\-]*)', raw)
        if not match:
            continue
        value = _document_no_token(match[1])
        if not value:
            continue
        candidate = dict(block, value=value, raw_text=raw, source_role='submission',
                         source_field='document_no', page_no=page_no,
                         precision='header_ocr_blocks', pixel_coordinates_available=False,
                         header_polygon=block.get('polygon'),
                         header_coordinate_system='rotated_header_pixels_top_left')
        candidate.pop('polygon', None)
        if candidate.get('score') is None:
            candidate.pop('score', None)
        header_candidates.append(candidate)

    values = []
    if template_value:
        values.append(template_value)
    values.extend(candidate['value'] for candidate in header_candidates)
    unique_values = list(dict.fromkeys(values))
    conflict = len(unique_values) > 1

    if template_value:
        source = dict(structured)
        source.update(page_no=page_no, source_role='submission', source_field='document_no')
        source['value'] = template_value
        source.setdefault('raw_text', structured.get('raw_text') or '编号：' + template_value)
        source.setdefault('precision', 'template_crop')
        source.setdefault('pixel_coordinates_available', bool(source.get('polygon_page')))
    elif header_candidates:
        # Prefer the candidate that agrees with the already parsed page text;
        # otherwise retain all candidates in evidence and require review.
        parsed_value = _document_no_token(form.get('document_no'))
        source = next((candidate for candidate in header_candidates if candidate['value'] == parsed_value),
                      header_candidates[0])
        source = dict(source)
        source['header_ocr_candidates'] = [dict(candidate) for candidate in header_candidates]
    else:
        value = _document_no_token(form.get('document_no'))
        if not value:
            return form.get('document_no', ''), None
        source = {'page_no': page_no, 'raw_text': value, 'value': value,
                  'source_role': 'submission', 'source_field': 'document_no',
                  'precision': 'page', 'pixel_coordinates_available': False}

    source = _document_no_review_issues(source, candidate_conflict=conflict)
    if conflict:
        source['document_no_candidates'] = unique_values
    return source.get('value', form.get('document_no', '')), source


def _submission_header_location(page):
    """Join only the blocks between the B.0.6 location's left/right anchors."""
    blocks = []
    for block in page.get('submission_header_blocks', []):
        points = block.get('polygon') or []
        if not points:
            continue
        box = (min(p[0] for p in points), min(p[1] for p in points),
               max(p[0] for p in points), max(p[1] for p in points))
        blocks.append((block, box, normalized(block.get('text', ''))))
    starts = [item for item in blocks if re.search(r'拟用于(?:工程)?$', item[2])]
    ends = [item for item in blocks if item[2] in {'部位', '部位的'}]
    if len(starts) != 1 or len(ends) != 1:
        return '', []
    _, start, _ = starts[0]
    _, end, _ = ends[0]
    height = max(1, start[3] - start[1], end[3] - end[1])
    center_y = (start[1] + start[3]) / 2
    if start[2] >= end[0] or abs(center_y - (end[1] + end[3]) / 2) > height:
        return '', []
    pieces = [(box[1], box[0], text, block) for block, box, text in blocks
              if text and box[0] >= start[2] - 2 and box[2] <= end[0] + 2
              and abs((box[1] + box[3]) / 2 - center_y) <= 1.6 * height]
    pieces.sort(key=lambda item: (item[0], item[1]))
    return ''.join(item[2] for item in pieces), [item[3] for item in pieces]


def _submission_ledger_fields(page, form):
    """Read approval fields from template crops or this submission's B.0.6 body."""
    text = normalized(form['text'])
    # The range is optional: ordinary single-day approval dates must remain
    # valid, while B.0.6 forms may state an abbreviated interval such as
    # ``2026年5月22日至23日``.
    date_token = (r'\d{4}[年./-]\d{1,2}[月./-]\d{1,2}日?'
                  r'(?:(?:至|到|—|–)(?:\d{4}[年./-]\d{1,2}[月./-])?\d{1,2}日?)?')
    next_label = '|'.join(sorted({alias for aliases in ALIASES.values() for alias in aliases
                                  if alias != '名称'} | {'附件', '我方', '经我方'}, key=len, reverse=True))
    patterns = {
        # Bare 名称 also occurs in 工程名称, so it is not a material label here.
        'material_name': (r'(?:材料名称|材料品名)[:：](.{1,80}?)(?=' + next_label + r'|[，,；;。]|$)',
                          r'部位的[_]*(.{1,80}?)(?:经)?我方(?:检验|检查|自检)'),
        'supply_date': (r'(?:供货日期|供应日期|发货日期|送货日期|进场日期)[:：]?(' + date_token + r')',
                        r'于[_]*(' + date_token + r')[_]*进场'),
        'use_location': (
            r'(?:使用部位|工程部位|浇筑部位|浇注部位|施工部位|部位)[:：](.{1,240}?)(?=' + next_label + r'|$)',
            r'进场的拟用于工程[_]*(.{1,240}?)_*部位的'),
    }
    template_fields = page.get('submission_fields') or {}
    for key in SUBMISSION_EXTRACTED_FIELDS:
        field = template_fields.get(key) or {}
        if not isinstance(field, dict):
            field = {'value': field}
        value = value_text(field.get('value'))
        if key == 'supply_date':
            value = submission_date(value)
        source = dict(field, page_no=form['page_no'], source_role='submission', source_field=key)
        source['issues'] = [code for code in field.get('issues', [])
                            if code != 'material_not_confirmed_as_rebar']
        source.setdefault('source_pixel_size', page.get('submission_pixel_size'))
        source.setdefault('precision', 'page')
        source.setdefault('pixel_coordinates_available', bool(source.get('polygon_page')))
        candidates = {}
        if not value or key == 'supply_date':
            for pattern in patterns[key]:
                for match in re.finditer(pattern, text):
                    candidate = value_text(match[1].strip('_'))
                    if key == 'supply_date':
                        candidate = submission_date(candidate)
                    if candidate:
                        candidates.setdefault(candidate, match[0])
        range_candidates = {candidate: raw for candidate, raw in candidates.items()
                            if key == 'supply_date' and '至' in candidate}
        template_date = submission_date(value) if key == 'supply_date' else ''
        template_start = template_date.split('至', 1)[0] if template_date else ''
        if (range_candidates and len(range_candidates) == 1
                and (not value or template_date in range_candidates
                     or any(candidate.startswith(template_start) for candidate in range_candidates))):
            # A template crop may recognize only the first endpoint.  The
            # approval sentence preserves the full delivery interval.
            value, raw = next(iter(range_candidates.items()))
            source = {'page_no': form['page_no'], 'source_role': 'submission',
                      'source_field': key, 'raw_text': raw, 'precision': 'page',
                      'pixel_coordinates_available': False,
                      'mapping_rule': 'concrete_submission_body_' + key}
        elif not value and len(candidates) == 1:
            value, raw = next(iter(candidates.items()))
            source = {'page_no': form['page_no'], 'source_role': 'submission',
                      'source_field': key, 'raw_text': raw, 'precision': 'page',
                      'pixel_coordinates_available': False,
                      'mapping_rule': 'concrete_submission_body_' + key}
        elif not value and candidates:
            source['issues'].append('submission_field_multiple_candidates')
        if key == 'use_location' and len(candidates) <= 1:
            location, blocks = _submission_header_location(page)
            if location and len(normalized(location)) >= len(normalized(value)):
                value = location
                source = {'page_no': form['page_no'], 'source_role': 'submission',
                          'source_field': key, 'raw_text': value, 'precision': 'header_ocr_blocks',
                          'pixel_coordinates_available': False,
                          'header_blocks': blocks,
                          'header_coordinate_system': 'rotated_header_pixels_top_left',
                          'mapping_rule': 'concrete_submission_header_location_between_anchors'}
        source.setdefault('raw_text', '')
        form[key] = value
        form['field_evidence'][key] = source


def _mix_design_specification(page, text, fallback=''):
    """Read a mix report's strength grade from its strength-grade row.

    OCR may order the engineering-location fragment before the strength value
    even though both share one visual row. Prefer selected-rotation OCR blocks
    bounded by the next field heading; if geometry is unavailable, inspect only
    the text between 强度等级 and the next form-field heading.
    """
    segment = ''
    selected = page.get('orientation', {}).get('selected_rotation_ccw', page.get('rotation_ccw', 0))
    passes = [item for item in page.get('ocr_passes', [])
              if item.get('rotation_ccw') == selected and not item.get('detail_retry')]
    blocks = page.get('blocks') or (passes[0].get('blocks', []) if passes else [])

    def bounds(block):
        points = block.get('polygon') or []
        if not points:
            return None
        return (min(point[0] for point in points), min(point[1] for point in points),
                max(point[0] for point in points), max(point[1] for point in points))

    strength_labels = {'强度等级', '混凝土强度等级', '砼强度等级'}
    next_labels = {'工程部位', '使用部位', '浇筑部位', '浇注部位', '施工部位',
                   '坍落度', '工作性能', '报告日期', '抗折', '抗渗'}
    label_blocks = [(block, bounds(block)) for block in blocks
                    if bounds(block) and normalized(block.get('text', '')).strip(':：') in strength_labels]
    if len(label_blocks) == 1:
        label, (lx1, ly1, lx2, ly2) = label_blocks[0]
        label_height = max(1.0, ly2 - ly1)
        stop_x = min((box[0] for block in blocks if (box := bounds(block))
                      and box[0] > lx2
                      and normalized(block.get('text', '')).strip(':：') in next_labels),
                     default=float('inf'))
        row = []
        for block in blocks:
            if block is label:
                continue
            box = bounds(block)
            if not box:
                continue
            x1, y1, x2, y2 = box
            center_y = (y1 + y2) / 2
            if lx2 < x1 < stop_x and abs(center_y - (ly1 + ly2) / 2) <= 1.6 * label_height:
                value = str(block.get('text', '')).strip()
                if value:
                    row.append((x1, y1, value))
        row.sort(key=lambda item: (item[0], item[1]))
        segment = ''.join(item[2] for item in row)

    if not re.search(r'(?:自密实)?C\d{2,3}', segment, re.I):
        label_match = re.search(r'(?:混凝土强度等级|砼强度等级|强度等级(?:及名称)?|规格品种|规格型号|规格)\s*[:：]?', text, re.I)
        if label_match:
            rest = text[label_match.end():]
            next_field = re.search(r'(?:工程部位|使用部位|浇筑部位|浇注部位|施工部位|坍落度|工作性能|报告日期)', rest)
            segment = rest[:next_field.start()] if next_field else rest[:160]
        else:
            segment = ''

    grade = re.search(r'((?:自密实)?C\d{2,3}(?:\s*[/、-]?\s*P\d{1,2})?(?:自密实)?)', segment, re.I)
    if not grade:
        return fallback
    token = normalized(grade[1]).upper()
    base_match = re.search(r'C(\d{2,3})', token)
    if not base_match:
        return fallback
    impermeability = re.search(r'抗渗\s*[:：]?\s*P\s*(\d{1,2})', segment, re.I)
    p_match = re.search(r'P(\d{1,2})', token, re.I)
    p_level = p_match[1] if p_match else impermeability[1] if impermeability else ''
    is_self_compacting = '自密实' in token
    parsed = 'C' + base_match[1] + ('P' + p_level if p_level else '')
    if is_self_compacting:
        parsed += '自密实'
    return specification(parsed) or fallback


def _specification_key(value):
    """Match strength/impermeability exactly; self-compacting is optional."""
    return specification(value).removesuffix('自密实')


def _mix_design_location(page, fallback=''):
    """Reassemble a split 工程部位 from nearby OCR blocks on a mix report.

    Rotated mix-report OCR can emit the start and end of the location on
    separate blocks on either side of the 工程部位 heading. The selected OCR
    pass preserves polygons, which lets us join that row without borrowing the
    neighboring strength-grade or slump values.
    """
    selected = page.get('orientation', {}).get('selected_rotation_ccw', page.get('rotation_ccw', 0))
    passes = [item for item in page.get('ocr_passes', [])
              if item.get('rotation_ccw') == selected and not item.get('detail_retry')]
    blocks = page.get('blocks') or (passes[0].get('blocks', []) if passes else [])

    def bounds(block):
        points = block.get('polygon') or []
        if not points:
            return None
        return (min(point[0] for point in points), min(point[1] for point in points),
                max(point[0] for point in points), max(point[1] for point in points))

    label_blocks = []
    for block in blocks:
        raw = normalized(block.get('text', '')).strip(':：')
        if raw in {'工程部位', '使用部位', '浇筑部位', '浇注部位', '施工部位'} and bounds(block):
            label_blocks.append((block, bounds(block)))
    if len(label_blocks) != 1:
        return fallback

    label, (lx1, ly1, lx2, ly2) = label_blocks[0]
    label_height = max(1.0, ly2 - ly1)
    # A following field label bounds the location column. This avoids joining
    # 坍落度 and its numeric value, which share the same OCR text row.
    stop_x = min((bounds(block)[0] for block in blocks
                  if bounds(block) and bounds(block)[0] > lx2
                  and normalized(block.get('text', '')).strip(':：') in {
                      '坍落度', '工作性能', '报告日期', '强度等级', '抗折', '抗渗'}),
                 default=float('inf'))
    pieces = []
    for block in blocks:
        if block is label:
            continue
        box = bounds(block)
        if not box:
            continue
        x1, y1, x2, y2 = box
        text = str(block.get('text', '')).strip()
        if not text or x1 < lx2 or x1 >= stop_x:
            continue
        center_y = (y1 + y2) / 2
        label_center_y = (ly1 + ly2) / 2
        if abs(center_y - label_center_y) > 1.6 * label_height:
            continue
        # Location fragments contain a grid/level marker or a structural
        # component word. Other adjacent form values are not eligible.
        if not re.search(r'\d|区|轴|层|墙|板|柱|梁|基础', text):
            continue
        pieces.append((y1, x1, text, block))
    pieces.sort(key=lambda item: (item[0], item[1]))
    reconstructed = ''.join(item[2] for item in pieces)
    if len(normalized(reconstructed)) < len(normalized(fallback)):
        # OCR geometry may be partial; keep the textual parser's complete
        # value unless the geometric row actually contributes more evidence.
        reconstructed = fallback
    return reconstructed or fallback


def _location_key(text):
    value = normalized(use_location_text(text)).upper()
    return value.translate(str.maketrans({'－': '-', '—': '-', '–': '-', '‐': '-', '‑': '-'}))


def _same_location(left, right):
    left_key, right_key = _location_key(left), _location_key(right)
    return bool(left_key and right_key and left_key == right_key)


def _evidence(values, page, role):
    """Page evidence stays usable even when OCR supplies no pixel coordinates."""
    return {key: {'page_no': page, 'raw_text': str(value), 'source_role': role,
                  'precision': 'page', 'pixel_coordinates_available': False}
            for key, value in values.items() if key in {**ALIASES, **EXTRA_LABELS, 'quantity_m3': ()}
            and value and isinstance(value, str)}


def _cumulative_volume_cell(text):
    """Allow one stray terminal stop after a decimal in a located total cell."""
    quantity, _ = volume(text, concrete_delivery=True)
    if quantity:
        return quantity, {}
    match = re.fullmatch(r'([+]?(?:\d+\.\d+|\.\d+))[.。]', normalized(text))
    if match:
        quantity, _ = volume(match[1], concrete_delivery=True)
        if quantity:
            return quantity, {'raw_text': str(text), 'normalized_text': match[1],
                              'normalization_rule': 'single_trailing_stop_after_decimal'}
    return None, {}


def _delivery_volumes(page):
    """Bind cumulative totals to the numeric cell below its own OCR heading."""
    selected = page.get('orientation', {}).get('selected_rotation_ccw', page.get('rotation_ccw', 0))
    passes = [p for p in page.get('ocr_passes', []) if p.get('rotation_ccw') == selected and not p.get('detail_retry')]
    blocks = page.get('blocks') or (passes[0]['blocks'] if passes else [])
    def bounds(block):
        points = block.get('polygon', [])
        return (min(p[0] for p in points), min(p[1] for p in points),
                max(p[0] for p in points), max(p[1] for p in points))
    totals = []
    for block in blocks:
        if not _is_cumulative_delivery_label(block.get('text')) or not block.get('polygon'):
            continue
        x1, y1, x2, y2 = bounds(block)
        candidates = []
        for other in blocks:
            if not other.get('polygon'):
                continue
            ox1, oy1, ox2, oy2 = bounds(other)
            if not (x1 <= (ox1 + ox2) / 2 <= x2
                    and y2 <= (oy1 + oy2) / 2 <= y2 + 3 * (y2 - y1)):
                continue
            q, normalization = _cumulative_volume_cell(other.get('text', ''))
            if q:
                candidates.append((oy1, other, q, normalization))
        candidates.sort(key=lambda item: item[0])
        if candidates and (len(candidates) == 1 or candidates[1][0] - candidates[0][0] > (y2 - y1) / 2):
            _, source, q, normalization = candidates[0]
            totals.append({'y': y1, 'center': ((x1 + x2) / 2, (y1 + y2) / 2),
                           'value': source['text'], 'quantity_m3': q['value'],
                           'evidence': {'header_block': block, 'value_block': source,
                                        'method': 'ocr_column_below_header', **normalization}})
    ticket_blocks = []
    for block in blocks:
        match = re.search(r'(?:小票号|小票编号)\s*[:：]?\s*([A-Za-z0-9-]+)', block.get('text', ''))
        if match and block.get('polygon'):
            ticket_blocks.append((bounds(block)[1], match[1], block))
    # The ticket number is normally printed at the bottom of its own slip.  A
    # vertical region therefore starts immediately after the preceding ticket
    # and ends at the current ticket.  The region prevents the second slip's
    # header from being assigned to the first slip on a page containing two
    # tickets.  If a form places the ticket number above its totals, fall back
    # to the closest ticket by absolute distance.
    ticket_blocks.sort(key=lambda item: item[0])
    for total in totals:
        region_candidates = []
        for index, (ticket_y, ticket, block) in enumerate(ticket_blocks):
            previous_y = ticket_blocks[index - 1][0] if index else float('-inf')
            if previous_y < total['y'] <= ticket_y:
                region_candidates.append((ticket_y, ticket, block))
        candidates = region_candidates or sorted(
            ticket_blocks,
            key=lambda item: math.hypot(
                (bounds(item[2])[0] + bounds(item[2])[2]) / 2 - total['center'][0],
                (bounds(item[2])[1] + bounds(item[2])[3]) / 2 - total['center'][1]))
        if candidates:
            y, ticket, block = candidates[0]
            tx1, ty1, tx2, ty2 = bounds(block)
            distance = math.hypot((tx1 + tx2) / 2 - total['center'][0],
                                  (ty1 + ty2) / 2 - total['center'][1])
            total.update(ticket_no=ticket, ticket_no_y=y, ticket_no_block=block,
                         ticket_binding_distance_px=round(distance, 2),
                         spatial_binding_method='ticket_region_then_nearest_distance')
    return sorted(totals, key=lambda item: item['y'])


def _spatial_ticket_fields(page):
    """Read identifiers from each ticket region and pair split labels by distance.

    OCR block order is not guaranteed.  On the saved 0814 page, the value block
    for the second task number is a few pixels above its label, so flattening
    blocks into lines can make the next nearby ``C35`` block look like the
    identifier.  This reads label/value blocks geometrically and keeps their
    evidence with the physical ticket region.
    """
    selected = page.get('orientation', {}).get('selected_rotation_ccw', page.get('rotation_ccw', 0))
    passes = [p for p in page.get('ocr_passes', [])
              if p.get('rotation_ccw') == selected and not p.get('detail_retry')]
    blocks = page.get('blocks') or (passes[0].get('blocks', []) if passes else [])

    def y_bounds(block):
        points = block.get('polygon') or []
        if not points:
            return None
        return min(point[1] for point in points), max(point[1] for point in points)

    anchors = []
    for block in blocks:
        bounds = y_bounds(block)
        if not bounds:
            continue
        match = re.search(r'(?:小票号|小票编号)\s*[:：]?\s*([A-Za-z0-9-]+)',
                          str(block.get('text', '')))
        if match:
            anchors.append((bounds[0], bounds[1], match[1], block))
    anchors.sort(key=lambda item: item[0])
    if not anchors:
        return {}

    labels = {
        'task_no': ('生产任务单编号', '生产任务编号', '任务单号', '任务编号'),
        'warranty_no': ('质量证明书编号', '质保证书编号', '质量证明书号', '质保书编号', '质保证编号', '质保编号', '质保书号'),
        'report_no': ('报告编号',),
    }
    normalized_labels = {key: {normalized(label) for label in values}
                         for key, values in labels.items()}
    label_patterns = {key: re.compile(
        r'(?:' + '|'.join(re.escape(label) for label in sorted(values, key=len, reverse=True))
        + r')\s*[:：]?\s*(?:第|号)?\s*([A-Za-z0-9][A-Za-z0-9()-]*)')
        for key, values in labels.items()}

    def center(block):
        points = block.get('polygon') or []
        return (sum(point[0] for point in points) / len(points),
                sum(point[1] for point in points) / len(points))

    def size(block):
        x1, y1, x2, y2 = min(point[0] for point in block['polygon']), min(point[1] for point in block['polygon']), \
                          max(point[0] for point in block['polygon']), max(point[1] for point in block['polygon'])
        return x1, y1, x2, y2

    result = {}
    for index, (ticket_y, ticket_bottom, ticket_no, ticket_block) in enumerate(anchors):
        previous_y = anchors[index - 1][0] if index else float('-inf')
        region = []
        for block in blocks:
            bounds = y_bounds(block)
            if bounds and previous_y < bounds[0] <= ticket_bottom:
                region.append(block)
        fields, sources = {}, {}
        label_blocks = []
        value_blocks = []
        for block in region:
            raw = str(block.get('text', ''))
            compact = normalized(raw).strip(':：')
            for key, pattern in label_patterns.items():
                match = pattern.search(raw)
                if match:
                    fields[key] = match[1]
                    sources[key] = (block, block)
                    break
                if compact in normalized_labels[key]:
                    label_blocks.append((key, block))
                    break
            else:
                # A detached number is eligible only when it is long enough to
                # be an identifier.  This intentionally excludes common nearby
                # grade tokens such as C35.
                candidate = re.fullmatch(r'(?:编号|号)?([A-Za-z0-9][A-Za-z0-9()-]{2,})', compact)
                is_concrete_grade = bool(candidate and re.fullmatch(
                    r'(?:C\d{2,3}(?:P\d{1,2})?|P\d{1,2})', candidate[1], re.I))
                if candidate and not is_concrete_grade and not re.search(r'小票号|小票编号', raw):
                    value_blocks.append((candidate[1], block))

        used_values = set()
        for key, label_block in label_blocks:
            lx1, ly1, lx2, ly2 = size(label_block)
            label_center = center(label_block)
            max_distance = max(180.0, 5 * max(lx2 - lx1, ly2 - ly1))
            candidates = []
            for value, value_block in value_blocks:
                if id(value_block) in used_values:
                    continue
                vx1, vy1, vx2, vy2 = size(value_block)
                value_center = center(value_block)
                same_row = vy1 <= ly2 and vy2 >= ly1
                close_following_row = (ly1 - 2 * (ly2 - ly1) <= value_center[1]
                                       <= ly2 + 3 * (ly2 - ly1)
                                       and lx1 - max_distance <= value_center[0] <= lx2 + max_distance)
                distance = math.hypot(value_center[0] - label_center[0],
                                      value_center[1] - label_center[1])
                if distance <= max_distance and (same_row or close_following_row):
                    candidates.append((distance, value, value_block))
            candidates.sort(key=lambda item: item[0])
            if candidates:
                distance, value, value_block = candidates[0]
                fields[key] = value
                sources[key] = (label_block, value_block)
                used_values.add(id(value_block))

        evidence = {}
        for key in ('task_no', 'warranty_no', 'report_no'):
            if fields.get(key):
                label_block, source = sources.get(key, (ticket_block, ticket_block))
                source_center = center(source)
                ticket_points = ticket_block.get('polygon') or []
                ticket_center = center(ticket_block)
                evidence[key] = {
                    'page_no': page.get('page_no', page.get('page')),
                    'raw_text': fields[key], 'source_role': 'delivery_note',
                    'precision': 'ocr_blocks', 'pixel_coordinates_available': True,
                    'polygon_page': source.get('polygon'),
                    'label_polygon_page': label_block.get('polygon'),
                    'ticket_no': ticket_no,
                    'ticket_binding_distance_px': round(math.hypot(
                        ticket_center[0] - source_center[0], ticket_center[1] - source_center[1]), 2),
                    'spatial_binding_method': 'ticket_vertical_region_and_nearest_distance',
                }
        result[ticket_no] = {'ticket_no': ticket_no, **fields,
                             'spatial_field_evidence': evidence,
                             'identifier_spatial_binding_method': 'ticket_vertical_region_and_nearest_distance'}
    return result


def _same_quantity(left, right):
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError, TypeError):
        return False


def _delivery_evidence(ticket, page_no, page, *, value=''):
    evidence = {'page_no': page_no, 'raw_text': str(value), 'source_role': 'delivery_note',
                'precision': 'ocr_blocks', 'pixel_coordinates_available': False}
    if (ticket or {}).get('polygon'):
        evidence['polygon_page'] = ticket['polygon']
        evidence['source_pixel_size'] = page.get('pixel_size') or page.get('original_pixel_size')
        evidence['pixel_coordinates_available'] = True
    return evidence


def _delivery_quantity_evidence(ticket, page_no):
    return {'page_no': page_no, 'raw_text': ticket.get('cumulative_delivery', ''),
            'value': ticket.get('cumulative_delivery_m3', ''), 'source_role': 'delivery_note',
            'precision': 'ocr_blocks', 'ticket_binding_distance_px': ticket.get('ticket_binding_distance_px'),
            'cumulative_delivery_evidence': ticket.get('cumulative_delivery_evidence', {})}


def classify_pages(pages: list[dict]) -> list[dict]:
    """Keep source-addressable forms; inspection rows and delivery tickets stay separate."""
    forms = []
    for page in pages:
        text = str(page.get('raw_text') or page.get('text') or '')
        kind = page.get('form_type') or classify_form(text)
        if kind not in FORM_LABELS:
            continue
        pno = page.get('page_no', page.get('page'))
        form = {'form_type': kind, 'form_name': FORM_LABELS[kind], 'page_no': pno,
                'text': text, **_fields('' if '<table' in text.lower() else text), 'issues': list(page.get('issues', []))}
        submission_number_ocr = page.get('submission_document_no_ocr') if kind == 'submission' else None
        if kind == 'submission':
            # A short template/OCR prefix such as TL is not a complete document
            # number. Prefer the concrete-only high-resolution ROI recovery.
            if not is_complete_document_no(form.get('document_no')):
                form['document_no'] = _submission_document_no(text)
            if (not is_complete_document_no(form.get('document_no')) and submission_number_ocr
                    and is_complete_document_no(submission_number_ocr.get('value'))):
                form['document_no'] = submission_number_ocr['value']
        if kind == 'mix_design':
            form['specification'] = _mix_design_specification(page, text, form.get('specification', ''))
            location = _mix_design_location(page, form.get('use_location', ''))
            if location:
                form['use_location'] = location
        if kind == 'quality_certificate' and not form['warranty_no']:
            # Bare 编号 is a certificate identifier only within this form family.
            number = re.search(r'(?:^|[\s:：])编号\s*[:：]?\s*([A-Za-z0-9-]+)', text)
            form['warranty_no'] = number[1] if number else ''
        form['field_evidence'] = _evidence(form, pno, kind)
        if kind == 'submission':
            _submission_ledger_fields(page, form)
            document_no, document_no_source = _submission_document_no_field(page, form, pno)
            if document_no:
                form['document_no'] = document_no
            if document_no_source:
                form['field_evidence']['document_no'] = document_no_source
        if (submission_number_ocr and form.get('document_no')
                and form['document_no'] == submission_number_ocr.get('value')):
            roi_source = {
                **submission_number_ocr,
                'page_no': pno,
                'source_role': 'submission',
                'mapping_rule': 'concrete_submission_document_number_high_resolution_roi',
            }
            roi_source['issues'] = list(dict.fromkeys(
                list(roi_source.get('issues') or []) + ['document_no_visual_review_required']))
            form['field_evidence']['document_no'] = roi_source
        if kind == 'mix_design' and form.get('use_location'):
            selected = page.get('orientation', {}).get('selected_rotation_ccw', page.get('rotation_ccw', 0))
            passes = [item for item in page.get('ocr_passes', [])
                      if item.get('rotation_ccw') == selected and not item.get('detail_retry')]
            blocks = page.get('blocks') or (passes[0].get('blocks', []) if passes else [])
            location_key = _location_key(form['use_location'])
            location_blocks = [block for block in blocks if block.get('polygon')
                               and _location_key(block.get('text', ''))
                               and _location_key(block.get('text', '')) in location_key]
            form['field_evidence']['use_location'] = {
                'page_no': pno, 'raw_text': form['use_location'], 'source_role': kind,
                'precision': 'ocr_blocks' if location_blocks else 'page',
                'pixel_coordinates_available': bool(location_blocks),
            }
            if location_blocks:
                form['field_evidence']['use_location']['polygon_page'] = [
                    block['polygon'] for block in location_blocks]
                form['field_evidence']['use_location']['source_pixel_size'] = (
                    page.get('pixel_size') or page.get('original_pixel_size'))
        if kind == 'inspection_record':
            if 'inspection_records' in page:
                form['inspection_records'] = []
                for source_row in page['inspection_records']:
                    row = dict(source_row, field_evidence=dict(source_row.get('field_evidence', {})))
                    inspection_manufacturer_alias(row, row['field_evidence'])
                    form['inspection_records'].append(row)
            elif '<table' in text.lower():
                try:
                    parsed = parse_inspection_html(text, pno, inference=page.get('inference'),
                                                   page_title=text.split('<table', 1)[0])
                    form['inspection_records'] = parsed['records']
                    form['issues'].extend(parsed['issues'])
                except ValueError as exc:
                    form['inspection_records'] = []
                    form['issues'].append(str(exc))
            else:
                q, problem = volume(form.get('quantity', ''))
                form['quantity_m3'] = q['value'] if q else ''
                form['issues'].extend(validate_fields(form))
                if problem:
                    form['issues'].append(problem)
        if kind == 'delivery_note':
            matches = list(re.finditer(r'(?:小票号|小票编号)\s*[:：]?\s*([A-Za-z0-9-]+)', text))
            segments, left = [], 0
            for i, match in enumerate(matches):
                right = match.end() if i < len(matches) - 1 else len(text)
                segments.append(text[left:right])
                left = match.end()
            if not segments:
                segments = [text]
            totals = _delivery_volumes(page)
            spatial_fields = _spatial_ticket_fields(page)
            require_spatial_totals = len(segments) > 1 and bool(totals or spatial_fields)
            tickets = []
            for i, segment in enumerate(segments):
                ticket = {'page_no': pno, 'ticket_index': i, **_fields(segment), 'issues': []}
                ticket['field_evidence'] = _evidence(ticket, pno, kind)
                spatial = spatial_fields.get(ticket.get('ticket_no', ''))
                if spatial:
                    # Once a physical slip is located, identifiers must satisfy
                    # its region and label/value distance rather than text order.
                    for key in ('task_no', 'warranty_no', 'report_no'):
                        if spatial.get(key):
                            ticket[key] = spatial[key]
                            ticket['field_evidence'][key] = spatial['spatial_field_evidence'].get(
                                key, ticket['field_evidence'].get(key, {}))
                        else:
                            ticket[key] = ''
                            ticket['field_evidence'].pop(key, None)
                    ticket['identifier_spatial_binding_method'] = spatial['identifier_spatial_binding_method']
                matched = [t for t in totals if t.get('ticket_no') == ticket.get('ticket_no') and ticket.get('ticket_no')]
                single_unambiguous_fallback = (len(segments) == 1 and len(totals) == 1
                                               and not any(t.get('ticket_no') for t in totals))
                if len(matched) == 1 or single_unambiguous_fallback:
                    total = matched[0] if matched else totals[i]
                    ticket.update(cumulative_delivery=total['value'], cumulative_delivery_m3=total['quantity_m3'],
                                  cumulative_delivery_evidence=total['evidence'],
                                  ticket_binding_distance_px=total.get('ticket_binding_distance_px'),
                                  cumulative_spatial_binding_method='nearest_cumulative_header_and_ticket_number')
                    ticket.setdefault('spatial_binding_method', total.get(
                        'spatial_binding_method', 'single_ticket_single_total_fallback'))
                    ticket['field_evidence']['cumulative_delivery'] = _delivery_quantity_evidence(ticket, pno)
                    ticket['field_evidence']['quantity_m3'] = _delivery_quantity_evidence(ticket, pno)
                    ticket['field_evidence']['ticket_no'] = _delivery_evidence(
                        total.get('ticket_no_block'), pno, page, value=ticket.get('ticket_no', ''))
                else:
                    # Failed spatial binding on a multi-slip page must not be
                    # bypassed by the flattened text's unrelated numeric value.
                    if require_spatial_totals or len(matched) > 1:
                        q, problem = None, 'cumulative_delivery_missing_or_ambiguous'
                    else:
                        q, problem = volume(ticket.get('cumulative_delivery', ''), concrete_delivery=True)
                    ticket['cumulative_delivery_m3'] = q['value'] if q else ''
                    if problem:
                        ticket['cumulative_delivery'] = ''
                        ticket['issues'].append('cumulative_delivery_missing_or_ambiguous')
                ticket['quantity_unit'] = 'm3'
                tickets.append(ticket)
            form['tickets'] = tickets
        form['review_reasons'] = [issue_text(code) for code in form['issues']]
        forms.append(form)
    return forms


def _identifiers(form):
    return {str(form.get(key)) for key in ('warranty_no', 'task_no', 'report_no') if form.get(key)}


def _inherit_submission_document_no(row, submission):
    """Fill a missing inspection file number from the preceding submission."""
    if row.get('document_no'):
        # Saved concrete upstream parses may already carry the template number
        # on each row. Apply the same review policy used by the classified
        # submission form before emitting it to the ledger.
        evidence = row.get('field_evidence', {}).get('document_no', {})
        if evidence.get('source_role') == 'submission':
            row['field_evidence']['document_no'] = _document_no_review_issues(
                dict(evidence), candidate_conflict=bool(evidence.get('document_no_candidates')))
        return
    if not (submission or {}).get('document_no'):
        return
    row['document_no'] = submission['document_no']
    source = (submission.get('field_evidence') or {}).get('document_no', {})
    row['field_evidence']['document_no'] = {
        **source,
        'page_no': submission.get('page_no'),
        'raw_text': source.get('raw_text') or submission['document_no'],
        'source_role': 'submission',
        'source_mapping_rule': source.get('mapping_rule'),
        'mapping_rule': 'concrete_submission_document_number_applied_to_inspection',
    }


def _inherit_submission_ledger_fields(row, submission):
    """These ledger columns belong to the current approval, including blanks."""
    row['submission_page'] = (submission or {}).get('page_no')
    for key in SUBMISSION_LEDGER_FIELDS:
        row[key] = (submission or {}).get(key, '')
        row['field_evidence'].pop(key, None)
        if submission:
            row['field_evidence'][key] = dict(submission.get('field_evidence', {}).get(key, {}),
                                            page_no=submission.get('page_no'), source_role='submission')
        # Inspection parsing diagnostics do not describe the submission field.
        row['issues'] = [issue for issue in row.get('issues', [])
                         if issue not in (key + '_not_observed', key + '_cell_missing_or_ambiguous')
                         and not (key == 'material_name' and issue == 'material_name_not_concrete')]
        if key == 'material_name' and row[key] and not re.search(r'混凝土|砼', row[key]):
            row['issues'].append('material_name_not_concrete')


def _inherit_submission_use_location(row, submission):
    """Prefer approval; a different observed inspection location wins with review."""
    evidence = row['field_evidence']
    # Older saved inputs may already contain approval values. They are not a
    # second observation from the inspection and must not create a conflict.
    if evidence.get('use_location', {}).get('source_role') == 'submission':
        row['use_location'] = ''
        evidence.pop('use_location', None)
    inspection_location_from_remarks(row, evidence, row['issues'])
    inspection_value = value_text(row.get('use_location'))
    inspection_source = dict(evidence.get('use_location', {}),
                             page_no=row.get('page_no'), source_role='inspection')
    inspection_source.setdefault('raw_text', inspection_value)
    inspection_value = use_location_text(inspection_value)
    submission_value = value_text((submission or {}).get('use_location'))
    submission_source = dict((submission or {}).get('field_evidence', {}).get('use_location', {}),
                             page_no=(submission or {}).get('page_no'), source_role='submission')
    submission_source.setdefault('raw_text', submission_value)
    submission_value = use_location_text(submission_value)
    row['field_conflicts'] = list(row.get('field_conflicts', []))
    if inspection_value and submission_value and not _same_location(inspection_value, submission_value):
        source_label = '进场表备注' if inspection_source.get('source_field') == 'remarks' else '进场表部位'
        row['field_conflicts'].append({
            'field': 'use_location', 'conflict_type': 'submission_inspection_use_location',
            'reason': (f'报审表（第{submission_source["page_no"]}页）使用部位“{submission_value}”'
                       f'与{source_label}（第{inspection_source["page_no"]}页）“{inspection_value}”不一致；'
                       f'已按{source_label}填写，请人工审核'),
            'values': [submission_value, inspection_value],
            'locations': [dict(submission_source, value=submission_value),
                          dict(inspection_source, value=inspection_value)],
        })
        value, source = inspection_value, inspection_source
    elif submission_value:
        value, source = submission_value, submission_source
        row['issues'] = [issue for issue in row['issues'] if issue not in {
            'use_location_cell_missing_or_ambiguous', 'use_location_from_remarks_check_source'}]
    else:
        value, source = inspection_value, inspection_source if inspection_value else submission_source
    row['use_location'] = value
    if source.get('page_no') is not None:
        evidence['use_location'] = source
    else:
        evidence.pop('use_location', None)
    if value:
        row['issues'] = [issue for issue in row['issues'] if issue != 'use_location_not_observed']


def match_forms(forms: list[dict]) -> dict:
    """Inspection -> mix -> delivery: preserve the existing ordered match rules."""
    inspections = []
    active_submission = None
    for form in sorted(forms, key=lambda item: item.get('page_no') if item.get('page_no') is not None else float('inf')):
        if form['form_type'] == 'submission':
            # A later submission starts a new scope, even if its number is blank.
            active_submission = form
            continue
        if form['form_type'] == 'inspection_record':
            if 'inspection_records' in form:
                for source_row in form['inspection_records']:
                    row = dict(source_row, page_no=source_row.get('page_no', form['page_no']),
                               field_evidence=dict(source_row.get('field_evidence', {})),
                               issues=list(form.get('issues', [])) + list(source_row.get('issues', [])))
                    _inherit_submission_document_no(row, active_submission)
                    _inherit_submission_ledger_fields(row, active_submission)
                    _inherit_submission_use_location(row, active_submission)
                    inspection_manufacturer_alias(row, row['field_evidence'])
                    inspections.append(row)
            else:
                row = dict(form, field_evidence=dict(form.get('field_evidence', {})))
                _inherit_submission_document_no(row, active_submission)
                _inherit_submission_ledger_fields(row, active_submission)
                _inherit_submission_use_location(row, active_submission)
                inspection_manufacturer_alias(row, row['field_evidence'])
                inspections.append(row)
    inspections, deduplication = deduplicate_certificate_records(inspections)
    mixes = [f for f in forms if f['form_type'] == 'mix_design']
    certificates = [f for f in forms if f['form_type'] == 'quality_certificate']
    deliveries = [ticket for f in forms if f['form_type'] == 'delivery_note' for ticket in f.get('tickets', [f])]
    results = []
    for inspection in inspections:
        warranty = inspection.get('warranty_no', '')
        location_match_reason = ''
        if warranty:
            candidates = [f for f in mixes if warranty in _identifiers(f)]
            specification_candidates = list(candidates)
            strategy = 'warranty_no'
            reason = (f'进场检验记录质保书编号“{warranty}”未找到对应配比报告' if not candidates else
                      f'质保书编号“{warranty}”匹配到 {len(candidates)} 份配比报告，需人工选择' if len(candidates) > 1 else '')
        else:
            spec = _specification_key(inspection.get('specification', ''))
            candidates = [f for f in mixes if spec and _specification_key(f.get('specification', '')) == spec]
            specification_candidates = list(candidates)
            strategy = 'specification'
            reason = ('进场检验记录缺少质保书编号和规格，无法检索配比报告' if not spec else
                      f'进场检验记录无质保书编号，规格“{spec}”未找到配比报告' if not candidates else
                      f'规格“{spec}”匹配到 {len(candidates)} 份配比报告，需人工选择' if len(candidates) > 1 else '')
            if len(candidates) > 1:
                entry_location = inspection.get('use_location') or inspection.get('remarks', '')
                location_matches = [candidate for candidate in candidates
                                    if _same_location(entry_location, candidate.get('use_location', ''))]
                if len(location_matches) == 1:
                    candidates = location_matches
                    strategy = 'specification_and_use_location'
                    location_match_reason = (f'规格“{spec}”有 {len(specification_candidates)} 份候选；'
                                             f'工程部位与进场备注一致，唯一匹配第 {candidates[0].get("page_no")} 页配比报告')
                    reason = ''
                elif location_matches:
                    candidates = location_matches
                    reason = (f'规格“{spec}”匹配到 {len(specification_candidates)} 份配比报告，'
                              f'工程部位匹配到 {len(location_matches)} 份，仍需人工选择')
                elif not entry_location:
                    reason = (f'进场记录未提取到备注/工程部位，规格“{spec}”匹配到 '
                              f'{len(specification_candidates)} 份配比报告，保留候选供人工核验')
                else:
                    reason = (f'规格“{spec}”匹配到 {len(specification_candidates)} 份配比报告，'
                              f'进场工程部位“{entry_location}”未与候选报告部位一致，保留候选供人工核验')
        mix = candidates[0] if len(candidates) == 1 else None
        certificate_links = {
            id(candidate): [certificate for certificate in certificates
                            if _identifiers(candidate) & _identifiers(certificate)]
            for candidate in candidates
        }
        linked_certificates = []
        seen_certificates = set()
        for candidate in candidates:
            for certificate in certificate_links[id(candidate)]:
                key = (certificate.get('page_no'), certificate.get('warranty_no', ''))
                if key not in seen_certificates:
                    seen_certificates.add(key)
                    linked_certificates.append(certificate)

        if warranty:
            quality_candidates = [certificate for certificate in certificates
                                  if warranty in _identifiers(certificate)]
        else:
            quality_candidates = linked_certificates
        quality = (quality_candidates[0] if len(quality_candidates) == 1
                   and (warranty or mix) else None)
        mix_warranty = (mix or {}).get('warranty_no', '')
        matched_warranty = warranty or mix_warranty or (
            quality.get('warranty_no', '') if quality else '')

        if warranty:
            identifiers = {warranty}
        elif mix:
            identifiers = _identifiers(mix)
            if matched_warranty:
                identifiers.add(matched_warranty)
            if quality:
                identifiers.update(_identifiers(quality))
        else:
            # Keep all identifiers reachable from ambiguous spec candidates so
            # downstream ticket candidates remain available for human review.
            identifiers = set().union(*(_identifiers(candidate) for candidate in candidates)) if candidates else set()
            for certificate in linked_certificates:
                identifiers.update(_identifiers(certificate))

        delivery_candidates = [f for f in deliveries if identifiers & _identifiers(f)]
        ambiguous_mix_selection = not warranty and len(candidates) > 1
        delivery_match_rule = 'shared_identifier'
        entry_quantity = inspection.get('quantity_m3', '')
        same_quantity = [candidate for candidate in delivery_candidates
                         if candidate.get('cumulative_delivery_m3') and entry_quantity
                         and _same_quantity(candidate['cumulative_delivery_m3'], entry_quantity)]
        if not ambiguous_mix_selection and len(delivery_candidates) > 1 and len(same_quantity) == 1:
            # A shared task/report number can appear on several concrete slips.
            # Prefer the unique slip whose cumulative volume matches this entry row.
            delivery_candidates = same_quantity
            delivery_match_rule = 'unique_cumulative_delivery_quantity'
        if not identifiers:
            delivery_reason = ('进场检验记录和候选配比报告均无质保书编号，且 PDF 未识别到送货单/小票页，'
                               '无法匹配送货单或提取炉批号') if not deliveries else \
                              '进场检验记录和候选配比报告均无质保书编号，无法匹配送货单'
        elif not deliveries:
            delivery_reason = 'PDF 未识别到送货单/小票页，无法匹配送货单或提取小票号作为炉批号'
        elif ambiguous_mix_selection:
            if delivery_candidates:
                delivery_reason = (f'规格匹配到 {len(candidates)} 份配比报告，关联到 '
                                   f'{len(delivery_candidates)} 份送货单/小票候选；需人工确认配比报告后再确定炉批号')
            else:
                delivery_reason = (f'规格匹配到 {len(candidates)} 份配比报告，候选关联编号未找到对应送货单，'
                                   '需人工核验候选配比报告')
        elif not delivery_candidates:
            delivery_reason = f'关联编号“{", ".join(sorted(identifiers))}”未找到对应送货单'
        elif len(delivery_candidates) > 1:
            delivery_reason = f'关联编号匹配到 {len(delivery_candidates)} 份送货单/小票，累计量无法唯一对应，需人工选择'
        else:
            delivery_reason = ''
        delivery = (delivery_candidates[0] if len(delivery_candidates) == 1
                    and not ambiguous_mix_selection else None)
        issues = list(dict.fromkeys(inspection.get('issues', [])))
        if delivery:
            issues.extend(delivery.get('issues', []))
        values = {key: inspection.get(key, '') for key in
                  ('material_name', 'specification', 'producer', 'supplier', 'use_location', 'entry_date', 'quantity_m3', 'document_no', 'supply_date', 'batch_no')}
        evidence = dict(inspection.get('field_evidence', {}))
        inspection_manufacturer_alias(values, evidence)
        if not warranty and matched_warranty and mix_warranty:
            evidence['warranty_no'] = mix.get('field_evidence', {}).get('warranty_no', {})
        elif not warranty and matched_warranty and quality:
            evidence['warranty_no'] = {
                **quality.get('field_evidence', {}).get('warranty_no', {}),
                'mapping_rule': 'concrete_mix_identifier_linked_quality_certificate_number',
                'linked_mix_page': mix.get('page_no'),
            }
        field_conflicts = list(inspection.get('field_conflicts', []))
        if delivery:
            delivery_quantity = delivery.get('cumulative_delivery_m3', '')
            original_quantity = values.get('quantity_m3', '')
            if delivery_quantity:
                if original_quantity and not _same_quantity(original_quantity, delivery_quantity):
                    field_conflicts.append({'field': 'quantity_m3',
                        'reason': '进场数量与送货单累计发货量不一致；按累计发货量填写验收数量，请核对',
                        'values': [original_quantity, delivery_quantity],
                        'locations': [evidence.get('quantity', {}), _delivery_quantity_evidence(delivery, delivery.get('page_no'))]})
                values['quantity_m3'] = delivery_quantity
                evidence['quantity'] = _delivery_quantity_evidence(delivery, delivery.get('page_no'))
            ticket_no = delivery.get('ticket_no', '')
            if ticket_no:
                original_batch = values.get('batch_no', '')
                ticket_evidence = delivery.get('field_evidence', {}).get('ticket_no') or {
                    'page_no': delivery.get('page_no'), 'raw_text': ticket_no,
                    'source_role': 'delivery_note', 'precision': 'page',
                    'pixel_coordinates_available': False}
                if original_batch and original_batch != ticket_no:
                    field_conflicts.append({'field': 'batch_no',
                        'reason': '进场表炉批号与送货单小票号不一致；按小票号填写炉批号，请核对',
                        'values': [original_batch, ticket_no],
                        'locations': [evidence.get('batch_no', {}), ticket_evidence]})
                values['batch_no'] = ticket_no
                evidence['batch_no'] = {**ticket_evidence, 'source_field': 'ticket_no',
                                        'mapping_rule': 'concrete_batch_number_uses_delivery_ticket_no'}
        for source in (mix, delivery, quality):
            if not source:
                continue
            # Ledger locations come only from approval/inspection observations.
            # Attachment locations remain available for mix disambiguation.
            for key in ('producer', 'supplier', 'batch_no'):
                source_evidence = source.get('field_evidence', {}).get(key, {})
                if values.get(key) and source.get(key) and normalized(values[key]) != normalized(source[key]):
                    field_conflicts.append({'field': key, 'reason': '关联表单取值不一致，请核对原页',
                                            'locations': [evidence.get(key, {}), source_evidence]})
                if not values.get(key) and source.get(key):
                    values[key] = source[key]
                    evidence[key] = source_evidence
        for key in ('producer', 'supplier', 'use_location'):
            if values.get(key):
                issues = [issue for issue in issues if issue != key + '_not_observed']
        if values.get('supply_date') or values.get('entry_date'):
            issues = [issue for issue in issues if issue != 'entry_date_not_observed']
        field_review_issues = {key: list(dict.fromkeys(source.get('issues', [])))
                               for key, source in evidence.items() if source.get('issues')}
        results.append({
            'record_id': inspection.get('record_id'), 'inspection_page': inspection.get('page_no'),
            'submission_page': inspection.get('submission_page'),
            'row_no': inspection.get('row_no'),
            'fields': {**values,
                       'quantity_unit': 'm3', 'warranty_no': matched_warranty},
            'field_evidence': evidence, 'issues': list(dict.fromkeys(issues)),
            'field_review_issues': field_review_issues,
            'field_conflicts': field_conflicts,
            'duplicate_source_records': inspection.get('duplicate_source_records', []),
            'certificate_deduplication_diagnostics': inspection.get('certificate_deduplication_diagnostics', []),
            'deduplication_conflicts': inspection.get('deduplication_conflicts', {}),
            'review_reasons': [issue_text(code) for code in dict.fromkeys(issues)],
            'quality_candidates': [{'page_no': f['page_no'], 'warranty_no': f.get('warranty_no')} for f in quality_candidates],
            'status': 'needs_review' if issues or field_review_issues or reason or delivery_reason or field_conflicts
                      or inspection.get('certificate_deduplication_diagnostics') else 'rule_pass_review_required',
            'mix_design': {'page_no': mix.get('page_no')} if mix else None,
            'mix_strategy': strategy,
            'mix_candidates': [
                {**{key: f.get(key, '') for key in ('page_no', 'warranty_no', 'specification', 'task_no', 'report_no', 'use_location')},
                 'quality_certificate_candidates': [
                     {'page_no': certificate.get('page_no'), 'warranty_no': certificate.get('warranty_no', '')}
                     for certificate in certificate_links[id(f)]],
                 'match_reason': location_match_reason if len(candidates) == 1 else ''}
                for f in candidates],
            'specification_candidates': [
                {key: f.get(key, '') for key in ('page_no', 'warranty_no', 'specification', 'task_no', 'report_no', 'use_location')}
                for f in specification_candidates],
            'mix_match_reason': location_match_reason,
            'mix_review_reason': reason,
            'delivery_note': {'page_no': delivery.get('page_no'), 'ticket_index': delivery.get('ticket_index')} if delivery else None,
            'delivery_match_rule': delivery_match_rule if delivery else '',
            'delivery_candidates': [{key: f.get(key, '') for key in
                                     ('page_no', 'ticket_index', 'ticket_no', 'cumulative_delivery', 'cumulative_delivery_m3', 'issues')} for f in delivery_candidates],
            'delivery_review_reason': delivery_reason,
            'delivery_fields': ({key: delivery.get(key, '') for key in
                                ('ticket_no', 'cumulative_delivery', 'cumulative_delivery_m3', 'quantity_unit', 'cumulative_delivery_evidence')} if delivery else {}),
        })
    return {'schema_version': 'concrete-matching-v1', 'forms': forms, 'records': results,
            'certificate_number_deduplication': deduplication,
            'matching_requires_human': not results or any(r['issues'] or r['field_review_issues']
                or r['mix_review_reason'] or r['delivery_review_reason'] or r['field_conflicts'] for r in results)
                                      or any(f.get('issues') for f in forms) or bool(deduplication['merged_record_count'])}
