"""Build a small, reviewable 12-column comparison from existing OCR caches.

This command never invokes OCR, renders a page, copies a PDF, or changes a Web
ledger.  GLM fields come exclusively from the selected GLM analysis and its
checkpoint references.  Paddle fields come from the selected result manifest;
fresh-result mode verifies a completed genuine OCR run for every PDF.
The exported workbook is an experimental comparison, not an approved ledger.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))

from material_ocr.materials.rebar.glm_experiment import location, token
from material_ocr.materials.rebar.matching import grade_number, grade_numeric_details, same_number
from material_ocr.materials.rebar.entry_duplicates import deduplicate_certificate_records
from material_ocr.materials.rebar.heat_review import single_heat_difference

COLUMNS = ['供应商是否报审', '文件编号', '材料名称', '规格品种', '生产单位', '供应单位',
           '供货日期与工程材料报审签认日期', '使用部位', '送货单编号', '炉批号', '验收数量', '质保书编号']
MODEL_NAMES = {'paddle': 'Paddle 本地既有结果', 'glm': 'GLM 缓存重解析'}
FRESH_MODEL_NAMES = {'paddle': 'Paddle 本次重新OCR', 'glm': 'GLM 缓存按当前规则重解析'}
REPLAY_MODEL_NAMES = {'paddle': 'Paddle 缓存按双路线重新匹配', 'glm': 'GLM 缓存按双路线重新匹配'}
REPLAY_COMPARISON_NOTE = (
    '两模型均引用已有原始 OCR，按本次双路线重新匹配；本次未重新运行 OCR。'
    '清单有质保书编号时先定位证书再按炉批号找铭牌；无编号时按清单牌号和规格找铭牌。'
    '两者使用各自版式解析器，差异不能仅归因于 OCR 模型。')
FRESH_COMPARISON_NOTE = (
    'Paddle 本次真实重新 OCR 并应用当前修正规则；GLM 复用原始 OCR 缓存按当前规则重解析。'
    '两者遵循相同业务约束，但使用各自版式解析器；差异不能仅归因于模型。')
NOTES = [
    'Paddle 保留历史台账及当时的匹配规则，GLM 使用本次修正规则重解析；比较包含 OCR、版式解析及规则版本差异，不能只归因于模型，也不是只替换模型的受控准确率实验。',
    '非空字段数仅表示提取覆盖，未经人工真值核对，不能当作准确率。所有实验结果均待导出前人工核验。',
    'GLM 仅引用自身 OCR；生产单位取铭牌顶部，供应单位取送货单顶部。无可靠结果时保留空值和候选。',
    '有清单证号：清单→质量证明书→炉批号→铭牌→送货单；无清单证号：清单牌号和规格→铭牌→送货单，再按铭牌炉批号反查证书。已知关键词矛盾仍保留异常；同页送货单优先关联，跨页送货行须规格严格相同且牌号数字部分相同，字母差异保留待最终核验。',
    '同一 PDF 内非空质保书编号重复时，保留按页码/行号首次出现的记录，并把后续页的来源证据和冲突附到该行；不累加验收数量。无质保编号的记录不因缺号而合并。',
    'GLM 定位精度为整页或实际 ROI，不提供伪造的文字框。本次仅生成 JSON、HTML 和 Excel，不生成图片。',
    '空值与候选不等同于 OCR 漏识别：可能来自原件未填写、文字未读出、版式未解析或匹配约束未满足。原清单缺少牌号时不从铭牌反填清单牌号。',
]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def issue_text(value):
    if isinstance(value, dict):
        return str(value.get('reason') or value.get('code') or value)
    return str(value)


def compact_evidence(value):
    if not isinstance(value, dict):
        return {}
    keys = ('page_no', 'polygon_page', 'polygon_ocr', 'source_pixel_size', 'coordinate_system',
            'precision', 'method', 'source_role', 'region_id', 'row_id', 'logical_row',
            'line_no', 'text_segment_start_line', 'text_segment_end_line',
            'checkpoint_reference', 'checkpoint_sha256', 'raw_text', 'cell_id', 'matcher_cell_id',
            'row_group_id', 'word_box', 'confidence_known', 'ocr_score', 'render_dpi')
    return {key: value[key] for key in keys if key in value}


def evidences(value):
    if isinstance(value, dict):
        value = [value]
    return [compact_evidence(v) for v in (value or []) if isinstance(v, dict)]


def new_field(value='', page=None, evidence=None, issues=None, candidates=None, *, status=None):
    value = '' if value is None else str(value).strip()
    issues = list(dict.fromkeys(issue_text(i) for i in (issues or [])))
    return dict(value=value, page=page, status=status or ('needs_review' if issues else 'observed' if value else 'missing'),
                evidence=evidences(evidence), diagnostics=issues, candidates=candidates or [],
                review_required=True)


def candidate(value, source, reason=''):
    return dict(value=str(value or ''), page=source.get('page_no'),
                candidate_id=source.get('candidate_id', ''), reason=reason,
                evidence=evidences(source.get('evidence')))


def entry_field_evidence(record, *field_names):
    """Prefer an observed entry cell's own location over the row locator.

    GLM per-cell OCR now shares the adaptive table matcher.  Its cell range is
    real image evidence, whereas a row anchor is useful only as a fallback.
    This helper changes locations only; it never changes parsed field values
    or any of the four-form matching rules.
    """
    fields = record.get('field_evidence', {})
    result = []
    for field_name in field_names:
        for item in evidences(fields.get(field_name)):
            item.setdefault('source_role', 'inspection')
            if item not in result:
                result.append(item)
    if result:
        return result
    return evidences(record.get('evidence'))


def canonical(value, column=''):
    text = unicodedata.normalize('NFKC', str(value or '')).strip()
    if column == '验收数量' and text:
        try:
            return format(Decimal(re.sub(r'\s*(?:t|吨)$', '', text, flags=re.I)).normalize(), 'f')
        except InvalidOperation:
            pass
    return re.sub(r'\s+', '', text).upper().replace('Ф', 'Φ').replace('Ø', 'Φ')


def _delivery_grade_mode(plate_grade, delivery_grade):
    """Return the grade comparison mode used for a cross-page delivery join.

    Diameter remains an exact key.  The grade text is retained verbatim in
    the resources and ledger; only the fallback comparison key ignores OCR
    noise in its alphabetic prefix.  ``grade_number`` deliberately returns a
    value only for one unambiguous strength class, so a missing or ambiguous
    hand-written grade cannot silently become a match.
    """
    if not plate_grade or not delivery_grade:
        return None
    plate_number = grade_number(plate_grade)
    delivery_number = grade_number(delivery_grade)
    if plate_number and plate_number == delivery_number:
        return ('exact' if not (grade_numeric_details(plate_grade)['ignored_leading_digits'] or
                                grade_numeric_details(delivery_grade)['ignored_leading_digits'])
                and canonical(plate_grade, '牌号') == canonical(delivery_grade, '牌号')
                else 'numeric')
    return None


def _delivery_row_grade(row):
    """Read one row's grade without treating its weight as a grade number.

    Older GLM resources intentionally left ``grade`` empty for hand-written
    OCR words outside HRB's strict parser.  Such a word may still preserve a
    usable strength number.  Look only for alphabetic grade-like words in
    that same row, keep their literal text, and require one numeric class.
    Never scan unrelated lines or interpret a bare number as a grade. A
    spurious leading digit in B3500E may be ignored by the comparison helper;
    the literal B3500E remains the evidence and is never rewritten to B500E.
    """
    if row.get('grade'):
        return str(row['grade']), False
    source = unicodedata.normalize('NFKC', str(row.get('source_line') or ''))
    words = re.findall(r'(?:[0-9]级)?[A-Za-z]+[0-9]{3,8}[A-Za-z]*(?![0-9])', source)
    if any(not grade_number(word) for word in words):
        return '', False
    numbers = unique(grade_number(word) for word in words)
    if len(numbers) != 1:
        return '', False
    return next(word for word in words if grade_number(word) == numbers[0]), True


def _delivery_grade_reason(plate_grade, delivery_grade, mode=None):
    if mode is None:
        mode = _delivery_grade_mode(plate_grade, delivery_grade)
    if mode == 'numeric':
        ignored = grade_numeric_details(delivery_grade)['ignored_leading_digits']
        plate_strength = grade_number(plate_grade)
        delivery_strength = grade_number(delivery_grade)
        suffix_reason = ('；按右端三位强度数字比较，忽略多识别的前置数字'
                         + '、'.join(item['ignored_prefix'] for item in ignored)) if ignored else ''
        return (f'牌号数字部分一致（铭牌「{plate_grade}」→{plate_strength}，'
                f'送货行「{delivery_grade}」→{delivery_strength}）；'
                '字母/原始 OCR 仍待人工核验' + suffix_reason)
    if mode == 'exact':
        return f'牌号完全一致（{plate_grade}）'
    if plate_grade and delivery_grade:
        return f'牌号不一致（铭牌 {plate_grade}，送货行 {delivery_grade}）'
    if not plate_grade:
        return '铭牌牌号未识别，无法校验'
    return f'送货行牌号未识别（铭牌值 {plate_grade}）'


def _delivery_matching_rows(plate, delivery):
    """Retain the unmodified row literals and locations used for association."""
    result = []
    for row in delivery.get('rows', []):
        observed_grade, recovered = _delivery_row_grade(row)
        mode = _delivery_grade_mode(plate.get('grade'), observed_grade)
        # Diameter/specification remains a strict numeric key.  Formatting
        # variants such as ``Φ12``/``12`` or ``12.0``/``12`` are the same
        # measured diameter, while different values (or a missing value) do
        # not match.  Only the grade's alphabetic prefix is relaxed.
        if (not mode or not plate.get('diameter_mm') or
                not same_number(row.get('diameter_mm'), plate.get('diameter_mm'))):
            continue
        evidence = evidences(row.get('evidence')) or evidences(delivery.get('evidence'))
        result.append(dict(grade_raw=row.get('grade', ''),
            matched_grade_raw=observed_grade, grade_numeric_key=grade_number(observed_grade),
            grade_numeric_details=grade_numeric_details(observed_grade),
            grade_from_source_line=recovered, diameter_mm_raw=row.get('diameter_mm', ''),
            grade_comparison=mode, source_line=row.get('source_line', ''),
            logical_row=row.get('logical_row'), row_id=row.get('row_id'), evidence=evidence))
    return result


def unique(values):
    return list(dict.fromkeys(v for v in values if v not in (None, '')))


def checkpoint_context(checkpoints):
    """Read approval fields from GLM text itself, retaining uncertain text."""
    contexts = []
    for cp in checkpoints:
        if cp.get('upstream_role') == 'submission':
            from material_ocr.materials.rebar.submission_ledger import submission_context
            contexts.append(submission_context(cp, new_field))
            continue
        raw = cp.get('raw_text', '')
        if not re.search(r'工程材料.{0,20}报审表', raw.split('\n', 1)[0]):
            continue
        ev = location(cp)
        fields = {'供应商是否报审': new_field('', cp['page_no'], ev,
                    ['报审表存在，未识别供应商是否报审的明确字段，待最终核验'])}
        # Horizontal whitespace only: an empty 编号 must not consume the 致
        # line below it and invent an approval number.
        number = re.search(r'(?:^|\n|\s)编号[ \t]*[:：][ \t]*([^\n]*)', raw)
        if number and number[1].strip():
            number_value = number[1].strip()
            fields['文件编号'] = new_field(number_value, cp['page_no'], ev,
                ['报审编号末尾不完整，保留 OCR 原值'] if re.search(r'[-－·]$', number_value) else [])
        date = re.search(r'于\s*(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', raw)
        if date:
            try:
                day = datetime(*map(int, date.groups())).strftime('%Y-%m-%d')
                fields['供货日期与工程材料报审签认日期'] = new_field(day, cp['page_no'], ev)
            except ValueError:
                fields['供货日期与工程材料报审签认日期'] = new_field('', cp['page_no'], ev, ['GLM 日期不是有效日期'])
        body = re.search(r'(?:拟用于工程|于工程)\s*(.{1,100}?)\s*部位的\s*(.{1,40}?)\s*[，,]?\s*经我方检', raw, re.S)
        if body:
            fields['使用部位'] = new_field(re.sub(r'\s+', '', body[1]), cp['page_no'], ev)
            fields['材料名称'] = new_field(re.sub(r'[\s，,]+', '', body[2]), cp['page_no'], ev)
        contexts.append(fields)
    contexts.sort(key=lambda context: min((f.get('page') or 0 for f in context.values()), default=0))
    result = {}
    for column in COLUMNS:
        observations = [context[column] for context in contexts if context.get(column, {}).get('value')]
        values = unique(f['value'] for f in observations)
        if len(values) == 1:
            result[column] = deepcopy(observations[0])
            result[column]['evidence'] = [e for f in observations for e in f['evidence']]
            result[column]['diagnostics'] = unique(i for f in observations for i in f.get('diagnostics', []))
            result[column]['review_notes'] = unique(i for f in observations for i in f.get('review_notes', []))
            result[column]['review_required'] = bool(result[column]['diagnostics'])
            result[column]['status'] = 'needs_review' if result[column]['diagnostics'] else 'observed'
        elif values:
            number = column == '文件编号'
            result[column] = new_field(observations[0]['value'] if number else '', observations[0]['page'],
                observations[0]['evidence'] if number else [e for f in observations for e in f['evidence']],
                ['报审表编号存在多个不同 OCR 值，暂填第一张送审表编号，待核验' if number else '报审表字段存在多个不同 OCR 值，未选定'],
                [dict(value=f['value'], page=f['page'], evidence=f['evidence']) for f in observations])
            result[column]['conflict'] = True
        else:
            empty_observations = [context[column] for context in contexts if column in context]
            if empty_observations:
                # Missing explicit fields can still have a valid source page
                # and an explanation. Do not discard their review evidence.
                result[column] = deepcopy(empty_observations[0])
    from material_ocr.materials.rebar.submission_ledger import classification_field
    classified = classification_field(checkpoints, new_field)
    if classified is not None:
        result['供应商是否报审'] = classified
    return result


def company_field(resource, key):
    heading_key = 'manufacturer_heading' if key == 'manufacturer' else 'supplier_heading'
    heading = resource.get(heading_key) or {}
    value = heading.get('value', resource.get(key, ''))
    raw_candidates = heading.get('candidates') or resource.get(key + '_candidates') or []
    candidates = []
    for item in raw_candidates:
        if isinstance(item, dict):
            candidates.append(dict(value=item.get('value') or item.get('company') or item.get('raw_value', ''),
                page=item.get('page_no', resource['page_no']), evidence=evidences(item.get('evidence'))))
        else:
            candidates.append(candidate(item, resource))
    issues = list(heading.get('issues') or [])
    issues.extend(i for i in resource.get('issues', [])
                  if any(word in issue_text(i) for word in ('company', 'supplier', 'manufacturer', 'heading')))
    if not value:
        issues.append('铭牌顶部生产单位未可靠识别' if key == 'manufacturer' else '送货单顶部供应单位未可靠识别')
    return new_field(value, resource['page_no'], heading.get('evidence') or resource.get('evidence'), issues, candidates)


def delivery_choices(plate, deliveries):
    """Same-page header first; cross-page rows need an exact diameter and grade.

    The diameter is never relaxed.  For the grade, an exact OCR text match is
    retained in diagnostics, while a unique strength-number match is also
    accepted so hand-written alphabetic prefixes do not prevent an otherwise
    strong association.  The latter mode is explicitly marked for final
    human review by ``build_glm_ledger``.
    """
    same = [d for d in deliveries if d['page_no'] == plate['page_no']]
    if same:
        # A number conflict belongs to the field, not the physical relationship.
        # Distinct form/view candidates remain ambiguous; never pick one here.
        if len(same) == 1 and _delivery_matching_rows(plate, same[0]):
            # Same-page co-location establishes the form relationship.  When a
            # readable row also agrees, retain that physical row so a delivery
            # form with several goods lines can be shared by multiple plates.
            return same, 'same_page_exact', True
        return same, 'same_page_header', len(same) == 1
    matched = []
    for delivery in deliveries:
        if _delivery_matching_rows(plate, delivery):
            matched.append(delivery)
    # Full-text agreement must not take precedence over another allowed
    # numeric match on a different form.  Preserve all such forms as ambiguous.
    return matched, 'grade_digits_and_exact_diameter', len(matched) == 1


def deliveries_available_for_plate(plate, deliveries, occupied_pages):
    """Keep a co-located delivery with the nameplate already on that page.

    A same-page form takes precedence even when another document contains an
    identical grade/diameter row. Reserve all delivery forms on pages that
    also contain a nameplate from cross-page matching; local nameplates still
    see their local forms and independently resolve their physical goods rows.
    """
    if any(delivery['page_no'] == plate['page_no'] for delivery in deliveries):
        return deliveries
    return [delivery for delivery in deliveries if delivery['page_no'] not in occupied_pages]


def review_delivery_choices(plate, deliveries):
    """Find review locations without weakening automatic matching constraints.

    An exact/same-page ambiguity stays ambiguous. When no allowed match exists,
    the nearest delivery having at least one equal keyword is a review-only
    candidate. A single entirely unreadable delivery page is only a locator.
    Neither fallback can populate a confirmed value or a sources entry.
    """
    existing, method, _ = delivery_choices(plate, deliveries)
    if existing:
        candidates = [dict(d, review_only=True, review_reason=(
            delivery_association_failure_reason(plate, deliveries) if method == 'same_page_header'
            else '牌号数字部分与严格直径一致；送货单候选尚未唯一确认')) for d in existing]
        pages = unique(d['page_no'] for d in candidates)
        return candidates, pages[0] if len(pages) == 1 else None
    grade, diameter = plate.get('grade'), plate.get('diameter_mm')
    ranked = []
    comparable = False
    for delivery in deliveries:
        best = None
        for row in delivery.get('rows', []):
            row_grade, _ = _delivery_row_grade(row)
            row_diameter = row.get('diameter_mm')
            comparable = comparable or bool((grade and row_grade) or (diameter and row_diameter))
            grade_mode = _delivery_grade_mode(grade, row_grade)
            same_grade = bool(grade_mode)
            same_diameter = bool(diameter and row_diameter and diameter == row_diameter)
            agreements = int(same_grade) + int(same_diameter)
            if not agreements:
                continue
            reasons = []
            reasons.append(_delivery_grade_reason(grade, row_grade))
            if same_diameter:
                reasons.append(f'直径严格一致（{diameter}）')
            elif not diameter:
                reasons.append('铭牌直径缺失，无法校验')
            elif not row_diameter:
                reasons.append(f'送货行直径未识别（铭牌值 {diameter}）')
            else:
                reasons.append(f'直径不一致（铭牌 {diameter}，送货行 {row_diameter}）')
            mismatches = int(bool(grade and row_grade and not same_grade)) + int(bool(diameter and row_diameter and not same_diameter))
            score = (agreements, -mismatches)
            if best is None or score > best[0]:
                best = (score, '；'.join(reasons) + '；仅供人工核验，不满足自动双关键词匹配')
        if best:
            ranked.append((best[0][0], abs(delivery['page_no'] - plate['page_no']),
                dict(delivery, review_only=True, review_reason=best[1])))
    if ranked:
        highest_agreement = max(item[0] for item in ranked)
        nearest = min(item[1] for item in ranked if item[0] == highest_agreement)
        candidates = [item[2] for item in ranked if item[:2] == (highest_agreement, nearest)]
        pages = unique(d['page_no'] for d in candidates)
        return candidates, pages[0] if len(pages) == 1 else None
    pages = unique(d['page_no'] for d in deliveries)
    return [], pages[0] if not comparable and len(pages) == 1 else None


def delivery_association_failure_reason(plate, deliveries):
    """Name the failed delivery-join stage and the observed cause."""
    local = [d for d in deliveries if d.get('page_no') == plate.get('page_no')]
    if len(local) > 1:
        numbers = unique(number for d in local for number in
            ([d.get('delivery_no')] + d.get('delivery_number_candidates', [])))
        labels = '、'.join(numbers) or '编号未识别'
        return (f'铭牌第{plate.get("page_no")}页同页送货表单识别到{len(local)}个独立候选（{labels}）；'
                '无法判断属于同一张单的重复OCR视图还是不同送货单，失败步骤为“铭牌→送货单表单唯一化”')
    rows = [(delivery, row) for delivery in deliveries for row in delivery.get('rows', [])]
    if not deliveries:
        return '未解析出送货单表单，失败步骤为“铭牌→送货单检索”；无送货单页面可供关联'
    if not rows:
        pages = '、'.join(str(page) for page in unique(d.get('page_no') for d in deliveries))
        if local:
            return (f'铭牌与第{pages}页送货单同页，表单关联已按共页规则成立；'
                    '但OCR未解析出可核验的货物明细行，未确认此铭牌对应的具体货物行；'
                    '失败步骤为“送货单货物行解析/规格牌号校验”')
        return (f'已定位送货单页面（第{pages}页），但OCR没有解析出货物明细行；'
                '失败步骤为“送货单货物行解析/规格牌号校验”')
    grade, diameter = plate.get('grade', ''), plate.get('diameter_mm', '')
    reasons = []
    for delivery, row in rows:
        row_grade, _ = _delivery_row_grade(row)
        row_diameter = row.get('diameter_mm')
        if not grade or not row_grade:
            reasons.append('牌号缺失或未能解析')
        elif not _delivery_grade_mode(grade, row_grade):
            reasons.append(f'牌号数字部分不一致（铭牌 {grade}，送货行 {row_grade}）')
        if not diameter or not row_diameter:
            reasons.append('规格直径缺失或未能解析')
        elif not same_number(diameter, row_diameter):
            reasons.append(f'规格直径不一致（铭牌 {diameter}，送货行 {row_diameter}）')
    detail = '；'.join(unique(reasons)) or '送货单物理行与铭牌关键词未形成唯一匹配'
    return f'失败步骤为“铭牌→送货单货物行匹配”：{detail}'


def delivery_number_conflict_reason(delivery, matched_rows=()):
    """Describe conflicting full-page/ROI numbers without losing form identity."""
    observations = delivery.get('delivery_number_observations', [])
    rendered = []
    for observation in observations:
        value = observation.get('value')
        if not value:
            continue
        precision = '裁区OCR' if observation.get('precision') == 'region' else '整页OCR'
        label = f'{precision}读到「{value}」'
        if label not in rendered:
            rendered.append(label)
    numbers = unique(number for item in observations for number in [item.get('value')])
    if not numbers:
        numbers = unique([delivery.get('delivery_no')] + delivery.get('delivery_number_candidates', []))
        rendered = [f'送货单编号读数「{value}」' for value in numbers]
    suppliers = unique(delivery.get('supplier_candidates', []) + [delivery.get('supplier', '')])
    quality = []
    issue_codes = {issue_text(issue) for issue in delivery.get('issues', [])}
    if any('truncated' in issue for issue in issue_codes):
        quality.append('裁区OCR输出截断')
    if any('repeated' in issue for issue in issue_codes):
        quality.append('裁区OCR存在重复文本')
    detail = '；'.join(rendered) or '编号候选不一致'
    supplier_detail = ('供应单位两视图一致为「' + suppliers[0] + '」' if len(suppliers) == 1
                       else '供应单位读数也存在冲突' if len(suppliers) > 1 else '供应单位未识别')
    association = '同一张送货单表单已定位'
    if matched_rows:
        association += f'，规格/牌号已命中{len(matched_rows)}条送货货物行'
    quality_detail = ('；' + '、'.join(quality) if quality else '')
    return (f'{association}；送货单编号存在冲突：{detail}，单号冲突不能自动选定；'
            f'{supplier_detail}{quality_detail}。失败步骤为“送货单编号确认”，单号留空待人工核验')


def _plate_summary(plate):
    return dict(candidate_id=plate['candidate_id'], page_no=plate['page_no'],
        grade=plate.get('grade', ''), diameter_mm=plate.get('diameter_mm', ''),
        heat_lots=plate.get('heat_lots', []), heat_lot_candidates=plate.get('heat_lot_candidates', []),
        manufacturer=company_field(plate, 'manufacturer'), issues=plate.get('issues', []),
        evidence=evidences(plate.get('evidence')))


def _delivery_summary(delivery):
    return dict(candidate_id=delivery['candidate_id'], page_no=delivery['page_no'],
        supplier=company_field(delivery, 'supplier'), delivery_no=delivery.get('delivery_no', ''),
        delivery_number_candidates=delivery.get('delivery_number_candidates', []),
        delivery_number_observations=deepcopy(delivery.get('delivery_number_observations', [])),
        evidence_views=evidences(delivery.get('evidence_views', [])),
        merged_view_candidate_ids=delivery.get('merged_view_candidate_ids', []),
        rows=delivery.get('rows', []), issues=delivery.get('issues', []),
        review_only=delivery.get('review_only', False), review_reason=delivery.get('review_reason', ''),
        evidence=evidences(delivery.get('evidence')))


def _entry_certificate_key(value):
    """Distinguish an absent identifier from an unreadable, nonempty one."""
    key = token(value)
    placeholders = {'', '无', '暂无', '未填', '未填写', '未提供', 'NONE', 'NULL', 'N/A', 'NA'}
    return '' if key in placeholders or re.fullmatch(r'[-—–_/\\]+', key) else key


def _review_certificate_numbers(original, certificates, *, minimum=0.7, limit=3):
    """Locate similar certificate text for review without creating a match.

    The original literals are never corrected or used as lookup aliases. Rank
    certificate pages by their best similarity, retain tied candidates within
    the bounded set, and preserve multiple observed numbers on each selected
    certificate. These results must not enter the heat/nameplate route.
    """
    expected = _entry_certificate_key(original)
    if not expected:
        return []
    ranked = []
    for index, certificate in enumerate(certificates):
        observations = []
        for value in unique(certificate.get('certificate_numbers', [])):
            observed = token(value)
            if not observed or observed == expected:
                continue
            score = SequenceMatcher(None, expected, observed, autojunk=False).ratio()
            if score < minimum:
                continue
            reason = (f'清单编号「{original}」与证书编号「{value}」非完全相同；'
                      '仅作相似编号复核定位，不确认关联或继续炉批号匹配')
            item = candidate(value, certificate, reason)
            if not item['evidence']:
                item['evidence'] = [dict(page_no=certificate['page_no'], source_role='certificate',
                    precision='page', method='certificate_number_similarity_review_only', raw_text=value)]
            item.update(review_only=True, similarity=score, entry_value=original)
            observations.append(item)
        if observations:
            observations.sort(key=lambda item: (-item['similarity'], item['value']))
            ranked.append((observations[0]['similarity'], certificate['page_no'], index, observations))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    result, seen_certificates = [], set()
    for _, page, index, observations in ranked:
        certificate = certificates[index]
        identity = (page, certificate.get('candidate_id') or '')
        if identity not in seen_certificates:
            if len(seen_certificates) >= limit:
                break
            seen_certificates.add(identity)
        for item in observations:
            if not any(old['page'] == item['page'] and old['value'] == item['value'] for old in result):
                result.append(item)
    return result


def _punctuation_certificate_match(original, certificates):
    """Join a unique punctuation-only variant, retaining both literal values.

    This is deliberately narrower than similarity: every letter and digit,
    including its order, must remain identical. Multiple possible certificate
    documents or multiple observed numbers on a document stay unconfirmed.
    """
    def key(value):
        value = token(value)
        if not re.fullmatch(r'[A-Z0-9][A-Z0-9._/\\-]*', value):
            return ''
        return re.sub(r'[^A-Z0-9]', '', value)
    expected = key(original)
    if not expected:
        return [], []
    matches = []
    for certificate in certificates:
        numbers = unique(certificate.get('certificate_numbers', []))
        equivalent = [value for value in numbers
            if token(value) != token(original) and key(value) == expected]
        if equivalent:
            matches.append((certificate, numbers, equivalent))
    if len(matches) != 1 or len(matches[0][1]) != 1:
        return [], []
    certificate, _, observed = matches[0]
    observed = observed[0]
    reason = (f'清单质保书编号「{original}」与质量证明书第{certificate["page_no"]}页编号「{observed}」'
              '仅标点不同，字母数字序列完全相同；已关联继续炉批号匹配，保留清单值并标注矛盾待人工核验')
    item = candidate(observed, certificate, reason)
    if not item['evidence']:
        item['evidence'] = [dict(page_no=certificate['page_no'], source_role='certificate',
            precision='page', method='certificate_number_punctuation_review', raw_text=observed)]
    item.update(kind='certificate_number_punctuation_difference', entry_value=original,
        review_only=False, write_allowed=True, associated=True, needs_review=True)
    return [certificate], [item]


def _certificate_grade_compatible(expected, observed):
    """Allow OCR prefix noise only when both observed strength classes agree."""
    strength = _certificate_grade_strength
    return (not expected or not observed or token(expected) == token(observed) or
            bool(strength(expected) and strength(expected) == strength(observed)))


def _certificate_grade_strength(value):
    """Return the unambiguous class accepted on the certificate route."""
    return grade_number(value)


def _plate_boundary_unverified(plate):
    codes = {i.get('code') if isinstance(i, dict) else str(i) for i in plate.get('issues', [])}
    return ('physical_card_boundary_not_verified' in codes or
            plate.get('scope') in ('full', 'model_plain_text', 'model_logical_table'))


def _independent_panel_key(plate):
    """A complete single-card input is usable, with its boundary warning kept."""
    evidence = plate.get('evidence') or {}
    return (evidence.get('kind') == 'independent_panel_view' and
            evidence.get('physical_card_boundary_verified') is False and
            evidence.get('logical_card_count', 1) == 1 and
            len(unique(plate.get('heat_lots', []))) == 1 and
            plate.get('grade') and plate.get('diameter_mm') and plate.get('manufacturer') and
            not plate.get('spatial_unresolved') and
            'glm_output_truncated' not in plate.get('issues', []))


def _single_heat_certificate_plate_views(plates):
    """Keep physical single-heat cards and reconcile one measured crop view.

    A report/table ROI containing several heats is not a competing physical
    card for every heat it mentions.  A segmented mixed-form reading and a
    separately measured card ROI may, however, be two views of the same card.
    Reconcile that pair only with equal observed keys, near containment, and
    one-to-one correspondence. Two actual cards remain independent candidates.
    """
    singles = [p for p in plates if len(unique(p.get('heat_lots', []))) == 1
               and not p.get('spatial_unresolved')]

    def segment(p):
        codes = {i.get('code') if isinstance(i, dict) else str(i) for i in p.get('issues', [])}
        evidence = p.get('evidence') or {}
        return ('mixed_form_nameplate_text_segment' in codes and
                _plate_boundary_unverified(p) and
                evidence.get('text_segment_start_line') and
                evidence.get('text_segment_end_line') and evidence.get('raw_text'))

    def measured_card(p):
        return (p.get('scope') == 'explicit_roi' and
                not _plate_boundary_unverified(p) and
                (p.get('evidence') or {}).get('polygon_page'))

    def compatible(a, b):
        if (a.get('page_no') != b.get('page_no') or
                unique(a.get('heat_lots', [])) != unique(b.get('heat_lots', [])) or
                not a.get('grade') or token(a['grade']) != token(b.get('grade')) or
                not a.get('diameter_mm') or not b.get('diameter_mm') or
                not same_number(a['diameter_mm'], b['diameter_mm']) or
                (a.get('manufacturer') and b.get('manufacturer') and
                 token(a['manufacturer']) != token(b['manufacturer']))):
            return False
        polygons = [(p.get('evidence') or {}).get('polygon_page') for p in (a, b)]
        if not all(polygons):
            return False
        boxes = [(min(x for x, _ in polygon), min(y for _, y in polygon),
                  max(x for x, _ in polygon), max(y for _, y in polygon))
                 for polygon in polygons]
        (ax1, ay1, ax2, ay2), (bx1, by1, bx2, by2) = boxes
        smaller = min((ax2-ax1)*(ay2-ay1), (bx2-bx1)*(by2-by1))
        intersection = max(0, min(ax2, bx2)-max(ax1, bx1)) * max(0, min(ay2, by2)-max(ay1, by1))
        return smaller > 0 and intersection / smaller >= .9

    matches = {index: [other for other, p in enumerate(singles)
                      if measured_card(p) and compatible(source, p)]
               for index, source in enumerate(singles) if segment(source)}
    consumed, reconciled = set(), {}
    for index, candidates in matches.items():
        if len(candidates) != 1:
            continue
        other = candidates[0]
        if sum(other in values for values in matches.values()) != 1:
            continue
        source, roi = singles[index], singles[other]
        merged = deepcopy(source)
        merged['evidence_views'] = []
        merged['source_candidate_ids'] = []
        for view in (source, roi):
            for evidence in view.get('evidence_views', [view.get('evidence', {})]):
                if evidence and evidence not in merged['evidence_views']:
                    merged['evidence_views'].append(deepcopy(evidence))
            for identifier in view.get('source_candidate_ids', [view.get('candidate_id')]):
                if identifier and identifier not in merged['source_candidate_ids']:
                    merged['source_candidate_ids'].append(identifier)
        merged['certificate_view_resolution'] = 'same_page_single_heat_segment_and_measured_roi'
        reconciled[index] = merged
        consumed.add(other)
    return [reconciled.get(index, p) for index, p in enumerate(singles) if index not in consumed]


def _unique_full_page_plate_on_certificate_heat(plate, plates, certificates):
    """Allow one structured page-level card only on a unique exact cert chain.

    This narrow exception is for a GLM full-page parse with one complete,
    explicitly labelled card and one top-company heading, where an exact
    certificate number leads to the same single heat lot. The page locator is
    retained as a review warning because no nameplate ROI polygon exists.
    """
    codes = {i.get('code') if isinstance(i, dict) else str(i) for i in plate.get('issues', [])}
    heading = plate.get('manufacturer_heading') or {}
    heats = unique(plate.get('heat_lots', []))
    evidence = plate.get('evidence') or {}
    segmented = ('mixed_form_nameplate_text_segment' in codes and
        bool(evidence.get('text_segment_start_line')) and
        bool(evidence.get('text_segment_end_line')) and
        bool(evidence.get('raw_text')) and
        codes <= {'physical_card_boundary_not_verified', 'mixed_form_nameplate_text_segment',
                  'mixed_roi_card_split_review'})
    legacy = (plate.get('scope') == 'model_plain_text' and
              codes == {'physical_card_boundary_not_verified'})
    if (not (segmented or legacy) or
            plate.get('spatial_unresolved') or len(heats) != 1 or
            not plate.get('grade') or not plate.get('diameter_mm') or
            not plate.get('manufacturer') or heading.get('rule') != 'nameplate_top_company' or
            not (plate.get('raw_header_lines') or heading.get('raw_header_lines')) or
            (not segmented and len([item for item in plates
                if item.get('page_no') == plate.get('page_no')]) != 1) or
            (segmented and len([item for item in plates
                if item.get('page_no') == plate.get('page_no') and
                   heats[0] in item.get('heat_lots', []) and item.get('grade') and
                   item.get('diameter_mm')]) != 1) or
            len(certificates) != 1):
        return False
    certificate = certificates[0]
    # One page can contain conflicting OCR readings of its certificate No.
    # That is not the unique certificate chain needed to waive a missing
    # physical card boundary. Explicit ROI cards retain their normal rules.
    if len(unique(certificate.get('certificate_numbers', []))) != 1:
        return False
    certificate_heats = unique(certificate.get('heat_lots', []))
    return len(certificate_heats) == 1 and heats[0] == certificate_heats[0]


def _unique_preceding_attachment_plate(plate, plates, deliveries, certificates):
    """Resolve one complete, unique card on the immediately preceding page.

    This is a document-specific attachment-group signal, gated by a unique
    certificate, exact single heat, complete plate key, a same-page delivery,
    and exactly one matching plate key on that page.  It retains a review
    diagnostic because the crop is still not a verified card boundary.
    """
    if (len(certificates) != 1 or len(unique(certificates[0].get('certificate_numbers', []))) != 1 or
            plate.get('page_no') != certificates[0].get('page_no', 0) - 1):
        return False
    views = plate.get('evidence_views', [plate.get('evidence', {})])
    if not any(v.get('kind') == 'attachment_recovery_view' for v in views):
        return False
    heats = unique(plate.get('heat_lots', []))
    cert_heats = unique(certificates[0].get('heat_lots', []))
    if (len(heats) != 1 or not set(heats).issubset(cert_heats) or not plate.get('grade') or
            not plate.get('diameter_mm') or plate.get('spatial_unresolved')):
        return False
    # A certificate can expose batch and smelting columns, or conflicting
    # OCR views of one heat.  Require a locally observed exact plate heat;
    # the other literals remain visible as certificate conflicts.
    observed_heats = {o.get('value') for o in certificates[0].get('heat_observations', [])}
    if len(cert_heats) > 1 and heats[0] not in observed_heats:
        return False
    page_plates = [p for p in plates if p.get('page_no') == plate['page_no'] and
        unique(p.get('heat_lots', [])) == heats and p.get('grade') == plate['grade'] and
        p.get('diameter_mm') == plate['diameter_mm'] and
        p.get('manufacturer') == plate['manufacturer']]
    return len(page_plates) == 1 and len([d for d in deliveries if d.get('page_no') == plate['page_no']]) == 1


def _heat_review_candidates(certificates, plates, *, grade='', diameter=''):
    """Return one-letter heat-lot conflicts with evidence for safe association.

    The caller promotes a conflict only when one unique certificate and one
    unique nameplate each have one heat, and no exact-heat nameplate was
    rejected for another conflict. Multiple candidates remain review-only.
    """
    result = []
    if len(certificates) != 1:
        return result
    for certificate in certificates:
        if len(unique(certificate.get('certificate_numbers', []))) != 1:
            continue
        for certificate_heat in unique(certificate.get('heat_lots', [])):
            for plate in plates:
                if _plate_boundary_unverified(plate) or plate.get('spatial_unresolved'):
                    continue
                if not _certificate_grade_compatible(grade, plate.get('grade')):
                    continue
                if not diameter or not plate.get('diameter_mm') or not same_number(plate['diameter_mm'], diameter):
                    continue
                for nameplate_heat in unique(plate.get('heat_lots', [])):
                    difference = single_heat_difference(certificate_heat, nameplate_heat)
                    if not difference:
                        continue
                    certificate_evidence = evidences(certificate.get('evidence')) or [
                        dict(page_no=certificate['page_no'], source_role='certificate', precision='page')]
                    plate_evidence = evidences(plate.get('evidence')) or [
                        dict(page_no=plate['page_no'], source_role='nameplate', precision='page')]
                    for item in certificate_evidence:
                        item.setdefault('page_no', certificate['page_no'])
                        item['source_role'] = 'certificate'
                    for item in plate_evidence:
                        item.setdefault('page_no', plate['page_no'])
                        item['source_role'] = 'nameplate'
                    result.append(dict(kind='certificate_nameplate_heat_conflict',
                        candidate_id=plate.get('candidate_id', ''), page=plate['page_no'],
                        nameplate=deepcopy(plate), certificate=deepcopy(certificate),
                        certificate_heat_lot=certificate_heat,
                        nameplate_heat_lot=nameplate_heat,
                        heat_difference=difference,
                        reason=(f'质量证明书第{certificate["page_no"]}页炉批号「{certificate_heat}」与'
                            f'铭牌第{plate["page_no"]}页炉批号「{nameplate_heat}」仅有一个字母差异：'
                            f'{difference["description"]}；疑似OCR误读，保留双方原文待人工核验'),
                        locations=certificate_evidence + plate_evidence,
                        review_only=True, write_allowed=False))
    unique_result = []
    seen = set()
    for item in result:
        key = (item['certificate']['page_no'], item['nameplate']['page_no'], item.get('candidate_id'),
               item['certificate_heat_lot'], item['nameplate_heat_lot'])
        if key not in seen:
            seen.add(key); unique_result.append(item)
    return unique_result


def build_glm_ledger(resources, checkpoints=()):
    """Conservative common matching constraints over GLM logical resources.

    Native GLM has no Paddle spatial grid. This adapter reports its own parsing
    and never calls the production OCR or borrows Paddle field values.
    """
    # Apply the same certificate-number deduplication used by the production
    # automatic ledger.  The GLM resources are cached OCR-derived JSON; this
    # only creates an in-memory copy and never rewrites that cache.
    resources, dedup_diagnostics, dedup_summary = deduplicate_certificate_records(resources)
    plates, deliveries, certificates = (resources.get(k, []) for k in ('nameplates', 'deliveries', 'certificates'))
    occupied_delivery_pages = {delivery['page_no'] for delivery in deliveries
        if any(plate['page_no'] == delivery['page_no'] for plate in plates)}
    contexts = checkpoint_context(checkpoints)
    rows, links = [], []
    for plate in plates:
        available = deliveries_available_for_plate(plate, deliveries, occupied_delivery_pages)
        choices, reason, reliable = delivery_choices(plate, available)
        review_reasons = []
        if not choices:
            choices, _ = review_delivery_choices(plate, available)
            if choices:
                reason = 'partial_keywords_review_only'
                review_reasons = [d['review_reason'] for d in choices]
        links.append(dict(nameplate_id=plate['candidate_id'], page_no=plate['page_no'],
            grade=plate.get('grade', ''), diameter_mm=plate.get('diameter_mm', ''),
            manufacturer=company_field(plate, 'manufacturer'), heat_lots=plate.get('heat_lots', []),
            delivery_candidates=[_delivery_summary(d) for d in choices],
            association_reason=reason if choices else 'no_exact_delivery_key_match',
            header_unique=reliable, review_required=True, issues=plate.get('issues', []) + review_reasons,
            evidence=evidences(plate.get('evidence'))))
    for record in resources.get('records', []):
        page = record['page_no']; ev = record.get('evidence')
        fields = {column: deepcopy(contexts.get(column, new_field('', page))) for column in COLUMNS}
        grade, diameter = record.get('grade'), record.get('diameter_mm')
        original = record.get('certificate_no') or ''
        certificate_key = _entry_certificate_key(original)
        route = 'certificate_number' if certificate_key else 'specification_grade'
        spec = (str(grade) + '/Φ' + str(diameter)) if grade and diameter else ('Φ' + str(diameter) if diameter else str(grade or ''))
        entry_specification = new_field(spec, page, entry_field_evidence(record, 'specification', 'grade'),
            [] if grade and diameter else [
                '清单牌号或直径缺失；保留清单原值，证号路线通过质量证明书炉批号核验铭牌' if certificate_key
                else '清单牌号或直径缺失；保留读到的规格，不允许仅凭单关键词确认铭牌'])
        fields['规格品种'] = deepcopy(entry_specification)
        if not fields['材料名称']['value'] and not fields['材料名称'].get('candidates'):
            fields['材料名称'] = new_field(record.get('material_name'), page, entry_field_evidence(record, 'material_name'))
        fields['验收数量'] = new_field(record.get('weight_t'), page, entry_field_evidence(record, 'quantity', 'weight'),
            [] if record.get('weight_t') else ['清单进场数量未可靠识别'])
        for column in ('生产单位', '供应单位', '送货单编号', '炉批号'):
            # A missing attachment field must not link to the entry list as
            # though the entry list were its source document.
            fields[column] = new_field('')
        fields['质保书编号'] = new_field('', page, entry_field_evidence(record, 'certificate_no'))
        # A certificate-first route is selected only for a real identifier.
        # Placeholder values such as '-', '/', or '无' are treated as absent
        # and therefore use the strict entry grade+diameter route.
        certificate_candidates = [c for c in certificates if certificate_key and
            certificate_key in [token(n) for n in c.get('certificate_numbers', [])]]
        punctuation_certificate_reviews = []
        if certificate_key and not certificate_candidates:
            certificate_candidates, punctuation_certificate_reviews = _punctuation_certificate_match(original, certificates)
        certificate_review_candidates = (_review_certificate_numbers(original, certificates)
            if certificate_key and not certificate_candidates else [])
        exact_plates = [p for p in plates if grade and diameter and p.get('grade') == grade and p.get('diameter_mm') == diameter]
        certificate_heats = {heat for c in certificate_candidates for heat in c.get('heat_lots', [])}
        heat_plates = [p for p in plates if certificate_heats.intersection(p.get('heat_lots', []))]
        keyword_conflicts = []
        if certificate_key:
            # Certificate route starts at certificate -> heat -> plate.  A
            # missing plate grade/diameter is acceptable; an observed,
            # contradictory value is not.  This lets a certificate resolve a
            # row whose entry list omitted the grade while still rejecting a
            # known mismatch.
            confirmed_plates = [p for p in heat_plates
                if _certificate_grade_compatible(grade, p.get('grade'))
                and (not diameter or not p.get('diameter_mm') or same_number(p['diameter_mm'], diameter))]
            for p in heat_plates:
                for name, expected, observed in [('牌号', grade, p.get('grade')),
                                                 ('直径', diameter, p.get('diameter_mm'))]:
                    compatible = (_certificate_grade_compatible(expected, observed) if name == '牌号'
                                  else not expected or not observed or same_number(expected, observed))
                    if not compatible:
                        keyword_conflicts.append(
                            f'清单{name} {expected} 与证书炉批号命中的铭牌第{p["page_no"]}页{name} {observed} 冲突，未自动关联')
        else:
            # Without a certificate number, both entry keywords remain
            # mandatory and are compared exactly.
            confirmed_plates = exact_plates
        issues = list(record.get('issues', [])) + keyword_conflicts
        issues.extend(item['reason'] for item in punctuation_certificate_reviews)
        if certificate_key:
            # A multi-heat report is unresolved before physical-card selection;
            # it must not make an otherwise unique exact-heat card ambiguous.
            # Same-card OCR views are reconciled only on the certificate route.
            multi_heat_plates = [p for p in confirmed_plates if len(unique(p.get('heat_lots', []))) != 1]
            confirmed_plates = _single_heat_certificate_plate_views(confirmed_plates)
            for p in multi_heat_plates:
                issues.append(f'第{p["page_no"]}页候选铭牌炉批号不唯一，包含「{"、".join(p.get("heat_lots", []))}」，'
                    '未绑定到单张铭牌；保留原始候选，不阻塞其他页已精确匹配的单炉号铭牌')
        boundary_candidates = [p for p in confirmed_plates if _plate_boundary_unverified(p)]
        recoverable_boundary = [p for p in boundary_candidates
            if certificate_key and _unique_full_page_plate_on_certificate_heat(
                p, _single_heat_certificate_plate_views(plates), certificate_candidates)]
        adjacent_recovery = [p for p in boundary_candidates if p not in recoverable_boundary and
            certificate_key and _unique_preceding_attachment_plate(
                p, _single_heat_certificate_plate_views(plates), deliveries, certificate_candidates)]
        recoverable_boundary.extend(adjacent_recovery)
        boundary_rejections = [p for p in boundary_candidates if p not in recoverable_boundary
                               and not _independent_panel_key(p)]
        confirmed_plates = [p for p in confirmed_plates if not _plate_boundary_unverified(p)
                            or _independent_panel_key(p)] + [p for p in recoverable_boundary
                            if not _independent_panel_key(p)]
        for p in boundary_rejections:
            issues.append(f'铭牌第{p["page_no"]}页物理卡片边界未检验成功，整页或混合表单读数仅列为候选')
        for p in recoverable_boundary:
            issues.append(f'铭牌第{p["page_no"]}页无独立卡片边界框；因清单证号定位的唯一证书炉批号与该炉号唯一完整铭牌标签组精确一致，已沿证号路线关联，仍需核验原页物理边界')
        for p in adjacent_recovery:
            issues.append(f'铭牌第{p["page_no"]}页裁区未验证实体卡片边界；唯一完整铭牌候选与后一页证书炉批号一致，且同页有送货单，按相邻附件组关联并保留人工核验提示')
        if not certificate_key and (not grade or not diameter):
            issues.append('清单牌号或直径缺失，铭牌双关键词校验未通过')
        if certificate_key and not certificate_candidates:
            issues.append('清单质保书编号未在质量证明书中精确确认')
        if certificate_candidates and not certificate_heats:
            issues.append('已找到清单编号对应质量证明书，但证书炉批号未可靠识别，不能确认铭牌')
        if len(confirmed_plates) != 1:
            issues.append('铭牌候选不唯一' if confirmed_plates else '没有通过全部约束的铭牌')
        plate = confirmed_plates[0] if len(confirmed_plates) == 1 else None
        if certificate_key and plate:
            # A certificate route is only confirmed when its heat identifier
            # resolves to one physical plate and one unambiguous heat value.
            # Do not treat an arbitrary intersection of multiple heat values
            # as confirmation.
            plate_heats = unique(plate.get('heat_lots', []))
            if len(plate_heats) != 1:
                issues.append('质量证明书炉批号命中铭牌但该铭牌炉批号不唯一，保留候选')
                plate = None
        heat_review_candidates = (_heat_review_candidates(certificate_candidates, plates,
            grade=grade, diameter=diameter)
            if certificate_key and not plate else [])
        associated_heat_conflict = None
        if not plate and not heat_plates and len(heat_review_candidates) == 1:
            selected_review = heat_review_candidates[0]
            if (len(unique(selected_review['certificate'].get('heat_lots', []))) == 1 and
                    len(unique(selected_review['nameplate'].get('heat_lots', []))) == 1):
                plate = selected_review['nameplate']
                associated_heat_conflict = selected_review
                selected_review.update(review_only=False, write_allowed=True, associated=True,
                    needs_review=True, status='associated_with_conflict')
                selected_review['heat_difference'] = dict(selected_review['heat_difference'],
                    review_only=False, write_allowed=True)
                selected_review['reason'] += '；唯一单字母差异铭牌已自动关联，台账采用铭牌炉批号并标注为矛盾，待人工核验'
                issues = [reason for reason in issues if reason != '没有通过全部约束的铭牌']
        if heat_review_candidates:
            issues.extend(item['reason'] for item in heat_review_candidates)
        plate_review_reasons = []
        if (certificate_key and plate and grade and plate.get('grade') and
                token(grade) != token(plate['grade']) and
                grade_number(grade) and grade_number(grade) == grade_number(plate['grade'])):
            plate_review_reasons.append(
                f'清单牌号「{grade}」与铭牌第{plate["page_no"]}页牌号「{plate["grade"]}」原文不同，'
                f'强度数字均为{grade_number(grade)}；按证号和炉批号关联，保留OCR原文待人工核验')
        if plate:
            if _independent_panel_key(plate):
                plate_review_reasons.append('铭牌已按独立图像窗口的唯一牌号、规格和炉批号关联；窗口不是已验证物理卡片边界，需按原图核验')
                for code in plate.get('issues', []):
                    if code in ('nameplate_panel_view_conflict', 'nameplate_damaged_heat_label_confirmed_by_certificate'):
                        plate_review_reasons.append('独立铭牌补读与混合页读数有差异或炉号标签残缺，原始证据及证书交叉核对结果保留待核验：' + code)
            if plate.get('certificate_view_resolution'):
                plate_review_reasons.append(
                    f'铭牌第{plate["page_no"]}页补读视图与同一原页文字段共享唯一完整铭牌键，已合并OCR证据；实体卡片边界仍待核验'
                    if plate['certificate_view_resolution'] == 'same_page_unique_attachment_key_views' else
                    f'铭牌第{plate["page_no"]}页混合表单文字段与独立铭牌裁区的炉批号/牌号/规格一致且几何一一对应，'
                    '已合并为同一铭牌的OCR视图；生产单位保留铭牌文字段顶部公司，原始位置均保留待最终核验')
            if plate in recoverable_boundary:
                plate_review_reasons.append(
                    f'铭牌第{plate["page_no"]}页未生成独立卡片边界框；已按唯一证书炉批号精确关联该炉号唯一完整牌号/规格/炉批号标签组，'
                    '该关联保留整页定位并需在导出前核验物理边界')
            if plate in adjacent_recovery:
                plate_review_reasons.append(
                    f'铭牌第{plate["page_no"]}页与证书第{plate["page_no"] + 1}页按唯一相邻附件组关联；裁区不是已验证卡片边界，需导出前核验')
                if len(unique(certificate_candidates[0].get('heat_lots', []))) > 1:
                    plate_review_reasons.append('证书存在多个炉批号/冶炼炉号或OCR视图读数；关联采用铭牌精确命中的原文，其余读数保留待核验')
            for plate_issue in plate.get('issues', []):
                raw_code = plate_issue.get('code', '') if isinstance(plate_issue, dict) else str(plate_issue)
                if raw_code in ('mixed_roi_card_split_review', 'nameplate_grade_view_conflict'):
                    plate_review_reasons.append(
                        f'铭牌第{plate["page_no"]}页核验提示：{issue_text(plate_issue)}')
            grade_candidates = plate.get('grade_ocr_candidates', [])
            if grade_candidates:
                literals = unique(str(x.get('value') or x.get('raw_value') or x)
                    if isinstance(x, dict) else str(x) for x in grade_candidates)
                plate_review_reasons.append(
                    f'铭牌第{plate["page_no"]}页牌号存在其他OCR读数候选：{ "、".join(literals) }')
        issues.extend(plate_review_reasons)
        if certificate_key and plate and (plate.get('grade') or plate.get('diameter_mm')):
            # The entry row remains unchanged in `entry_specification`; this
            # display field may use the selected plate's two keywords so the
            # final ledger does not look as if the grade was lost upstream.
            display_grade = plate.get('grade') or grade
            display_diameter = plate.get('diameter_mm') or diameter
            display_spec = (str(display_grade) + '/Φ' + str(display_diameter)) if display_grade and display_diameter else (
                'Φ' + str(display_diameter) if display_diameter else str(display_grade or ''))
            plate_ev = dict(plate.get('evidence') or {}, page_no=plate['page_no'], source_role='nameplate')
            fields['规格品种'] = new_field(display_spec, plate['page_no'], plate_ev,
                plate_review_reasons + ([] if display_grade and display_diameter else ['已通过证号炉批号路线，但牌号或直径仍不完整']))
        sources, delivery_candidates, selected_delivery, selected_certificate = {}, [], None, None
        certs_by_heat = []
        if plate:
            sources['nameplate'] = dict(candidate_id=plate['candidate_id'], page_no=plate['page_no'])
            if plate.get('certificate_view_resolution'):
                sources['nameplate'].update(view_resolution=plate['certificate_view_resolution'],
                    source_candidate_ids=deepcopy(plate['source_candidate_ids']),
                    evidence_views=deepcopy(plate['evidence_views']))
            fields['生产单位'] = company_field(plate, 'manufacturer')
            heat_values = unique(plate.get('heat_lots', []))
            fields['炉批号'] = new_field(heat_values[0] if len(heat_values) == 1 else '', plate['page_no'],
                plate.get('evidence'), [] if len(heat_values) == 1 else ['铭牌炉批号缺失或不唯一'],
                [candidate(h, plate) for h in heat_values + plate.get('heat_lot_candidates', [])])
            if associated_heat_conflict:
                fields['炉批号']['diagnostics'].append(associated_heat_conflict['reason'])
                fields['炉批号']['evidence'] = deepcopy(associated_heat_conflict['locations'])
                fields['炉批号']['candidates'].append(candidate(
                    associated_heat_conflict['certificate_heat_lot'], associated_heat_conflict['certificate'],
                    associated_heat_conflict['reason']))
                fields['炉批号']['status'] = 'needs_review'
                fields['炉批号']['conflict'] = True
            available_deliveries = deliveries_available_for_plate(plate, deliveries, occupied_delivery_pages)
            delivery_candidates, method, reliable_header = delivery_choices(plate, available_deliveries)
            if reliable_header:
                selected_delivery = delivery_candidates[0]
                matched_delivery_rows = (_delivery_matching_rows(plate, selected_delivery)
                    if method != 'same_page_header' else [])
                sources['delivery'] = dict(candidate_id=selected_delivery['candidate_id'],
                    page_no=selected_delivery['page_no'], association_method=method, header_only=method == 'same_page_header')
                if matched_delivery_rows:
                    sources['delivery'].update(grade_numeric_key=grade_number(plate.get('grade')),
                        plate_grade_raw=plate.get('grade', ''),
                        plate_diameter_mm_raw=plate.get('diameter_mm', ''),
                        matched_rows=matched_delivery_rows)
                fields['供应单位'] = company_field(selected_delivery, 'supplier')
                numbers = unique([selected_delivery.get('delivery_no')] +
                    selected_delivery.get('delivery_number_candidates', []))
                number_issues = [issue_text(i) for i in selected_delivery.get('issues', [])
                    if any(w in issue_text(i) for w in ('number', 'truncated', 'repeated'))]
                number_conflict = len(numbers) > 1 or any('delivery_number_conflict' in i for i in number_issues)
                if number_conflict:
                    reason = delivery_number_conflict_reason(selected_delivery, matched_delivery_rows)
                    number_issues.append(reason)
                    issues.append(reason)
                number_evidence = []
                for observation in selected_delivery.get('delivery_number_observations', []):
                    for item in evidences(observation.get('evidence')):
                        if item not in number_evidence:
                            number_evidence.append(item)
                if not number_evidence:
                    number_evidence = evidences(selected_delivery.get('evidence'))
                number_candidates = []
                for number in numbers:
                    item = candidate(number, selected_delivery,
                        '整页与裁区OCR编号冲突，未自动选号' if number_conflict else '')
                    view_evidence = []
                    for observation in selected_delivery.get('delivery_number_observations', []):
                        if observation.get('value') != number:
                            continue
                        for evidence_item in evidences(observation.get('evidence')):
                            if evidence_item not in view_evidence:
                                view_evidence.append(evidence_item)
                    if view_evidence:
                        item['evidence'] = view_evidence
                    number_candidates.append(item)
                fields['送货单编号'] = new_field(
                    '' if number_conflict else selected_delivery.get('delivery_no'), selected_delivery['page_no'],
                    number_evidence, number_issues, number_candidates)
                if method == 'same_page_header':
                    if matched_delivery_rows:
                        issues.append('同页送货单表单已定位，货物行规格/牌号已匹配；送货单编号或其他OCR视图存在冲突时保留候选待核验')
                    else:
                        issues.append(delivery_association_failure_reason(plate, [selected_delivery]))
                elif any(r['grade_comparison'] == 'numeric' or r['grade_from_source_line']
                         for r in matched_delivery_rows):
                    raw_grades = unique(r['matched_grade_raw'] for r in matched_delivery_rows)
                    reason = (f'铭牌牌号「{plate.get("grade", "")}」与送货行牌号「{"、".join(raw_grades)}」'
                              f'按数字部分 {grade_number(plate.get("grade"))} 和严格规格 Φ{plate.get("diameter_mm")} 关联；'
                              '保留原始 OCR，字母差异或原未解析牌号待最终人工核验')
                    ignored = [item for raw in raw_grades
                        for item in grade_numeric_details(raw)['ignored_leading_digits']]
                    if ignored:
                        reason += ('；按右端三位强度数字比较，忽略多识别的前置数字'
                            + '、'.join(item['ignored_prefix'] for item in ignored))
                    issues.append(reason)
                    for key in ('供应单位', '送货单编号'):
                        fields[key]['diagnostics'] = unique(fields[key]['diagnostics'] + [reason])
                        fields[key]['status'] = 'needs_review'
            else:
                failure_reason = delivery_association_failure_reason(plate, available_deliveries)
                issues.append(failure_reason)
                delivery_candidates, page_hint = review_delivery_choices(plate, available_deliveries)
                review_issues = unique([failure_reason] + [d['review_reason'] for d in delivery_candidates])
                if page_hint and not delivery_candidates:
                    review_issues.append('送货行关键词无法比较；仅定位文档中唯一送货单页面，不确认关联')
                fallback_ev = [dict(page_no=page_hint, source_role='delivery', precision='page',
                    method='unconfirmed_delivery_page_for_review')] if page_hint else []
                fields['供应单位'] = new_field('', page_hint,
                    [e for d in delivery_candidates for e in company_field(d, 'supplier')['evidence']] or fallback_ev,
                    issues=review_issues,
                    candidates=[candidate(d.get('supplier'), d, d['review_reason']) for d in delivery_candidates if d.get('supplier')])
                fields['送货单编号'] = new_field('', page_hint,
                    [e for d in delivery_candidates for e in evidences(d.get('evidence'))] or fallback_ev,
                    issues=review_issues,
                    candidates=[candidate(n, d, d['review_reason']) for d in delivery_candidates
                        for n in unique([d.get('delivery_no')] + d.get('delivery_number_candidates', []))])
            certs_by_heat = [c for c in certificates if set(plate.get('heat_lots', [])) & set(c.get('heat_lots', []))]
        # A certificate value read on the entry list is always retained, even
        # when verification fails. A contradictory observed certificate is red.
        if certificate_key:
            seen = unique(n for c in certs_by_heat for n in c.get('certificate_numbers', []))
            cert_issues = []
            cert_issues.extend(item['reason'] for item in punctuation_certificate_reviews)
            if associated_heat_conflict:
                cert_issues.append(associated_heat_conflict['reason'])
            if not certificate_candidates:
                cert_issues.append('清单质保书编号未精确匹配质量证明书，保留清单值待核验')
            if seen and any(token(n) != certificate_key for n in seen):
                cert_issues.append('清单质保书编号与铭牌炉批号对应证书编号冲突：' + '、'.join(seen))
            exact_numbers = unique(n for c in certificate_candidates for n in c.get('certificate_numbers', []))
            certificate_number_conflicts = [n for n in exact_numbers if token(n) != certificate_key]
            if certificate_number_conflicts and not cert_issues:
                cert_issues.append('质量证明书存在冲突编号读数：' + '、'.join(exact_numbers))
            if len(certificate_candidates) > 1:
                cert_issues.append('相同清单编号对应多个质量证明书页面')
            # The entry value remains authoritative, but a unique exact match
            # makes the certificate page the most useful location for this
            # field. Keep the inspection evidence as well so reviewers can
            # compare both physical forms. Downstream heat/nameplate failures
            # must not erase this independent certificate-number match.
            certificate_field_evidence = entry_field_evidence(record, 'certificate_no')
            for item in certificate_field_evidence:
                item.setdefault('source_role', 'inspection')
            certificate_field_page = page
            if certificate_candidates:
                exact_certificate_evidence = []
                for cert in certificate_candidates:
                    for item in evidences(cert.get('evidence')) or [dict(page_no=cert['page_no'], precision='page')]:
                        # Inventory records are page-scoped; repair stale
                        # page metadata from compact synthetic/legacy caches
                        # without inventing a polygon or OCR location.
                        item['page_no'] = cert['page_no']
                        item.setdefault('source_role', 'certificate')
                        if item not in exact_certificate_evidence:
                            exact_certificate_evidence.append(item)
                certificate_field_evidence.extend(item for item in exact_certificate_evidence
                    if item not in certificate_field_evidence)
                if len(certificate_candidates) == 1 and not certificate_number_conflicts:
                    certificate_field_page = certificate_candidates[0]['page_no']
            if punctuation_certificate_reviews and len(certificate_candidates) == 1:
                certificate_field_page = certificate_candidates[0]['page_no']
            exact_candidate_sources = (certificate_candidates if (len(certificate_candidates) > 1 or
                                    punctuation_certificate_reviews) else certs_by_heat)
            certificate_field_candidates = [candidate(n, c)
                for c in exact_candidate_sources for n in c.get('certificate_numbers', [])]
            for review in punctuation_certificate_reviews:
                for item in certificate_field_candidates:
                    if item['value'] == review['value'] and item['page'] == review['page']:
                        item.update(reason=review['reason'], entry_value=original,
                            associated=True, needs_review=True)
            fields['质保书编号'] = new_field(original, certificate_field_page,
                certificate_field_evidence, cert_issues,
                certificate_field_candidates + certificate_review_candidates)
            if len(certificate_candidates) == 1:
                selected_certificate = certificate_candidates[0]
                sources['certificate'] = dict(page_no=certificate_candidates[0]['page_no'])
                if punctuation_certificate_reviews:
                    sources['certificate']['association_method'] = 'entry_certificate_number_punctuation_review'
        else:
            values = unique(n for c in certs_by_heat for n in c.get('certificate_numbers', []))
            cert = certs_by_heat[0] if len(certs_by_heat) == 1 else None
            fields['质保书编号'] = new_field(values[0] if cert and len(values) == 1 else '',
                cert['page_no'] if cert else page, cert.get('evidence') if cert else ev,
                list(cert.get('issues', [])) if cert and len(values) == 1 else ['铭牌炉批号未唯一匹配质量证明书编号'],
                [candidate(n, c) for c in certs_by_heat for n in c.get('certificate_numbers', [])])
            if cert and len(values) == 1:
                selected_certificate = cert
                sources['certificate'] = dict(page_no=cert['page_no'])
        # Unconfirmed candidates are useful on screen, but never promoted into
        # manufacturer/supplier/heat fields merely to improve coverage.
        suggested_plates = (heat_plates or exact_plates or
                            [item['nameplate'] for item in heat_review_candidates]) if certificate_key else exact_plates
        if not plate:
            plate_pages = unique(p['page_no'] for p in suggested_plates)
            plate_page = plate_pages[0] if len(plate_pages) == 1 else None
            chain_reasons = []
            if certificate_key:
                if not certificate_candidates:
                    chain_reasons.append(
                        f'清单质保书编号「{original}」未精确定位质量证明书，停在“清单编号→证书”，尚未检索铭牌炉批号')
                else:
                    certificate_readings = '；'.join(
                        f'第{c["page_no"]}页炉批号「{"、".join(c.get("heat_lots", [])) or "未可靠读出"}」'
                        for c in certificate_candidates)
                    if not certificate_heats:
                        quality_codes = unique(issue_text(i) for c in certificate_candidates
                                               for i in c.get('issues', []))
                        quality = ('；OCR输出达到长度上限或出现重复文本'
                            if any('truncated' in i or 'repeated' in i for i in quality_codes) else '')
                        chain_reasons.append(
                            f'已按清单编号找到质量证明书：{certificate_readings}{quality}；停在“证书→炉批号”，未取得可用的证书炉批号')
                    elif not heat_plates:
                        detail = ('本卷尚未解析出有效铭牌标签组，需检查混合页面/铭牌解析'
                            if not plates else '已解析铭牌中没有相同炉批号，单字母差异候选也未唯一通过约束')
                        chain_reasons.append(
                            f'已按清单编号找到质量证明书并读出炉批号：{certificate_readings}；停在“炉批号→铭牌”，{detail}')
                    else:
                        detail = '；'.join(keyword_conflicts) or '铭牌候选多条、炉批号多值或物理边界未通过核验'
                        chain_reasons.append(
                            f'质量证明书{certificate_readings}已命中铭牌；停在“铭牌约束校验”：{detail}')
            issues.extend(chain_reasons)
            fields['生产单位'] = new_field('', plate_page,
                [e for p in suggested_plates for e in company_field(p, 'manufacturer')['evidence']],
                chain_reasons + ['铭牌尚未通过证号/炉批号及已知关键词约束，生产单位仅列为候选' if certificate_key
                 else '铭牌尚未通过双关键词与关联约束，生产单位仅列为候选'],
                [candidate(p.get('manufacturer'), p, '尚未确认的铭牌顶部候选')
                    for p in suggested_plates if p.get('manufacturer')])
            heat_evidence = [e for p in suggested_plates for e in evidences(p.get('evidence'))]
            heat_candidates = []
            for p in suggested_plates:
                for heat in unique(p.get('heat_lots', []) + p.get('heat_lot_candidates', [])):
                    review = next((item for item in heat_review_candidates
                        if item['nameplate'].get('candidate_id') == p.get('candidate_id') and
                           item['nameplate_heat_lot'] == heat), None)
                    heat_candidates.append(candidate(heat, p, review['reason'] if review else ''))
            for item in heat_review_candidates:
                for evidence_item in item['locations']:
                    if evidence_item not in heat_evidence:
                        heat_evidence.append(deepcopy(evidence_item))
            # A certificate heat is an upstream observation, even if the
            # nameplate has not been resolved. Retain it as a located candidate
            # instead of misleadingly showing no OCR value or source at all.
            for cert_source in certificate_candidates:
                cert_evidence = [dict(e, source_role='certificate')
                                 for e in evidences(cert_source.get('evidence'))]
                if not cert_evidence:
                    cert_evidence = [dict(page_no=cert_source['page_no'], source_role='certificate', precision='page')]
                for evidence_item in cert_evidence:
                    if evidence_item not in heat_evidence:
                        heat_evidence.append(evidence_item)
                for heat in unique(cert_source.get('heat_lots', [])):
                    heat_candidates.append(dict(candidate(heat, cert_source,
                        '质量证明书已读出此炉批号；尚未关联唯一铭牌，保留上游读数供核验'),
                        source_role='certificate', evidence=deepcopy(cert_evidence),
                        review_only=True, write_allowed=False))
            heat_page = plate_page
            if heat_page is None and len(certificate_candidates) == 1:
                heat_page = certificate_candidates[0]['page_no']
            fields['炉批号'] = new_field('', heat_page,
                heat_evidence,
                unique(chain_reasons + ['铭牌尚未通过证号/炉批号及已知关键词约束，炉批号仅列为候选' if certificate_key
                    else '铭牌尚未通过双关键词与关联约束，炉批号仅列为候选'] +
                    [item['reason'] for item in heat_review_candidates]),
                heat_candidates)
            potential_deliveries = {}
            potential_pages = []
            for p in suggested_plates:
                candidates_for_plate, page_for_review = review_delivery_choices(p, deliveries)
                if page_for_review:
                    potential_pages.append(page_for_review)
                for d in candidates_for_plate:
                    potential_deliveries[d['candidate_id']] = d
            delivery_candidates = list(potential_deliveries.values())
            delivery_pages = unique([d['page_no'] for d in delivery_candidates] + potential_pages)
            delivery_page = delivery_pages[0] if len(delivery_pages) == 1 else None
            review_issues = unique(chain_reasons + ['铭牌尚未确认，关联送货单字段仅列为候选'] +
                [d['review_reason'] for d in delivery_candidates])
            if delivery_page and not delivery_candidates:
                review_issues.append('送货行关键词无法比较；仅定位文档中唯一送货单页面，不确认关联')
            fallback_ev = [dict(page_no=delivery_page, source_role='delivery', precision='page',
                method='unconfirmed_delivery_page_for_review')] if delivery_page else []
            fields['供应单位'] = new_field('', delivery_page,
                [e for d in delivery_candidates for e in company_field(d, 'supplier')['evidence']] or fallback_ev,
                review_issues,
                [candidate(d.get('supplier'), d, d['review_reason'])
                    for d in delivery_candidates if d.get('supplier')])
            fields['送货单编号'] = new_field('', delivery_page,
                [e for d in delivery_candidates for e in evidences(d.get('evidence'))] or fallback_ev,
                review_issues,
                [candidate(n, d, d['review_reason']) for d in delivery_candidates
                    for n in unique([d.get('delivery_no')] + d.get('delivery_number_candidates', []))])
        all_resources = list(plate.get('issues', []) if plate else [])
        if selected_delivery:
            all_resources.extend(selected_delivery.get('issues', []))
        for key in ('生产单位', '供应单位', '炉批号', '送货单编号'):
            if any('truncated' in issue_text(i) or 'repeated' in issue_text(i) for i in all_resources):
                fields[key]['diagnostics'] = unique(fields[key]['diagnostics'] + [issue_text(i) for i in all_resources
                    if 'truncated' in issue_text(i) or 'repeated' in issue_text(i)])
                fields[key]['status'] = 'needs_review'
        complete = all(role in sources for role in ('nameplate', 'delivery', 'certificate')) and all(
            fields[key]['value'] for key in ('生产单位', '供应单位', '送货单编号', '炉批号', '质保书编号'))
        verified_fields = ['生产单位', '供应单位', '送货单编号', '炉批号', '质保书编号']
        if not certificate_key:
            verified_fields.append('规格品种')
        complete = complete and all(fields[key]['status'] == 'observed' for key in verified_fields)
        match_path = ['inspection', 'certificate', 'nameplate', 'delivery'] if certificate_key else [
            'inspection', 'nameplate', 'delivery', 'certificate']
        matched_heat_lots = sorted(set(selected_certificate.get('heat_lots', [])).intersection(
            plate.get('heat_lots', []))) if plate and selected_certificate else []
        if plate:
            sources['nameplate'].update(association_method=(
                'certificate_heat_lot_letter_conflict' if associated_heat_conflict else
                'certificate_heat_lot' if certificate_key else 'entry_grade_and_diameter'),
                matched_heat_lots=matched_heat_lots)
            if associated_heat_conflict:
                sources['nameplate'].update(needs_review=True, conflict=True,
                    certificate_heat_lot=associated_heat_conflict['certificate_heat_lot'],
                    nameplate_heat_lot=associated_heat_conflict['nameplate_heat_lot'])
        if selected_certificate:
            sources['certificate'].update(association_method=(
                'entry_certificate_number_punctuation_review' if punctuation_certificate_reviews else
                'entry_certificate_number_exact' if certificate_key else 'nameplate_heat_lot'),
                matched_heat_lots=matched_heat_lots)
        if plate and plate_review_reasons:
            plate_evidence = evidences(plate.get('evidence')) or [dict(
                page_no=plate['page_no'], source_role='nameplate', precision='page')]
            for key in ('规格品种', '生产单位', '炉批号', '供应单位', '送货单编号'):
                field = fields[key]
                field['diagnostics'] = unique(field.get('diagnostics', []) + plate_review_reasons)
                field['evidence'] = list(field.get('evidence', []))
                for evidence_item in plate_evidence:
                    marker = deepcopy(evidence_item)
                    marker['source_role'] = 'nameplate'
                    if marker not in field['evidence']:
                        field['evidence'].append(marker)
                field['status'] = 'needs_review'
            issues.extend(plate_review_reasons)
        dedup_issues = deepcopy(dedup_diagnostics.get(record['record_id'], []))
        if dedup_issues:
            certificate_field = fields['质保书编号']
            certificate_field['diagnostics'] = list(certificate_field.get('diagnostics', [])) + [
                issue.get('reason', '重复质保书编号已合并') for issue in dedup_issues]
            certificate_field['evidence'] = list(certificate_field.get('evidence', []))
            for issue in dedup_issues:
                for location_item in issue.get('locations', []):
                    if location_item not in certificate_field['evidence']:
                        certificate_field['evidence'].append(deepcopy(location_item))
            certificate_field['status'] = 'needs_review'
            issues.extend(issue.get('reason', '重复质保书编号已合并') for issue in dedup_issues)
        rows.append(dict(record_id=record['record_id'], page_no=page, row_no=record['row_no'], fields=fields,
            entry_specification=entry_specification,
            match_route=route, match_path=match_path, matched_heat_lots=matched_heat_lots,
            match_status='matched_pending_final_review' if complete else 'incomplete_or_ambiguous',
            diagnostics=unique(issue_text(i) for i in issues), sources=sources, reviewed=False,
            deduplication_diagnostics=dedup_issues,
            match_candidates=dict(nameplates=[_plate_summary(p) for p in suggested_plates],
                deliveries=[_delivery_summary(d) for d in delivery_candidates],
                certificate_pages=unique([c['page_no'] for c in certificate_candidates or certs_by_heat] +
                    [c['page'] for c in certificate_review_candidates]),
                review_only_certificates=certificate_review_candidates,
                certificate_number_conflicts=punctuation_certificate_reviews,
                heat_lot_conflicts=heat_review_candidates)))
    return dict(records=rows, nameplate_links=links,
                nameplates=[_plate_summary(p) for p in plates], deliveries=[_delivery_summary(d) for d in deliveries],
                certificates=[dict(page_no=c['page_no'], certificate_numbers=c.get('certificate_numbers', []),
                    heat_lots=c.get('heat_lots', []), issues=c.get('issues', []), evidence=evidences(c.get('evidence')))
                    for c in certificates],
                certificate_number_deduplication=dedup_summary,
                certificate_number_deduplication_diagnostics=dedup_diagnostics)


def compact_paddle(ledger, records):
    positions = {r['record_id']: (r['page_no'], r['row_no']) for r in records}
    result = []
    for source in ledger['records']:
        if source['record_id'] not in positions:
            raise ValueError('Paddle 台账行无法追溯至原清单行：' + source['record_id'])
        page, row_no = positions[source['record_id']]
        fields = {}
        for column in COLUMNS:
            # A CLI automatic ledger uses 供货日期; the current Web normalizes
            # it to the combined 12-column heading. Keep exactly that mapping.
            source_column = ('供货日期' if column == '供货日期与工程材料报审签认日期'
                             and column not in source.get('fields', {}) else column)
            value = source.get('fields', {}).get(source_column, {})
            alt = value.get('alternatives') or value.get('candidates') or []
            fields[column] = new_field(value.get('value'), value.get('page'), value.get('evidence'),
                value.get('diagnostics') or ([] if value else ['该字段未可靠提取']),
                [dict(value=c.get('value', ''), evidence=evidences(c.get('evidence')),
                    page=next((e.get('page_no') for e in c.get('evidence', []) if e.get('page_no')), None))
                    for c in alt if isinstance(c, dict)], status=value.get('status'))
            fields[column]['raw_diagnostics'] = deepcopy(value.get('diagnostics', []))
        result.append(dict(record_id=source['record_id'], page_no=page, row_no=row_no, fields=fields,
            match_route=source.get('match_route'), match_status=source.get('match_status'),
            match_path=deepcopy(source.get('match_path', [])),
            matched_heat_lots=deepcopy(source.get('matched_heat_lots', [])),
            diagnostics=[issue_text(i) for i in source.get('diagnostics', [])],
            raw_diagnostics=deepcopy(source.get('diagnostics', [])),
            match_candidates=deepcopy(source.get('match_candidates', [])),
            reviewed=source.get('reviewed', False),
            sources={key: {k: deepcopy(item[k]) for k in (
                'candidate_id', 'page_no', 'row_id', 'association_method', 'header_only',
                'grade_numeric_key', 'nameplate_grade_raw', 'delivery_grades_raw', 'diameters_mm_raw',
                'delivery_grade_numeric_details',
                'form_id', 'physical_row_index') if k in item}
                for key, item in source.get('sources', {}).items()}))
    return result


def relative_pdf(pdf, output):
    return quote(os.path.relpath(Path(pdf).resolve(), Path(output).resolve()), safe='/')


def project_source(path):
    """Stored run references are relative to the project, not the shell cwd."""
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def validate_glm_sources(resources, digest, pdf_name, page_count, analysis, checkpoint_sources):
    """Reject misfiled resources, mixed PDFs, stale evidence, and wrong pages.

    The resources schema has no top-level PDF identity.  The complete SHA in
    analysis.json plus every real checkpoint reference therefore provides its
    identity; a directory's eight-character suffix alone is insufficient.
    """
    declared = [d for d in analysis.get('documents', [])
                if d.get('pdf_name') == pdf_name and d.get('pdf_sha256') == digest]
    if len(declared) != 1 or declared[0].get('checkpoint_pages') != page_count:
        raise ValueError('GLM 分析声明的 PDF 完整 SHA256 或页数与原文件不一致：' + pdf_name)
    pages = [p.get('page_no') for p in resources.get('pages', [])]
    if sorted(pages) != list(range(1, page_count + 1)):
        raise ValueError('GLM resources 页码集合与原 PDF 不一致：' + pdf_name)
    source_count = 0
    for source in checkpoint_sources.values():
        if source['pdf_sha256'] == digest:
            source_count += 1
            if not isinstance(source['page_no'], int) or not 1 <= source['page_no'] <= page_count:
                raise ValueError('GLM checkpoint 的 PDF 页码无效：' + pdf_name)
    if not source_count:
        raise ValueError('GLM 分析未引用此 PDF 的原始 checkpoint：' + pdf_name)
    evidence_count = 0
    def visit(value):
        nonlocal evidence_count
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            reference = value.get('checkpoint_reference')
            if reference:
                resolved = str(project_source(reference).resolve())
                source = checkpoint_sources.get(resolved)
                if not source or source['pdf_sha256'] != digest:
                    raise ValueError('GLM 字段证据引用了其他 PDF 或未登记的 checkpoint：' + str(reference))
                if value.get('checkpoint_sha256') != source['sha256']:
                    raise ValueError('GLM 字段证据与 checkpoint SHA256 不一致：' + str(reference))
                if value.get('page_no') != source['page_no']:
                    raise ValueError('GLM 字段证据与 checkpoint 页码不一致：' + str(reference))
                evidence_count += 1
            for item in value.values():
                if isinstance(item, (dict, list)):
                    visit(item)
    visit(resources)
    for record in resources.get('records', []):
        if not isinstance(record.get('page_no'), int) or not 1 <= record['page_no'] <= page_count:
            raise ValueError('GLM 清单行来源页码超出 PDF：' + str(record.get('record_id')))
        if not isinstance(record.get('row_no'), int) or record['row_no'] < 1:
            raise ValueError('GLM 清单行号无效：' + str(record.get('record_id')))
    return dict(pdf_sha256=digest, checkpoint_files=source_count,
                evidence_references_checked=evidence_count, pdf_pages=page_count)


def validate_fresh_paddle_manifest(manifest):
    """Require an explicit, completed fresh-run manifest, not an old snapshot."""
    if (manifest.get('source_kind') != 'fresh_paddle_ocr'
            or manifest.get('status') != 'completed'
            or manifest.get('new_ocr_executed') is not True):
        raise ValueError('需要已完成的真实 Paddle OCR 新运行 manifest，不能使用历史台账或证据重放')
    if not manifest.get('volumes'):
        raise ValueError('Paddle 新运行 manifest 缺少文档')


def validate_fresh_paddle_run(volume, metadata, digest, ledger_path, records_path, metadata_path):
    """Tie result files to a completed OCR run and the exact source PDF."""
    run_id = volume.get('paddle_run_id')
    if not run_id or metadata.get('run_id') != run_id:
        raise ValueError('Paddle 新运行 ID 与运行元数据不一致：' + volume['pdf_name'])
    if (metadata.get('status') != 'completed' or metadata.get('new_ocr_executed') is not True
            or metadata.get('exit_code') != 0):
        raise ValueError('Paddle 运行未成功完成真实 OCR：' + volume['pdf_name'])
    if digest not in (metadata.get('data_version') or {}).values():
        raise ValueError('Paddle 运行元数据未记录对应 PDF SHA256：' + volume['pdf_name'])
    # Metadata lives at <run>/experiment/metadata.json. A fresh manifest may
    # not pair fresh metadata with the old Web ledger outside that run.
    run_root = project_source(metadata_path).resolve().parent.parent
    for path in (ledger_path, records_path):
        if not project_source(path).resolve().is_relative_to(run_root):
            raise ValueError('Paddle 台账/清单不在对应新运行目录内：' + volume['pdf_name'])
    return dict(run_id=run_id, status='completed', new_ocr_executed=True,
                pdf_sha256=digest, metadata_path=str(project_source(metadata_path).resolve()))


def validate_replayed_paddle_run(volume, metadata, replay, ocr_metadata, digest):
    """Validate a new matching run while keeping its original OCR attribution."""
    run_id = volume.get('paddle_run_id')
    if not run_id or metadata.get('run_id') != run_id or replay.get('run_id') != run_id:
        raise ValueError('Paddle 匹配回放 ID 不一致')
    if (metadata.get('status') != 'completed' or metadata.get('exit_code') != 0 or
            metadata.get('new_ocr_executed') is not False or
            replay.get('status') != 'completed' or replay.get('new_ocr_executed') is not False):
        raise ValueError('Paddle 匹配回放未完成或错误标记为新 OCR')
    if (ocr_metadata.get('status') != 'completed' or ocr_metadata.get('exit_code') != 0 or
            ocr_metadata.get('new_ocr_executed') is not True or
            ocr_metadata.get('run_id') != volume.get('paddle_ocr_run_id') or
            ocr_metadata.get('run_id') != replay.get('source_ocr_run_id')):
        raise ValueError('Paddle 回放缺少已完成的原始 OCR 来源')
    if (replay.get('pdf_sha256') != digest or
            digest not in (metadata.get('data_version') or {}).values() or
            digest not in (ocr_metadata.get('data_version') or {}).values()):
        raise ValueError('Paddle 回放与原始 OCR 的 PDF 身份不一致')
    paths = volume['paths']
    ledger_path = project_source(paths['ledger']['path']).resolve()
    run_root = project_source(paths['run_metadata']['path']).resolve().parent
    if not ledger_path.is_relative_to(run_root):
        raise ValueError('Paddle 回放台账不在对应匹配运行目录内')
    for key in ('source_pdf', 'records', 'ocr_run_metadata'):
        ref = replay.get('inputs', {}).get(key, {})
        if (project_source(ref.get('path', '')).resolve() != project_source(paths[key]['path']).resolve()
                or ref.get('sha256') != paths[key].get('sha256')):
            raise ValueError('Paddle 回放来源引用不一致：' + key)
    if (replay.get('ledger_reference', {}).get('sha256') != paths['ledger'].get('sha256') or
            project_source(replay.get('ledger_reference', {}).get('path', '')).resolve() != ledger_path):
        raise ValueError('Paddle 回放台账与运行产物不一致')
    return dict(run_id=run_id, status='completed', new_ocr_executed=False,
                source_ocr_run_id=ocr_metadata['run_id'], pdf_sha256=digest)


def build_comparison(analysis_root, baseline_path, output, *, require_fresh_paddle=False,
                     require_replayed_paddle=False):
    analysis_root, baseline_path = Path(analysis_root).resolve(), Path(baseline_path).resolve()
    analysis = read_json(analysis_root / 'analysis.json')
    baseline = read_json(baseline_path)
    fresh_paddle = baseline.get('source_kind') == 'fresh_paddle_ocr'
    replayed_paddle = baseline.get('source_kind') == 'paddle_matching_replay'
    if require_fresh_paddle or fresh_paddle:
        validate_fresh_paddle_manifest(baseline)
    if require_replayed_paddle or replayed_paddle:
        if (not replayed_paddle or baseline.get('status') != 'completed' or
                baseline.get('new_ocr_executed') is not False or not baseline.get('volumes')):
            raise ValueError('需要已完成且明确未重新 OCR 的 Paddle 匹配回放来源')
    references = {}
    def read_ref(path, expected=None):
        path = project_source(path)
        actual = sha256(path)
        if expected and actual != expected:
            raise ValueError('缓存内容与来源 SHA256 不一致：' + str(path))
        references[str(path.resolve())] = dict(path=str(path.resolve()), sha256=actual, bytes=path.stat().st_size)
        return read_json(path)
    read_ref(baseline_path)
    read_ref(analysis_root / 'analysis.json')
    checkpoints = defaultdict(list)
    checkpoint_sources = {}
    for item in analysis.get('source_checkpoints', []):
        cp = read_ref(item['path'], item.get('sha256'))
        reference = str(project_source(item['path']).resolve())
        checkpoint_sources[reference] = dict(pdf_sha256=cp.get('pdf_sha256'),
            page_no=cp.get('page_no'), sha256=references[reference]['sha256'])
        if cp.get('raw_text'):
            cp['checkpoint_reference'] = reference
            cp['checkpoint_sha256'] = references[cp['checkpoint_reference']]['sha256']
            checkpoints[cp['pdf_sha256']].append(cp)
    documents = []
    for volume in baseline['volumes']:
        digest = volume['paths']['source_pdf']['sha256']
        resource_path = analysis_root / (volume['pdf_name'] + '-' + digest[:8]) / 'resources.json'
        if not resource_path.is_file():
            raise ValueError('缺少对应 PDF 的 GLM resources.json：' + str(resource_path))
        resource = read_ref(resource_path)
        glm_source_validation = validate_glm_sources(resource, digest, volume['pdf_name'],
            volume['page_count'], analysis, checkpoint_sources)
        ledger_ref, records_ref = (volume['paths'][key] for key in ('ledger', 'records'))
        ledger = read_ref(ledger_ref['path'], ledger_ref.get('sha256'))
        records = read_ref(records_ref['path'], records_ref.get('sha256'))
        paddle_source_validation = None
        if fresh_paddle:
            metadata_ref = volume['paths'].get('run_metadata')
            if not metadata_ref or not metadata_ref.get('sha256'):
                raise ValueError('Paddle 新运行缺少带 SHA256 的运行元数据引用：' + volume['pdf_name'])
            metadata = read_ref(metadata_ref['path'], metadata_ref['sha256'])
            paddle_source_validation = validate_fresh_paddle_run(volume, metadata, digest,
                ledger_ref['path'], records_ref['path'], metadata_ref['path'])
        elif replayed_paddle:
            refs = volume['paths']
            metadata = read_ref(refs['run_metadata']['path'], refs['run_metadata']['sha256'])
            replay = read_ref(refs['replay_metadata']['path'], refs['replay_metadata']['sha256'])
            ocr_metadata = read_ref(refs['ocr_run_metadata']['path'], refs['ocr_run_metadata']['sha256'])
            paddle_source_validation = validate_replayed_paddle_run(volume, metadata, replay, ocr_metadata, digest)
            for key, item in replay['inputs'].items():
                if key == 'source_pdf':
                    continue
                read_ref(item['path'], item['sha256'])
        pdf_path = project_source(volume['paths']['source_pdf']['path'])
        if sha256(pdf_path) != digest or ledger['pdf_sha256'] != digest or records['pdf_sha256'] != digest:
            raise ValueError('PDF、清单和台账来源不一致：' + volume['pdf_name'])
        references[str(pdf_path.resolve())] = dict(path=str(pdf_path.resolve()), sha256=digest, bytes=pdf_path.stat().st_size)
        glm = build_glm_ledger(resource, checkpoints[digest])
        paddle = compact_paddle(ledger, records['records'])
        for row in paddle:
            if not isinstance(row['page_no'], int) or not 1 <= row['page_no'] <= volume['page_count']:
                raise ValueError('Paddle 清单行来源页码超出 PDF：' + row['record_id'])
            if not isinstance(row['row_no'], int) or row['row_no'] < 1:
                raise ValueError('Paddle 清单行号无效：' + row['record_id'])
        grouped = {'paddle': {}, 'glm': {}}
        for model, rows in [('paddle', paddle), ('glm', glm['records'])]:
            for row in rows:
                key = (row['page_no'], row['row_no'])
                if key in grouped[model]:
                    raise ValueError(f'{model} 同一页/清单行存在多个解析行，无法静默对齐：{key}')
                grouped[model][key] = row
        aligned = []
        for page, row_no in sorted(set(grouped['paddle']) | set(grouped['glm'])):
            key = (page, row_no)
            pair = {model: grouped[model].get(key) for model in ('paddle', 'glm')}
            difference = [column for column in COLUMNS if canonical(
                (pair['paddle'] or {}).get('fields', {}).get(column, {}).get('value'), column) != canonical(
                (pair['glm'] or {}).get('fields', {}).get(column, {}).get('value'), column)]
            aligned.append(dict(alignment_id=f'p{page:03d}-row-{row_no}', page_no=page, row_no=row_no,
                                difference_columns=difference, **pair))
        document_metrics = dict(physical_rows=len(aligned), rows_with_value_differences=sum(bool(r['difference_columns']) for r in aligned),
            value_difference_fields=sum(len(r['difference_columns']) for r in aligned),
            certificate_number_deduplication=glm.get('certificate_number_deduplication'),
            models={})
        for model, rows in [('paddle', paddle), ('glm', glm['records'])]:
            document_metrics['models'][model] = dict(rows=len(rows),
                match_status_counts=dict(Counter(r['match_status'] for r in rows)),
                match_route_counts=dict(Counter(r.get('match_route') for r in rows)),
                four_form_linked_rows=sum(all(role in r.get('sources', {}) for role in
                    ('certificate', 'nameplate', 'delivery')) for r in rows),
                source_link_counts={role: sum(role in r.get('sources', {}) for r in rows)
                    for role in ('certificate', 'nameplate', 'delivery')},
                field_coverage={column: dict(nonempty=sum(bool(r['fields'][column]['value']) for r in rows),
                    needs_review=sum(r['fields'][column]['status'] != 'observed' for r in rows)) for column in COLUMNS})
        duplicates = defaultdict(list)
        for record in resource.get('records', []):
            key = tuple(canonical(record.get(k)) for k in ('row_no', 'material_name', 'grade', 'diameter_mm', 'weight_t', 'certificate_no', 'manufacturer'))
            duplicates[key].append(dict(page_no=record['page_no'], row_no=record['row_no']))
        documents.append(dict(pdf_name=volume['pdf_name'], pdf_sha256=digest, pdf_path=str(pdf_path.resolve()),
            pdf_href=relative_pdf(pdf_path, output), page_count=volume['page_count'],
            rows=aligned, metrics=document_metrics,
            duplicate_source_rows=[items for items in duplicates.values() if len(items) > 1],
            certificate_number_deduplication=glm.get('certificate_number_deduplication'),
            glm_resources={key: glm[key] for key in ('nameplates', 'deliveries', 'certificates', 'nameplate_links')},
            baseline_run_id=volume.get('paddle_run_id') if fresh_paddle or replayed_paddle else volume.get('baseline_run_id'),
            paddle_run_id=volume.get('paddle_run_id') if fresh_paddle or replayed_paddle else volume.get('baseline_run_id'),
            paddle_ocr_run_id=volume.get('paddle_ocr_run_id') if replayed_paddle else volume.get('paddle_run_id'),
            paddle_source_validation=paddle_source_validation, paddle_ledger_path=ledger_ref['path'],
            glm_resources_path=str(resource_path), glm_source_validation=glm_source_validation))
    return dict(schema_version='ocr-ledger-comparison-v1', status='completed',
        analysis_type=('cached_models_current_matching' if replayed_paddle else
                       'fresh_paddle_vs_cached_glm' if fresh_paddle else 'existing_outputs_only'),
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        columns=COLUMNS, models=REPLAY_MODEL_NAMES if replayed_paddle else FRESH_MODEL_NAMES if fresh_paddle else MODEL_NAMES,
        notes=([REPLAY_COMPARISON_NOTE, *NOTES[1:]] if replayed_paddle else
               [FRESH_COMPARISON_NOTE, *NOTES[1:]] if fresh_paddle else NOTES),
        new_ocr_executed=False, source_new_ocr_executed={'paddle': fresh_paddle, 'glm': False},
        images_saved=0,
        approval_status='experimental_comparison_pending_human_review',
        source_manifest=list(references.values()), documents=documents)


HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paddle / GLM 台账对比</title>
<style>
*{box-sizing:border-box}body{font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#f4f6f9;color:#273245}
header{padding:22px 28px;background:#152a46;color:white}h1{font-size:23px;margin:0 0 5px}header p{margin:0;opacity:.8}
main{padding:18px 24px}section{background:white;border:1px solid #dae1eb;border-radius:8px;padding:16px;margin:12px 0}
.toolbar{display:flex;gap:16px;align-items:center;flex-wrap:wrap}select,button{padding:8px 12px;border:1px solid #b7c3d4;border-radius:5px;background:white}
.note{font-size:13px;color:#5f6c7d}.summary{display:flex;gap:12px;flex-wrap:wrap;margin-top:12px}.stat{background:#edf3fc;padding:8px 13px;border-radius:5px}
.scroll{overflow:auto;max-height:70vh}table{border-collapse:separate;border-spacing:0;width:100%;font-size:13px}th,td{border-right:1px solid #dbe1e9;border-bottom:1px solid #dbe1e9;padding:8px;vertical-align:top;min-width:138px;white-space:normal}th{background:#e9eff7;position:sticky;top:0;z-index:2;text-align:left}th:first-child,td:first-child{min-width:105px;border-left:1px solid #dbe1e9}thead tr:first-child th{border-top:1px solid #dbe1e9}
td.diff{background:#fff6cf}.value{padding:5px;border-radius:4px;min-height:50px;overflow-wrap:anywhere}.value+.value{border-top:1px dashed #bac5d3;margin-top:6px;padding-top:8px}.value.bad{color:#b12625;background:#fff0ef}.missing{font-style:italic}.tag{display:inline-block;font-size:10px;padding:0 4px;border-radius:3px;background:#d9e8ff;color:#1a50a0;margin-right:4px}.tag.glm{background:#dcf1e6;color:#116845}.loc{font-size:11px;margin-left:5px;color:#315f9a}details{font-size:12px;color:#617086;margin-top:4px}summary{cursor:pointer}details pre{white-space:pre-wrap;max-width:550px;overflow-wrap:anywhere;font:inherit}a{color:#275ea4}h2{margin:0 0 10px;font-size:17px}.candidate-table{max-height:50vh}.candidate-table td{min-width:120px}.empty{color:#8a96a6}.badge{font-size:11px;background:#fff2d5;color:#805600;padding:3px 6px;border-radius:3px}
</style>
<header><h1>Paddle / GLM 台账对比</h1><p>当前前端同款 12 列 · 保留原始清单行 · 红色为缺失或待核验，黄色为两模型值不同</p></header>
<main><section><div class="toolbar"><label>文件 <select id="document"></select></label><label>显示 <select id="mode"><option value="both">两模型并排</option><option value="glm">__MODEL_GLM__</option><option value="paddle">__MODEL_PADDLE__</option></select></label><label><input type="checkbox" id="diffOnly"> 仅看值不同的行</label><a href="ocr_ledger_comparison.xlsx">下载 Excel 对比</a></div><div id="summary" class="summary"></div><details><summary>比较范围和限制</summary><ul id="notes"></ul></details></section>
<section><h2>按原清单页码和行号对齐的台账</h2><p class="note">每格显示各自 OCR 的原值；点击页码查看原 PDF，展开“核验”查看原因与候选。表格保留重复页，不重复累计数量。</p><div class="scroll" id="ledger"></div></section>
<section><h2>炉批号字母差异核验候选</h2><p class="note">仅显示单字母差异；唯一候选会自动关联并写入铭牌炉批号，同时标红矛盾，多个候选仍仅供人工核验。保留双方原文和差异位置，点击证书页、铭牌页可分别查看原 PDF。</p><div class="scroll candidate-table" id="heat-reviews"></div></section>
<section><h2>GLM 铭牌与送货单候选</h2><p class="note">这里展示 OCR 读到的公司、炉批号和关联证据。清单有证号时可沿证书炉号链关联铭牌；没有证号时需要清单牌号和规格同时匹配。</p><div class="scroll candidate-table" id="links"></div></section>
<section><h2>GLM 送货单识别明细</h2><div class="scroll candidate-table" id="deliveries"></div></section>
<section><h2>GLM 质量证明书识别明细</h2><div class="scroll candidate-table" id="certificates"></div></section>
<p class="note" id="provenance"></p></main>
<script id="comparison-data" type="application/json">__DATA__</script>
<script>
const report=JSON.parse(document.getElementById('comparison-data').textContent);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const docSelect=document.getElementById('document'),mode=document.getElementById('mode');
docSelect.innerHTML=report.documents.map((d,i)=>`<option value="${i}">${esc(d.pdf_name)}</option>`).join('');
for (const model of ['paddle','glm']) mode.querySelector(`option[value="${model}"]`).textContent=report.models[model];
document.getElementById('notes').innerHTML=report.notes.map(n=>`<li>${esc(n)}</li>`).join('');
function loc(d,page){return page?`<a class="loc" href="${esc(d.pdf_href)}#page=${Number(page)}" target="_blank" rel="noopener">p${Number(page)} ↗</a>`:''}
function details(data,label='核验 / 候选'){return data?`<details><summary>${esc(label)}</summary><pre>${esc(typeof data==='string'?data:JSON.stringify(data,null,2))}</pre></details>`:''}
function cell(d,f,model){if(!f)return `<div class="value bad" data-model="${esc(model)}"><span class="field-text missing">本模型无此清单行</span></div>`;let reasons=f.diagnostics||[];let bad=!f.value||f.status!=='observed'||reasons.length;let hints=!f.value&&(f.candidates||[]).length?`<div class="candidate-hints note">候选：${f.candidates.slice(0,3).map(c=>`${esc(c.value||'未识别')}${loc(d,c.page)}`).join('；')}${f.candidates.length>3?'…':''}</div>`:'';return `<div class="value ${bad?'bad':''}" data-model="${esc(model)}"><span class="tag ${model}">${model==='glm'?'GLM':'Paddle'}</span><span class="field-text ${f.value?'':'missing'}">${esc(f.value||'未识别 / 未确认')}</span>${loc(d,f.page)}${hints}${(reasons.length||f.candidates?.length)?details({原因:reasons,候选:f.candidates||[],证据:f.evidence||[]}):''}</div>`}
function table(headers,rows){return `<table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`}
function heatReviews(row){let choices=row?.match_candidates||[];if(!Array.isArray(choices))choices=choices.heat_lot_conflicts||[];return choices.filter(c=>c.kind==='certificate_nameplate_heat_conflict').flatMap(c=>(c.heat_lot_differences||[c.heat_difference]).filter(Boolean).map(diff=>({candidate:c,difference:diff})))}
function render(){let d=report.documents[Number(docSelect.value)],models=mode.value==='both'?['paddle','glm']:[mode.value],rows=d.rows.filter(r=>!document.getElementById('diffOnly').checked||r.difference_columns.length);let heads=['清单来源',...report.columns];
document.getElementById('ledger').innerHTML=table(heads,rows.map(r=>`<tr><td><strong>第 ${r.page_no} 页<br>第 ${r.row_no} 行</strong>${loc(d,r.page_no)}${models.map(m=>details({模型:report.models[m],匹配状态:r[m]?.match_status,匹配路径:r[m]?.match_path,已匹配炉批号:r[m]?.matched_heat_lots,异常:r[m]?.diagnostics,原始诊断:r[m]?.raw_diagnostics,匹配候选:r[m]?.match_candidates,来源:r[m]?.sources},m==='glm'?'GLM 匹配详情':'Paddle 匹配详情')).join('')}</td>${report.columns.map(c=>`<td data-field="${esc(c)}" class="${r.difference_columns.includes(c)?'diff':''}">${models.map(m=>cell(d,r[m]?.fields[c],m)).join('')}</td>`).join('')}</tr>`));
document.getElementById('heat-reviews').innerHTML=table(['清单来源 / 模型','证书炉批号原文 / 页码','铭牌炉批号原文 / 页码','差异及核验原因'],rows.flatMap(r=>models.flatMap(m=>heatReviews(r[m]).map(({candidate:c,difference:x})=>`<tr><td>第${r.page_no}页第${r.row_no}行<br>${esc(report.models[m])}</td><td>${esc(x.certificate_value)}${loc(d,c.certificate?.page_no)}</td><td>${esc(x.nameplate_value)}${loc(d,c.nameplate?.page_no)}</td><td><span class="badge">${c.associated?'已自动关联；炉批号矛盾待人工核验':'候选，待人工核验'}</span><br>${esc(x.description)}<br>${esc(c.reason)}${details({证据:c.locations,差异:x})}</td></tr>`))));
let p=d.metrics.models.paddle.field_coverage,g=d.metrics.models.glm.field_coverage;document.getElementById('summary').innerHTML=[`清单物理行 ${d.metrics.physical_rows}`,`值不同 ${d.metrics.rows_with_value_differences} 行`,`四表已关联 Paddle ${d.metrics.models.paddle.four_form_linked_rows||0} / GLM ${d.metrics.models.glm.four_form_linked_rows||0}`,`台账生产单位 Paddle ${p['生产单位'].nonempty} / GLM ${g['生产单位'].nonempty}`,`台账供应单位 Paddle ${p['供应单位'].nonempty} / GLM ${g['供应单位'].nonempty}`,`GLM铭牌顶部公司已读 ${d.glm_resources.nameplates.filter(x=>x.manufacturer.value).length}/${d.glm_resources.nameplates.length}`,`GLM送货单顶部公司已读 ${d.glm_resources.deliveries.filter(x=>x.supplier.value).length}/${d.glm_resources.deliveries.length}`,`疑似重复组 ${d.duplicate_source_rows.length}`].map(v=>`<span class="stat">${esc(v)}</span>`).join('');
document.getElementById('links').innerHTML=table(['铭牌页 / 牌号规格','生产单位（顶部）','炉批号','送货单候选','供应单位（顶部）','关联说明 / 异常'],d.glm_resources.nameplate_links.map(p=>`<tr><td>${loc(d,p.page_no)}<br>${esc(p.grade||'牌号缺失')} / Φ${esc(p.diameter_mm||'?')}</td><td>${cell(d,p.manufacturer,'glm')}</td><td>${esc(p.heat_lots.join('、')||'未确认')}</td><td>${p.delivery_candidates.map(x=>`${esc(x.delivery_no||'单号缺失')}${loc(d,x.page_no)}`).join('<br>')||'<span class="empty">无精确候选</span>'}</td><td>${p.delivery_candidates.map(x=>cell(d,x.supplier,'glm')).join('')||'—'}</td><td><span class="badge">候选，待核验</span><br>${esc(p.association_reason)}${details({异常:p.issues,定位:p.evidence})}</td></tr>`));
document.getElementById('deliveries').innerHTML=table(['页码','送货单编号','供应单位（顶部）','货物行关键词','异常 / 候选'],d.glm_resources.deliveries.map(x=>`<tr><td>${loc(d,x.page_no)}</td><td>${esc(x.delivery_no||'未识别')}${details(x.delivery_number_candidates?.length?x.delivery_number_candidates:null,'编号候选')}</td><td>${cell(d,x.supplier,'glm')}</td><td>${x.rows.map(r=>`${esc(r.grade||'牌号未识别')} / Φ${esc(r.diameter_mm||'?')}`).join('<br>')||'行未识别'}</td><td>${details({异常:x.issues,货物行:x.rows,证据:x.evidence},'展开原始关键词 / 来源')}</td></tr>`));
document.getElementById('certificates').innerHTML=table(['页码','质量证明书编号','炉批号','异常 / 定位'],d.glm_resources.certificates.map(c=>`<tr><td>${loc(d,c.page_no)}</td><td>${esc(c.certificate_numbers.join('、')||'未识别')}</td><td>${esc(c.heat_lots.join('、')||'未识别')}</td><td>${details({异常:c.issues,证据:c.evidence})}</td></tr>`));
document.getElementById('provenance').textContent=`来源 PDF SHA256：${d.pdf_sha256}；Paddle 运行：${d.paddle_run_id||d.baseline_run_id}；${report.analysis_type==='fresh_paddle_vs_cached_glm'?'Paddle 为本次真实重新 OCR；GLM 为原始缓存重解析。':report.analysis_type==='cached_models_current_matching'?'两模型均复用原始 OCR，按本次双路线重新匹配。':'仅分析已有缓存。'}比较导出本身未运行 OCR，新增图片 0 张。`;
}
[docSelect,mode,document.getElementById('diffOnly')].forEach(el=>el.addEventListener('change',render));render();
</script></html>'''


def write_html(path, report):
    payload = json.dumps(report, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    # Keep the initial markup accurate as well as the JS-populated options.
    # This avoids even a transient historical label in a fresh comparison.
    models = report.get('models', MODEL_NAMES)
    markup = HTML_TEMPLATE.replace('__MODEL_GLM__', escape(models['glm'])).replace(
        '__MODEL_PADDLE__', escape(models['paddle']))
    Path(path).write_text(markup.replace('__DATA__', payload), encoding='utf-8')


def write_xlsx(path, report):
    import xlsxwriter
    with xlsxwriter.Workbook(path, {'strings_to_formulas': False, 'strings_to_urls': False}) as book:
        book.set_properties({'title': 'Paddle / GLM OCR 台账实验对比',
            'comments': '待人工核验的实验比较，不是已审核正式台账。仅引用原 PDF，不复制图片。'})
        base = dict(font_name='Microsoft YaHei', font_size=10, text_wrap=True, valign='top', border=1)
        formats = {key: book.add_format(dict(base, **extra)) for key, extra in {
            'normal': {}, 'header': dict(bold=True, bg_color='#E9EFF7'),
            'bad': dict(font_color='#B12625', bg_color='#FFF0EF'),
            'diff': dict(bg_color='#FFF6CF'), 'bad_diff': dict(font_color='#B12625', bg_color='#FFF0CF')}.items()}
        note = book.add_format(dict(font_name='Microsoft YaHei', font_color='#59697E', text_wrap=True, valign='top'))
        intro = book.add_worksheet('比较说明')
        intro.set_column(0, 0, 115)
        intro.write(0, 0, 'OCR 台账对比：实验结果，待人工核验', formats['header'])
        for i, text in enumerate(report['notes'], 1):
            intro.write_string(i, 0, text, note); intro.set_row(i, 40)
        intro.write_string(len(report['notes']) + 2, 0, '生成时间 UTC：' + report['created_at_utc'], note)
        for i, document in enumerate(report['documents'], len(report['notes']) + 4):
            intro.write_string(i, 0, document['pdf_name'] + '；Paddle 运行：' + str(
                document.get('paddle_run_id') or document.get('baseline_run_id') or '未登记'), note)
        overview = book.add_worksheet('模型汇总')
        overview_fields = ['生产单位', '供应单位', '送货单编号', '炉批号', '验收数量', '质保书编号']
        overview_headers = ['文件', '模型', '对齐物理清单行数', '本模型清单行数',
                            *[column + '非空数' for column in overview_fields], '匹配状态统计']
        overview.merge_range(0, 0, 0, len(overview_headers)-1,
            '模型台账覆盖汇总（非准确率）', formats['header'])
        overview.merge_range(1, 0, 1, len(overview_headers)-1,
            '直接引用比较指标；非空数表示提取覆盖，包含待核验值。未建立人工真值，不能据此判断准确率。重复物理行保留。', note)
        overview.write_row(2, 0, overview_headers, formats['header'])
        overview.set_row(1, 30); overview.set_row(2, 40)
        overview.set_column(0, 1, 32); overview.set_column(2, 9, 16); overview.set_column(10, 10, 58)
        overview.freeze_panes(3, 2)
        overview_index = 3
        for document in report['documents']:
            for model in ('paddle', 'glm'):
                metrics = document['metrics']['models'][model]
                overview.write_row(overview_index, 0, [document['pdf_name'], report['models'][model],
                    document['metrics']['physical_rows'], metrics['rows'],
                    *[metrics['field_coverage'][column]['nonempty'] for column in overview_fields],
                    json.dumps(metrics['match_status_counts'], ensure_ascii=False)], formats['normal'])
                overview_index += 1
        overview.autofilter(2, 0, overview_index - 1, len(overview_headers)-1)
        provenance = book.add_worksheet('字段溯源')
        source_headers = ['文件', '模型', '清单页', '清单行', '字段', 'OCR 值', '状态', '字段页', '异常原因', '候选', '证据', 'PDF SHA256']
        provenance.write_row(0, 0, source_headers, formats['header']); provenance.freeze_panes(1, 5)
        provenance.set_column(0, 1, 30); provenance.set_column(2, 4, 16); provenance.set_column(5, 11, 37)
        candidate_sheet = book.add_worksheet('GLM候选与关联')
        candidate_headers = ['文件', '类型', '页码', '牌号', '直径', '生产单位/供应单位', '炉批号/单号', '关联说明', '异常', '候选及证据']
        candidate_sheet.write_row(0, 0, candidate_headers, formats['header'])
        candidate_sheet.set_column(0, 0, 30); candidate_sheet.set_column(1, 4, 16); candidate_sheet.set_column(5, 9, 42)
        candidate_sheet.freeze_panes(1, 3)
        paddle_candidates = book.add_worksheet('Paddle匹配候选')
        paddle_headers = ['文件', '清单页', '清单行', '记录ID', '类型', '候选/诊断序号',
                          '相关页码', '原因', 'JSON分段', '候选/原始诊断JSON', 'PDF SHA256']
        paddle_candidates.write_row(0, 0, paddle_headers, formats['header'])
        paddle_candidates.set_column(0, 0, 30); paddle_candidates.set_column(1, 2, 12)
        paddle_candidates.set_column(3, 6, 22); paddle_candidates.set_column(7, 7, 55)
        paddle_candidates.set_column(8, 8, 12); paddle_candidates.set_column(9, 10, 70)
        paddle_candidates.freeze_panes(1, 4)
        paddle_index = 1
        heat_reviews = book.add_worksheet('炉批号核验候选')
        heat_headers = ['文件', '模型', '清单页', '清单行', '质保书编号', '证书页',
                        '证书炉批号原文', '铭牌页', '铭牌炉批号原文', '字母差异', '状态', '具体原因']
        heat_reviews.write_row(0, 0, heat_headers, formats['header'])
        heat_reviews.set_column(0, 1, 30); heat_reviews.set_column(2, 5, 14)
        heat_reviews.set_column(6, 9, 25); heat_reviews.set_column(10, 10, 28)
        heat_reviews.set_column(11, 11, 85); heat_reviews.freeze_panes(1, 5)
        heat_index = 1
        def candidate_pages(value):
            pages = set()
            def visit(item):
                if isinstance(item, dict):
                    page = item.get('page_no')
                    if isinstance(page, int):
                        pages.add(page)
                    for child in item.values():
                        visit(child)
                elif isinstance(item, list):
                    for child in item:
                        visit(child)
            visit(value)
            return '、'.join(str(page) for page in sorted(pages))
        source_index = candidate_index = 1
        for doc_index, document in enumerate(report['documents'], 1):
            for pair in document['rows']:
                for model in ('paddle', 'glm'):
                    row = pair.get(model) or {}
                    choices = row.get('match_candidates', [])
                    choices = choices.get('heat_lot_conflicts', []) if isinstance(choices, dict) else choices
                    for item in choices:
                        if item.get('kind') != 'certificate_nameplate_heat_conflict':
                            continue
                        differences = item.get('heat_lot_differences', []) or [item.get('heat_difference')]
                        for difference in filter(None, differences):
                            heat_reviews.write_row(heat_index, 0, [document['pdf_name'], report['models'][model],
                                pair['page_no'], pair['row_no'], row.get('fields', {}).get('质保书编号', {}).get('value', ''),
                                item.get('certificate', {}).get('page_no', ''), difference['certificate_value'],
                                item.get('nameplate', {}).get('page_no', ''), difference['nameplate_value'],
                                difference['description'],
                                ('已自动关联；炉批号矛盾待人工核验' if item.get('associated')
                                 else '候选，待人工核验；未自动匹配'), item.get('reason', '')], formats['bad'])
                            heat_index += 1
                row = pair.get('paddle')
                if not row:
                    continue
                for group, items in [('匹配候选', row.get('match_candidates', [])),
                                     ('原始诊断', row.get('raw_diagnostics', []))]:
                    for item_index, item in enumerate(items, 1):
                        reason = (item.get('reason') or item.get('association_reason') or ''
                                  if isinstance(item, dict) else str(item))
                        kind = (item.get('kind') or item.get('code') or group
                                if isinstance(item, dict) else group)
                        payload = json.dumps(item, ensure_ascii=False, indent=2)
                        # Excel has a 32,767-character cell limit. Retain every
                        # byte of the JSON across ordered, explicitly numbered
                        # chunks rather than silently truncating large evidence.
                        chunks = [payload[start:start + 30000] for start in range(0, len(payload), 30000)]
                        for chunk_index, chunk in enumerate(chunks, 1):
                            paddle_candidates.write_row(paddle_index, 0, [document['pdf_name'], pair['page_no'],
                                pair['row_no'], row['record_id'], group + '：' + str(kind), item_index,
                                candidate_pages(item), str(reason)[:30000], f'{chunk_index}/{len(chunks)}', chunk,
                                document['pdf_sha256']], formats['normal'])
                            paddle_index += 1
            for model in ('paddle', 'glm'):
                title = f'{doc_index}-' + ('Paddle' if model == 'paddle' else 'GLM')
                sheet = book.add_worksheet(title)
                sheet.merge_range(0, 0, 0, len(COLUMNS)-1, document['pdf_name'] + ' · ' + report['models'][model] + ' · 待核验', formats['header'])
                sheet.merge_range(1, 0, 1, len(COLUMNS)-1, '红色：缺失/待核验；黄色：两模型值不同。单元格批注保留页码、异常、候选；详见字段溯源。', note)
                sheet.write_row(2, 0, COLUMNS, formats['header']); sheet.freeze_panes(3, 4)
                sheet.set_row(1, 32); sheet.set_row(2, 42); sheet.set_column(0, 0, 15); sheet.set_column(1, 11, 25)
                for row_index, pair in enumerate(document['rows'], 3):
                    row = pair.get(model)
                    sheet.set_row(row_index, 65)
                    for column_index, column in enumerate(COLUMNS):
                        f = (row or {}).get('fields', {}).get(column, new_field('', pair['page_no'], issues=['本模型无此清单行']))
                        bad = not f['value'] or f['status'] != 'observed' or bool(f.get('diagnostics'))
                        diff = column in pair['difference_columns']
                        fmt = formats['bad_diff' if bad and diff else 'bad' if bad else 'diff' if diff else 'normal']
                        sheet.write_string(row_index, column_index, f['value'], fmt)
                        comment = dict(清单来源=f"第{pair['page_no']}页第{pair['row_no']}行", 字段页=f.get('page'),
                            匹配状态=(row or {}).get('match_status'), 匹配路径=(row or {}).get('match_path'), 已匹配炉批号=(row or {}).get('matched_heat_lots'), 异常=f.get('diagnostics'), 候选=f.get('candidates'),
                            原始诊断=f.get('raw_diagnostics'), 证据=f.get('evidence'), PDF=document['pdf_path'])
                        sheet.write_comment(row_index, column_index, json.dumps(comment, ensure_ascii=False, indent=2)[:32000])
                        provenance.write_row(source_index, 0, [document['pdf_name'], report['models'][model], pair['page_no'], pair['row_no'],
                            column, f['value'], f['status'], f.get('page') or '', '\n'.join(f['diagnostics']),
                            json.dumps(f.get('candidates'), ensure_ascii=False)[:32000], json.dumps(f.get('evidence'), ensure_ascii=False)[:32000],
                            document['pdf_sha256']], formats['bad'] if bad else formats['normal'])
                        source_index += 1
                sheet.autofilter(2, 0, len(document['rows']) + 2, len(COLUMNS)-1)
                sheet.set_landscape(); sheet.fit_to_pages(1, 0); sheet.repeat_rows(0, 2)
            for kind, items in [('铭牌关联候选', document['glm_resources']['nameplate_links']), ('送货单', document['glm_resources']['deliveries'])]:
                for item in items:
                    company = item.get('manufacturer') or item.get('supplier') or {}
                    candidate_sheet.write_row(candidate_index, 0, [document['pdf_name'], kind, item['page_no'], item.get('grade', ''),
                        item.get('diameter_mm', ''), company.get('value', ''), '、'.join(item.get('heat_lots', [])) or item.get('delivery_no', ''),
                        item.get('association_reason', ''), '\n'.join(issue_text(i) for i in item.get('issues', [])),
                        json.dumps(item, ensure_ascii=False)[:32000]], formats['normal'])
                    candidate_index += 1
        provenance.autofilter(0, 0, source_index - 1, len(source_headers)-1)
        candidate_sheet.autofilter(0, 0, candidate_index - 1, len(candidate_headers)-1)
        paddle_candidates.autofilter(0, 0, max(1, paddle_index - 1), len(paddle_headers)-1)
        heat_reviews.autofilter(0, 0, max(1, heat_index - 1), len(heat_headers)-1)
        chains = book.add_worksheet('四表匹配链')
        chains.write_row(0, 0, ['文件', '模型', '清单页', '清单行', '匹配顺序', '质量证明书页',
            '铭牌页', '送货单页', '匹配炉批号', '四表已关联', '异常/待核验原因'], formats['header'])
        chains.set_column(0, 1, 34); chains.set_column(2, 3, 12); chains.set_column(4, 4, 58)
        chains.set_column(5, 9, 18); chains.set_column(10, 10, 80); chains.freeze_panes(1, 4)
        role_names = dict(inspection='进场清单', certificate='质量证明书', nameplate='铭牌', delivery='送货单')
        chain_index = 1
        for document in report['documents']:
            for model in ('paddle', 'glm'):
                for pair in document['rows']:
                    row = pair.get(model)
                    if not row:
                        continue
                    sources = row.get('sources', {})
                    path = row.get('match_path') or (['inspection', 'certificate', 'nameplate', 'delivery']
                        if row.get('match_route') == 'certificate_number' else
                        ['inspection', 'nameplate', 'delivery', 'certificate'])
                    chains.write_row(chain_index, 0, [document['pdf_name'], report['models'][model],
                        pair['page_no'], pair['row_no'], ' → '.join(role_names.get(v, v) for v in path),
                        *[sources.get(role, {}).get('page_no') or '' for role in ('certificate', 'nameplate', 'delivery')],
                        '、'.join(row.get('matched_heat_lots', [])),
                        '已关联，待最终核验' if all(role in sources for role in ('certificate', 'nameplate', 'delivery')) else '关联未完整',
                        '\n'.join(row.get('diagnostics', []))[:32000]], formats['normal'])
                    chain_index += 1
        chains.autofilter(0, 0, chain_index - 1, 10)
        manifest = book.add_worksheet('源文件引用' if report.get('analysis_type') in
            ('fresh_paddle_vs_cached_glm', 'cached_models_current_matching') else '缓存来源')
        manifest.write_row(0, 0, ['原文件引用', 'SHA256', '字节'], formats['header'])
        manifest.set_column(0, 0, 100); manifest.set_column(1, 1, 70); manifest.set_column(2, 2, 16)
        for i, item in enumerate(report['source_manifest'], 1):
            manifest.write_row(i, 0, [item['path'], item['sha256'], item['bytes']], formats['normal'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--analysis-root', type=Path, required=True,
                        help='GLM 缓存重解析目录：含 analysis.json 与每卷 resources.json')
    parser.add_argument('--paddle-results', '--paddle-baseline', dest='paddle_results', type=Path,
                        default=ROOT / 'experiments/analyses/analysis-20261017T131558Z-glm-inputs-paddle-baseline.json')
    parser.add_argument('--require-fresh-paddle', action='store_true',
                        help='仅接受逐卷可追溯到已完成真实 OCR 新运行的 Paddle manifest')
    parser.add_argument('--require-replayed-paddle', action='store_true',
                        help='仅接受可追溯到原始 OCR 的本次匹配回放，不重新 OCR')
    parser.add_argument('--out', type=Path, required=True, help='必须为新建或空目录')
    args = parser.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        raise ValueError('不能覆盖既有比较结果，请指定新的输出目录')
    args.out.mkdir(parents=True, exist_ok=True)
    report = build_comparison(args.analysis_root, args.paddle_results, args.out,
                              require_fresh_paddle=args.require_fresh_paddle,
                              require_replayed_paddle=args.require_replayed_paddle)
    write_json(args.out / 'comparison.json', report)
    write_json(args.out / 'metrics.json', {d['pdf_name']: d['metrics'] for d in report['documents']})
    write_html(args.out / 'index.html', report)
    write_xlsx(args.out / 'ocr_ledger_comparison.xlsx', report)
    print(json.dumps(dict(status='completed', output=str(args.out), new_ocr_executed=False,
        images_saved=0, bytes=sum(p.stat().st_size for p in args.out.iterdir() if p.is_file()),
        documents=[dict(pdf_name=d['pdf_name'], **d['metrics']) for d in report['documents']]), ensure_ascii=False))


if __name__ == '__main__':
    main()
