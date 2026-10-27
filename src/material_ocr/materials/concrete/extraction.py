"""Concrete business fields from existing OCR; no model or rebar rule changes."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import re
import unicodedata

from material_ocr.upstream_legacy.extraction.glm_table import TableParser, parse_tables


ALIASES = {
    'row_no': ('序号',),
    'document_no': ('文件编号', '报审编号'),
    'material_name': ('材料名称', '材料品名', '名称'),
    'specification': ('规格品种', '规格型号', '混凝土强度等级', '砼强度等级', '强度等级', '规格'),
    'quantity': ('验收数量', '进场数量', '进场量', '进货数量', '数量', '方量'),
    'producer': ('生产厂家', '生产单位', '生产企业'),
    'supplier': ('供应单位', '供货单位', '供应商'),
    'warranty_no': ('质量证明书编号', '质量证明书号', '质保证书编号', '质保书编号', '质证书编号', '质保编号', '质保书号'),
    'use_location': ('使用部位', '工程部位', '浇筑部位', '浇注部位', '施工部位', '部位'),
    'entry_date': ('进场日期', '进货日期', '到货日期'),
    'supply_date': ('供货日期', '供应日期', '发货日期', '送货日期', '发货时间'),
    'batch_no': ('炉批号', '炉批编号', '生产批号', '出厂批号', '批次号', '批号'),
    'remarks': ('备注',),
}
EMPTY = {'', '/', '／', '-', '—', '无', '空'}
VOLUME_UNIT = r'(?:m\^?3|立方米|方)'
FIELD_LABELS = {'row_no': '序号', 'material_name': '材料名称', 'specification': '规格', 'quantity': '数量',
                'producer': '生产单位', 'supplier': '供应单位', 'warranty_no': '质保书编号',
                'entry_date': '进货日期', 'supply_date': '供货日期', 'document_no': '文件编号',
                'batch_no': '炉批号', 'use_location': '使用部位'}


def issue_text(code):
    labels = {
        'material_name_not_concrete': '材料名称尚未确认为混凝土',
        'concrete_specification_missing_or_invalid': '混凝土规格缺失或无法确认',
        'concrete_volume_missing_or_invalid': '混凝土方量缺失或无法确认',
        'invalid_volume_or_mass_unit': '数量不是有效的立方米数，或使用了质量单位',
        'missing_volume_unit': '数量缺少立方米单位依据',
        'volume_must_be_positive': '方量必须大于零',
        'ocr_output_incomplete_check_source_page': '识别内容可能不完整，请核对原页是否漏行',
        'use_location_from_remarks_check_source': '使用部位取自备注，请核对原页',
        'concrete_no_observed_business_rows': '尚未提取到可靠的混凝土业务行',
        'row_no_missing_or_invalid': '序号缺失或无法确认', 'duplicate_row_no': '同页序号重复',
        'cumulative_delivery_missing_or_ambiguous': '累计发货量缺失或无法确认',
        'low_ocr_confidence': '来源字段 OCR 置信度偏低，请核对原页',
        'document_no_visual_review_required': '报审表文件编号需目视核对原页',
        'not_detected_requires_review': '字段未检测到，不代表原表为空白',
        'field_contains_form_labels_check_crop': '字段内容可能混入表单标签，请核对原页',
        'submission_field_multiple_candidates': '报审表字段识别到多个不同值，请核对报审表',
    }
    if code in labels:
        return labels[code]
    for key, label in FIELD_LABELS.items():
        if code == key + '_not_observed':
            return label + '未提取'
        if code == key + '_cell_missing_or_ambiguous':
            return label + '所在单元格无法确认'
    return '表格结构或字段尚未确认，请核对原页'


def normalized(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(text or '')))


def value_text(text):
    text = str(text or '').strip()
    return '' if normalized(text) in EMPTY else text


def inspection_location_from_remarks(fields, evidence, issues):
    """Use a structural remark only when this inspection row has no location."""
    if not value_text(fields.get('use_location')) and re.search(
            r'轴|顶板|地下|\d+层|外墙|内墙|基础', fields.get('remarks', '')):
        fields['use_location'] = fields['remarks']
        evidence['use_location'] = dict(evidence.get('remarks', {}), source_field='remarks')
        if 'use_location_from_remarks_check_source' not in issues:
            issues.append('use_location_from_remarks_check_source')


def specification(text):
    """Keep strength, impermeability and self-compacting modifiers in the key."""
    text = normalized(text).upper()
    match = re.fullmatch(r'(自密实)?C(\d{2,3})(?:[/、-]?P(\d{1,2}))?(自密实)?', text)
    if not match:
        return ''
    return 'C' + match[2] + ('P' + match[3] if match[3] else '') + ('自密实' if match[1] or match[4] else '')


def volume(text, header='', *, concrete_delivery=False):
    """Only volume units are accepted; mass units are never converted to m³."""
    raw = normalized(text).lower()
    match = re.fullmatch(r'([+]?(?:\d+(?:\.\d+)?|\.\d+))(' + VOLUME_UNIT + r')?', raw)
    if not match:
        return None, 'invalid_volume_or_mass_unit'
    try:
        number = Decimal(match[1])
    except InvalidOperation:
        return None, 'invalid_volume'
    if not number.is_finite() or number <= 0:
        return None, 'volume_must_be_positive'
    header_unit = re.search(VOLUME_UNIT, normalized(header).lower())
    if not match[2] and not header_unit and not concrete_delivery:
        return None, 'missing_volume_unit'
    source = 'cell' if match[2] else 'column_header' if header_unit else 'concrete_delivery_contract'
    return {'value': format(number, 'f'), 'unit': 'm3', 'unit_source': source}, ''


def entry_date(text):
    match = re.fullmatch(r'(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?', normalized(text))
    if match:
        try:
            return date(*map(int, match.groups())).isoformat()
        except ValueError:
            pass
    return ''


def submission_date(text):
    """Normalize the date written in a material approval form.

    Approval forms sometimes cover more than one delivery day, for example
    ``2026年5月22日至23日``.  The ordinary entry-table date parser remains
    deliberately single-day; this parser is used only for the approval
    field, and expands an omitted year/month on the right hand side while
    retaining the range for the ledger and its evidence.
    """
    raw = normalized(text).strip()
    if not raw:
        return ''
    date_part = r'(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?'
    single = re.fullmatch(date_part, raw)
    if single:
        try:
            return date(*map(int, single.groups())).isoformat()
        except ValueError:
            return ''

    # Keep the separator explicit.  A hyphen is a valid date separator and
    # therefore is not accepted as a range separator here.
    left = re.fullmatch(date_part + r'(?:至|到|—|–)(.+)', raw)
    if not left:
        return ''
    year, month, day, right = (*left.groups()[:3], left.group(4))
    try:
        start = date(int(year), int(month), int(day))
    except ValueError:
        return ''

    right_full = re.fullmatch(date_part, right)
    right_month = re.fullmatch(r'(\d{1,2})[月./-](\d{1,2})日?', right)
    right_day = re.fullmatch(r'(\d{1,2})日?', right)
    if right_full:
        end_values = tuple(map(int, right_full.groups()))
    elif right_month:
        end_values = (int(year), int(right_month[1]), int(right_month[2]))
    elif right_day:
        end_values = (int(year), int(month), int(right_day[1]))
    else:
        return ''
    try:
        end = date(*end_values)
    except ValueError:
        return ''
    if end < start:
        return ''
    return start.isoformat() + '至' + end.isoformat()


def header_field(text):
    text = normalized(text).strip(':：')
    for name, aliases in ALIASES.items():
        if text in aliases:
            return name
    if re.fullmatch(r'(验收数量|进场数量|进场量|进货数量|数量|方量)(?:[/／])?[(]?' + VOLUME_UNIT + r'[)]?', text, re.I):
        return 'quantity'
    return None


def validate_fields(fields):
    """Missing/invalid observations remain draft issues, never human approval."""
    issues = []
    name = fields.get('material_name', '')
    if not re.search(r'混凝土|砼', name) or re.search(r'钢筋|HRB|HPB', name, re.I):
        issues.append('material_name_not_concrete')
    if not specification(fields.get('specification', '')):
        issues.append('concrete_specification_missing_or_invalid')
    if not fields.get('quantity_m3'):
        issues.append('concrete_volume_missing_or_invalid')
    for key in ('producer', 'supplier', 'use_location'):
        if not fields.get(key):
            issues.append(key + '_not_observed')
    if not fields.get('entry_date') and not fields.get('supply_date'):
        issues.append('entry_date_not_observed')
    return issues


def inspection_manufacturer_alias(fields, evidence):
    """Concrete registration sheets use their production-factory keyword for both ledger columns."""
    producer = fields.get('producer')
    if not producer:
        return
    fields['supplier'] = producer
    source = evidence.get('producer')
    if source:
        evidence['supplier'] = {**source, 'source_field': 'producer',
                                'mapping_rule': 'concrete_supplier_uses_inspection_producer'}


def parse_inspection_html(raw, page_no, *, inference=None, page_title=''):
    """Resolve keyword columns, row/col spans and one record per observed row."""
    if len(raw) > 2_000_000 or len(re.findall(r'<table\b', raw, re.I)) != 1:
        raise ValueError('concrete_requires_one_bounded_table')
    if re.search(r'<(?:script|style|iframe)\b', raw, re.I):
        raise ValueError('unexpected_active_content_in_table')
    matches = list(re.finditer(r'<tr\b[^>]*>.*?</tr\s*>', raw, re.I | re.S))
    rows = []
    for match in matches:
        parser = TableParser()
        parser.feed('<table>' + match.group() + '</table>')
        parser.close()
        rows.append(parser.tables[0][0])
    starts = [i for i, row in enumerate(rows) if any(normalized(c['raw_text']) == '序号' for c in row)]
    if len(starts) != 1:
        raise ValueError('concrete_header_missing_or_ambiguous')
    start, stop = starts[0], len(rows)
    for i in range(start + 1, len(rows)):
        nonempty = [normalized(c['raw_text']) for c in rows[i] if c['raw_text'].strip()]
        if nonempty and nonempty[0].startswith(('检查意见', '检验结论', '验收意见', '签字栏', '合计')):
            stop = i
            break
    # Only scope the business grid. Metadata and footer widths need not equal it.
    scoped = '<table>' + ''.join(m.group() for m in matches[start:stop]) + '</table>'
    cells = parse_tables(scoped)[0]
    serial = next(c for c in cells if normalized(c['raw_text']) == '序号')
    band_end = serial['grid']['row_end']
    headers = {}
    page_issues = []
    for cell in cells:
        if cell['grid']['row_start'] >= band_end:
            continue
        name = header_field(cell['raw_text'])
        if name:
            if name in headers:
                raise ValueError('concrete_ambiguous_header:' + name)
            headers[name] = cell
    # Only explicit title evidence can replace a missing material-name column.
    title_text = normalized(page_title or re.sub(r'<[^>]*>', '', raw[:matches[start].start()]))
    concrete_delivery_title = '混凝土材料进货清单' in title_text
    required_headers = ('row_no', 'specification', 'quantity') if concrete_delivery_title else (
        'row_no', 'material_name', 'specification', 'quantity')
    for name in required_headers:
        if name not in headers:
            raise ValueError('concrete_missing_header:' + name)
    quantity_heading = headers['quantity']['raw_text']
    if not re.search(VOLUME_UNIT, normalized(quantity_heading), re.I):
        qgrid = headers['quantity']['grid']
        unit_cells = [c for c in cells if c['grid']['row_start'] < band_end
                      and (c['grid']['col_start'], c['grid']['col_end']) == (qgrid['col_start'], qgrid['col_end'])
                      and re.fullmatch(r'[(]?' + VOLUME_UNIT + r'[)]?', normalized(c['raw_text']), re.I)]
        if len(unit_cells) == 1:
            quantity_heading += '(' + unit_cells[0]['raw_text'].strip('()（）') + ')'
    info = inference or {}
    if not re.search(r'</table\s*>', raw, re.I) or info.get('hit_token_limit') or info.get('generation_stop', {}).get('triggered'):
        page_issues.append('ocr_output_incomplete_check_source_page')
    def columns(cell):
        g = cell['grid']
        return g['col_start'], g['col_end']
    body = [c for c in cells if c['grid']['row_start'] >= band_end]
    anchors = [c for c in body if columns(c) == columns(serial)]
    records = []
    for anchor in anchors:
        a = anchor['grid']
        related = [c for c in body if c['grid']['row_start'] < a['row_end'] and c['grid']['row_end'] > a['row_start']]
        if not any(value_text(c['raw_text']) for c in related if c['cell_id'] != anchor['cell_id']):
            continue
        fields, evidence, issues = {}, {}, list(page_issues)
        for name, header in headers.items():
            candidates = [c for c in related if columns(c) == columns(header)]
            peers = [h for h in headers.values() if columns(h) == columns(header)]
            if len(peers) > 1:
                hg = header['grid']
                candidates = [c for c in candidates
                    if (c['grid']['row_start'] - a['row_start']) / a['rowspan'] >= hg['row_start'] / band_end
                    and (c['grid']['row_end'] - a['row_start']) / a['rowspan'] <= hg['row_end'] / band_end]
            if len(candidates) != 1:
                fields[name] = ''
                issues.append(name + '_cell_missing_or_ambiguous')
                continue
            cell = candidates[0]
            fields[name] = value_text(cell['raw_text'])
            evidence[name] = {'page_no': page_no, 'cell_id': cell['cell_id'], 'grid': cell['grid'],
                              'original_row_start': cell['grid']['row_start'] + start,
                              'raw_text': cell['raw_text'], 'header_text': header['raw_text'],
                              'coordinate_system': 'html_logical_grid', 'pixel_coordinates_available': False}
        if concrete_delivery_title and not fields.get('material_name'):
            fields['material_name'] = '混凝土'
            evidence['material_name'] = {'page_no': page_no, 'raw_text': '混凝土材料进货清单',
                'source_role': 'page_title', 'mapping_rule': 'explicit_concrete_delivery_list_title',
                'coordinate_system': 'page_title_text', 'pixel_coordinates_available': False}
        q, problem = volume(fields.get('quantity', ''), quantity_heading)
        fields['quantity_m3'] = q['value'] if q else ''
        fields['quantity_unit'] = 'm3'
        if q:
            evidence['quantity']['unit_source'] = q['unit_source']
            evidence['quantity']['unit_header_text'] = quantity_heading
        if problem:
            issues.append(problem)
        raw_spec = fields.get('specification', '')
        fields['specification'] = specification(raw_spec) or raw_spec
        if fields.get('entry_date'):
            fields['entry_date'] = entry_date(fields['entry_date'])
        if fields.get('supply_date'):
            fields['supply_date'] = entry_date(fields['supply_date'])
        inspection_manufacturer_alias(fields, evidence)
        # A remark containing an explicit structural location is useful evidence,
        # but remains flagged so a general remark cannot silently become a location.
        inspection_location_from_remarks(fields, evidence, issues)
        issues.extend(validate_fields(fields))
        if not re.fullmatch(r'\d+', fields.get('row_no', '')):
            issues.append('row_no_missing_or_invalid')
        records.append({'record_id': f'p{page_no:03d}:{anchor["cell_id"]}', 'page_no': page_no,
                        **fields, 'field_evidence': evidence, 'issues': list(dict.fromkeys(issues)),
                        'status': 'needs_review' if issues else 'rule_pass_review_required',
                        'material_type': 'concrete'})
    if not records:
        page_issues.append('concrete_no_observed_business_rows')
    counts = {}
    for row in records:
        counts[row.get('row_no')] = counts.get(row.get('row_no'), 0) + 1
    for row in records:
        if counts[row.get('row_no')] > 1:
            row['issues'].append('duplicate_row_no')
            row['status'] = 'needs_review'
    return {'records': records, 'issues': page_issues, 'material_type': 'concrete',
            'source': 'existing_glm_html', 'scoped_original_rows': [start, stop]}
