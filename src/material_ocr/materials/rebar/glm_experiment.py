"""Conservative logical-field analysis of native GLM-OCR experiment output.

This is an experimental report, not a replacement for the production spatial
extractor. A native model-output cell has no pixel box. The original page or an
explicitly supplied real ROI is therefore the review location for every value.
"""
from decimal import Decimal
from copy import deepcopy
import re
import unicodedata

from material_ocr.ocr.glm_tables import clean, parse_tables
from .matching import grade_number, grade_numeric_details


GRADE = r'(?:HRBF|HRB|HPB|RRB|CRB)\d{3,4}[EB]?'
COMPANY = r'[\u4e00-\u9fffA-Za-z（）()·]{2,}(?:有限责任公司|股份有限公司|有限公司|钢铁集团)'
ALIASES = {
    'row_no': ('序号',), 'material_name': ('材料名称', '材料品名', '品名'),
    'specification': ('规格型号', '规格品种', '规格', '直径'),
    'grade': ('牌号', '型号', '材质', '钢号', 'GRADE'),
    'quantity_t': ('进场数量', '进场量'),
    'manufacturer': ('生产厂家', '生产单位', '生产厂', '生产企业'),
    'certificate_no': ('质量证明书编号', '质量证明书号', '质保证明书编号', '质保书编号', '质证书编号', '合格证号', '证明书号', '证书编号'),
    'heat_lot_no': ('炉批号', '炉号', '轧制批号', '轧制号', '批号', 'HEATNO', 'BATCHNO'),
}

COMPANY_LABELS = ('生产厂家', '制造厂家', '生产单位', '生产企业', '生产厂',
                  '制造商', '厂家', '供应单位', '供货单位', '供方')
# This printed title is the supplier value for the reviewed dispatch forms in
# the 2025.12.6 document.  It is intentionally scoped to delivery headings;
# the same text must not become a nameplate manufacturer merely because it is
# the first text on a page.
GENERIC_DELIVERY_HEADINGS = frozenset(('上海市钢筋混凝土结构用钢筋行业',))
OTHER_COMPANY_ROLE = (r'购货(?:单位|方)|收货(?:单位|方)|订货(?:单位|方)|客户(?:名称|单位)|'
                      r'客户\s*[:：]|需方|建设单位|监理单位|检验单位|提货单位')
OTHER_COMPANY_ROLE_LABEL = re.compile(
    r'(?:购货(?:单位|方)|收货(?:单位|方)|订货(?:单位|方)|客户(?:名称|单位)?|'
    r'需方|建设单位|监理单位|检验单位|提货单位|CUSTOMER(?:NAME)?|'
    r'PURCHASER(?:NAME)?|BUYER(?:NAME)?|CONSIGNEE|RECEIVER|'
    r'ORDERING(?:PARTY|COMPANY)|ORDEREDBY|SHIPTO)', re.I)
OTHER_COMPANY_ENGLISH_PREFIX = re.compile(
    r'^(?:CUSTOMER(?:\s+NAME)?|PURCHASER(?:\s+NAME)?|BUYER(?:\s+NAME)?|'
    r'CONSIGNEE|RECEIVER|ORDERING\s+(?:PARTY|COMPANY)|ORDERED\s+BY|SHIP\s+TO)'
    r'(?:\s*[:：]\s*|\s+)\S', re.I)
PLATE_BODY_START = re.compile(
    r'^(?:品\s*名|产品名称|执行标准|标准\s*[:：]|牌\s*号|规\s*格|炉\s*批\s*号|'
    r'轧\s*制\s*(?:号|批号)|重量|日期|生产(?:厂家|厂|单位|企业)|制造商)')


def _company_names(line, *, allow_generic=False):
    """Literal company text, excluding a customer/order/signature role."""
    text = clean(line).strip('#*| ')
    if re.search(OTHER_COMPANY_ROLE, text) or OTHER_COMPANY_ENGLISH_PREFIX.search(text):
        return []
    for label in sorted(COMPANY_LABELS, key=len, reverse=True):
        text = re.sub(r'^' + label + r'\s*[:：]?\s*', '', text)
    names = re.findall(COMPANY, text)
    # Some local dispatch forms print an industry association/title instead of
    # a legal company name.  Business review confirmed this exact title is the
    # supplier on the 2025.12.6 page 5 forms.  Keep the exception explicit and
    # only enable it for a delivery heading caller.
    if allow_generic and re.sub(r'\s+', '', text) in GENERIC_DELIVERY_HEADINGS:
        names.append(re.sub(r'\s+', '', text))
    # These are incomplete suffixes, not an identified company.  Never expand
    # one using another card or the name observed by another OCR model.
    return [name for name in names if name not in
            ('管理有限公司', '供应链管理有限公司', '股份有限公司', '有限责任公司')]


def _company_key(value):
    return re.sub(r'(?:有限责任公司|股份有限公司|有限公司)$', '', token(value))


def _other_company_role_label(line):
    """Recognize field labels, including a separate bilingual translation.

    This is an exact label check.  Words in an unlabelled legal company name
    must not blacklist that company merely because they resemble a role.
    """
    parts = re.split(r'[/／]', token(line).strip(':：.'))
    return bool(parts) and all(OTHER_COMPANY_ROLE_LABEL.fullmatch(part.strip(':：.'))
                               for part in parts)


def _company_heading(checkpoint, text, role, *, first_line=1):
    """Read this form's header, never prefer a company in its body.

    Logical line references locate model text; only the actual page/ROI is a
    pixel location.  Different body/header readings are kept for final review.
    """
    lines = [(first_line + i, clean(line).strip('#*| '))
             for i, line in enumerate(text.splitlines()) if clean(line)]
    boundary = next((i for i, (_, line) in enumerate(lines)
                     if PLATE_BODY_START.search(line)), len(lines)) if role == 'nameplate' else len(lines)
    role_excluded = [(_other_company_role_label(line) or OTHER_COMPANY_ENGLISH_PREFIX.search(line) or
                      bool(index and _other_company_role_label(lines[index-1][1])))
                     for index, (_, line) in enumerate(lines)]
    header, body = [], []
    for index, (line_no, line) in enumerate(lines):
        names = _company_names(line, allow_generic=role == 'delivery')
        source_lines = [dict(line_no=line_no, text=line)]
        if role_excluded[index]:
            names = []
        if role == 'nameplate' and index >= boundary:
            producer_label = r'^(?:生产厂家|制造厂家|生产单位|生产企业|生产厂|制造商|厂家)'
            labelled = re.match(producer_label, line) or (index and re.fullmatch(
                producer_label + r'\s*[:：]?', lines[index-1][1]))
            if not labelled:
                names = []
        # A wrapped company heading is admissible only across adjacent lines
        # in the same header; record that reconstruction as a text operation.
        if (index and index < boundary and not role_excluded[index] and not role_excluded[index-1]
                and re.match(r'^(?:供应链|钢铁|特钢|实业|贸易|建材|有限|股份)', line)
                and re.fullmatch(r'[\u4e00-\u9fff]{2,12}', lines[index-1][1])
                and lines[index-1][1] not in COMPANY_LABELS
                and not re.search(r'单位|品名|产品|标准|许可|牌号|规格|行业', lines[index-1][1])):
            joined = lines[index-1][1] + line
            if re.fullmatch(COMPANY, joined):
                names = [joined]
                source_lines.insert(0, dict(line_no=lines[index-1][0], text=lines[index-1][1]))
        for name in names:
            item = dict(value=name, raw_text=line, line_no=line_no,
                        source_lines=source_lines,
                        source_role=role, method='form_top_company' if index < boundary else 'form_body_company',
                        evidence=location(checkpoint, line_no=line_no, raw_text=line,
                                          source_lines=source_lines,
                                          source_role=role, field='manufacturer' if role == 'nameplate' else 'supplier'))
            (header if index < boundary else body).append(item)
    # A neighbouring supplier title must not become a nameplate manufacturer.
    if role == 'nameplate':
        header = [h for h in header if not re.search(r'供应单位|供货单位|供方', h['raw_text'])]
    issues = []
    if not header:
        for index, (line_no, line) in enumerate(lines[:boundary]):
            if role_excluded[index]:
                continue
            if re.fullmatch(r"[A-Za-z][A-Za-z .,&'()-]*(?:GROUP|CO[., ]*LTD[.]?|CORPORATION LTD[.]?)", line, re.I):
                header.append(dict(value=line, raw_text=line, line_no=line_no, source_role=role,
                    method='form_top_company_english', evidence=location(checkpoint, line_no=line_no,
                        raw_text=line, source_role=role)))
                issues.append('nameplate_manufacturer_english_only' if role == 'nameplate' else 'supplier_english_only')
                break
    value = header[0]['value'] if header else ''
    if not value:
        issues.append('nameplate_manufacturer_unreadable' if role == 'nameplate' else 'supplier_missing')
    all_candidates = header + body
    if value and any(_company_key(c['value']) != _company_key(value) for c in all_candidates):
        issues.append('manufacturer_heading_conflict' if role == 'nameplate' else 'supplier_heading_conflict')
    return dict(value=value, candidates=all_candidates, issues=issues,
                evidence=header[0]['evidence'] if header else location(checkpoint, source_role=role),
                rule='nameplate_top_company' if role == 'nameplate' else 'delivery_top_company',
                raw_header_lines=[dict(line_no=n, text=line) for n, line in lines[:boundary]],
                review_required=True)


def _delivery_heading_text(text, marker):
    """Restrict a dispatch header to the local lines immediately before it."""
    preceding = text[:marker.start()].splitlines()
    selected = []
    for index in range(len(preceding)-1, -1, -1):
        line = clean(preceding[index])
        if not line:
            continue
        if (PLATE_BODY_START.search(line) or re.search(
                r'送货单|发货单|供货单|电子质保书|合计|盖章|开单人|备注|许可证号|电话|地址', line)):
            break
        selected.append((index+1, preceding[index]))
        if len(selected) >= 6:
            break
    selected.reverse()
    first_line = selected[0][0] if selected else 1
    return ('\n'.join(preceding[first_line-1:]) if selected else '', first_line)


def token(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or ''))).upper()


def identifier(value):
    value = token(value)
    return value if re.fullmatch(r'[A-Z0-9][A-Z0-9._/-]{2,}', value) and re.search(r'\d', value) else ''


def specification(value, grade=''):
    text = token(value)
    grades = sorted(set(re.findall(GRADE, text + ' ' + token(grade))))
    sizes = re.findall(r'[ΦφФфØ⌀∅](\d{1,2}(?:\.\d+)?)', text)
    sizes += re.findall(r'(?<!\d)(\d{1,2}(?:\.\d+)?)MM', text)
    if not sizes and re.fullmatch(r'\d{1,2}(?:\.\d+)?', text):
        sizes = [text]
    sizes = sorted({format(Decimal(v).normalize(), 'f') for v in sizes if Decimal('5') <= Decimal(v) <= Decimal('50')})
    return (grades[0] if len(grades) == 1 else '', sizes[0] if len(sizes) == 1 else '')


def location(checkpoint, **extra):
    region = checkpoint.get('source_region') or {}
    evidence = dict(page_no=checkpoint['page_no'], polygon_page=region.get('polygon_page'),
        method='glm_native_logical_output', confidence=None,
        precision='region' if region.get('polygon_page') else 'page', **extra)
    source_pixel_size = (region.get('source_pixel_size') or checkpoint.get('source_pixel_size')
                         or checkpoint.get('original_pixel_size') or checkpoint.get('pixel_size'))
    render_dpi = region.get('render_dpi') or checkpoint.get('render_dpi') or checkpoint.get('dpi')
    if source_pixel_size:
        evidence['source_pixel_size'] = source_pixel_size
    if render_dpi:
        evidence['render_dpi'] = render_dpi
    for key in ('kind', 'boundary_method', 'physical_card_boundary_verified', 'rotation_ccw', 'region_id'):
        if key in region:
            evidence[key] = region[key]
    if checkpoint.get('checkpoint_reference'):
        evidence['checkpoint_reference'] = checkpoint['checkpoint_reference']
    if checkpoint.get('checkpoint_sha256'):
        evidence['checkpoint_sha256'] = checkpoint['checkpoint_sha256']
    return evidence


def output_quality(checkpoint):
    """A completed generation can still be incomplete or repetitive."""
    issues = []
    if checkpoint.get('truncated'):
        issues.append('glm_output_truncated')
    if re.search(r'(.{1,8})\1{30,}', token(checkpoint.get('raw_text', ''))):
        issues.append('glm_repeated_text')
    return issues


def merge_checkpoint_variants(checkpoints):
    """Merge full-page and ROI-only checkpoints without replacing page text."""
    if not checkpoints:
        raise ValueError('At least one page checkpoint is required')
    unique = {}
    for checkpoint in checkpoints:
        source_key = (checkpoint.get('checkpoint_reference'), checkpoint.get('checkpoint_sha256'),
                      checkpoint.get('schema_version'))
        key = source_key if source_key[0] else ('anonymous', len(unique))
        unique.setdefault(key, checkpoint)
    checkpoints = list(unique.values())
    identities = {(c.get('pdf_sha256'), c.get('page_no')) for c in checkpoints}
    if len(identities) != 1:
        raise ValueError('Only checkpoints for the same source PDF page can be merged')
    full_candidates = [c for c in checkpoints if c.get('raw_text')]
    base = deepcopy(max(full_candidates, key=lambda c: len(c.get('raw_text', '')))
                    if full_candidates else checkpoints[0])
    merged_regions = {}
    for checkpoint in checkpoints:
        for region in checkpoint.get('regions', []):
            key = region.get('region_id')
            if not key:
                key = f'anon:{len(merged_regions)}'
            old = merged_regions.get(key)
            new_score = (not region.get('truncated', False), len(region.get('raw_text', '')),
                         region.get('generated_tokens', 0))
            old_score = ((not old.get('truncated', False)), len(old.get('raw_text', '')),
                         old.get('generated_tokens', 0)) if old else None
            if old is None or new_score > old_score:
                merged_regions[key] = deepcopy(region)
    base['regions'] = list(merged_regions.values())
    base['checkpoint_variants'] = [dict(schema_version=c.get('schema_version'),
        checkpoint_reference=c.get('checkpoint_reference'), checkpoint_sha256=c.get('checkpoint_sha256'),
        has_full_page_text=bool(c.get('raw_text')), region_count=len(c.get('regions', [])))
        for c in checkpoints]
    # Keep the most informative status/text source and the full page's page
    # geometry.  ROI-only metadata stays in checkpoint_variants and regions.
    if full_candidates:
        best = max(full_candidates, key=lambda c: len(c.get('raw_text', '')))
        for key in ('schema_version', 'pdf_name', 'pdf_path', 'pdf_sha256', 'page_no',
                    'original_pixel_size', 'pixel_size', 'rotation_ccw', 'dpi', 'model_fingerprint',
                    'truncated', 'polygon_page', 'coordinate_note'):
            if key in best:
                base[key] = deepcopy(best[key])
    return base


def _header(text, name):
    text = re.sub(r'[/／:：.()（）\s]', '', token(text))
    return any(text == token(alias) or (name == 'quantity_t' and text in (token(alias)+'T', token(alias)+'吨', token(alias)+'KG'))
               for alias in ALIASES[name])


def _entry_tables(checkpoint, parsed, issues):
    records = []
    text = token(checkpoint.get('raw_text'))
    if not any(title in text for title in ('进场数量清单', '进场检验记录')):
        return records
    for table in parsed['tables']:
        grid = table['grid']
        headers, header_row = {}, None
        for ri, row in enumerate(grid):
            found = {name: ci for ci, value in enumerate(row) for name in ALIASES if _header(value, name)}
            if {'row_no', 'specification', 'quantity_t'} <= found.keys():
                headers, header_row = found, ri
                break
        if header_row is None:
            continue
        quantity_header = token(grid[header_row][headers['quantity_t']])
        unit = 'kg' if re.search(r'KG|千克|公斤', quantity_header) else 't' if re.search(r'[（(]?T[）)]?|吨', quantity_header) else None
        for ri in range(header_row+1, len(grid)):
            row = grid[ri]
            raw = {name: row[ci] for name, ci in headers.items()}
            if not re.fullmatch(r'[1-9]\d{0,3}', token(raw['row_no'])):
                continue
            grade, diameter = specification(raw['specification'], raw.get('grade'))
            quantity = token(raw.get('quantity_t'))
            inline_unit = re.search(r'(KG|千克|公斤|吨|T)$', quantity)
            quantity_unit = ('kg' if inline_unit[1] in ('KG', '千克', '公斤') else 't') if inline_unit else unit
            number = re.sub(r'(KG|千克|公斤|吨|T)$', '', quantity)
            value = None
            if re.fullmatch(r'\d+(?:\.\d+)?', number) and quantity_unit:
                value = format(Decimal(number) / (1000 if quantity_unit == 'kg' else 1), 'f')
            warnings = list(table['issues'])
            if not grade or not diameter: warnings.append('specification_or_grade_unreadable')
            if value is None: warnings.append('entry_quantity_or_unit_unreadable')
            cert = identifier(raw.get('certificate_no'))
            if raw.get('certificate_no') and not cert and token(raw['certificate_no']) not in ('-', '—', '/', '无'):
                warnings.append('certificate_number_unreadable')
            rid = f'p{checkpoint["page_no"]:03d}_glm_t{table["table_index"]}_r{ri}'
            records.append(dict(record_id=rid, page_no=checkpoint['page_no'], row_no=int(raw['row_no']),
                material_name=raw.get('material_name', ''), grade=grade, diameter_mm=diameter,
                weight_t=value, certificate_no=cert, manufacturer=raw.get('manufacturer', ''),
                raw_cells=raw, issues=warnings,
                evidence=location(checkpoint, table_index=table['table_index'], logical_row=ri)))
    return records


def _quantity(raw, default_unit=None):
    value = token(raw)
    match = re.fullmatch(r'(\d+(?:\.\d+)?)(KG|千克|公斤|吨|T)?', value)
    if not match:
        return None
    unit = match[2] or default_unit
    if not unit:
        return None
    return format(Decimal(match[1]) / (1000 if unit in ('KG', '千克', '公斤') else 1), 'f')


def _plain_entry_records(checkpoint):
    """Read explicit entry columns from inline or stacked GLM text."""
    text = checkpoint.get('raw_text') or ''
    if not any(title in token(text) for title in ('进场数量清单', '进场检验记录')):
        return []
    records = []
    header = re.search(r'进场数量[（(]([^）)]+)[）)]', text)
    unit = token(header[1]) if header else None
    # A quantity list is rendered as one complete row per line.
    for line_no, source_line in enumerate(text.splitlines(), 1):
        line = clean(source_line)
        match = re.match(
            r'^([1-9]\d{0,3})\s+(\S+)\s+(\d{4}[/-]\d{1,2}[/-]\d{1,2})\s+'
            r'(.+?)\s+(' + COMPANY + r')\s+(\d+(?:\.\d+)?(?:[tT]|[kK][gG])?)(?:\s+(.*))?$', line)
        if not match:
            continue
        row_no, material, date, raw_spec, company, quantity, tail = match.groups()
        records.append(_plain_entry(checkpoint, row_no, material, raw_spec, company,
                                    quantity, '', location(checkpoint, line_no=line_no,
                                    source='plain_text_line'), unit=unit,
                                    raw=dict(source_line=source_line, date=date,
                                             trailing=tail or '')))
    if records:
        return records
    # Inspection records often have every cell on its own line, with company
    # and certificate touching.  Require the consecutive material/spec/quantity
    # cells, not the project heading or sign-off company.
    lines = [(i, clean(v)) for i, v in enumerate(text.splitlines(), 1) if clean(v)]
    for index, (line_no, value) in enumerate(lines):
        if not re.fullmatch(r'[1-9]\d{0,3}', value) or index + 4 >= len(lines):
            continue
        material, raw_spec, quantity, company_value = [v for _, v in lines[index+1:index+5]]
        if not re.search(r'钢筋|钢材', material) or not specification(raw_spec)[1] or _quantity(quantity) is None:
            continue
        company = re.match('(' + COMPANY + r')(.*)$', company_value)
        if not company:
            continue
        cert_raw = company[2].strip()
        if not cert_raw and index + 5 < len(lines):
            next_value = lines[index + 5][1]
            if identifier(next_value):
                cert_raw = next_value
        records.append(_plain_entry(checkpoint, value, material, raw_spec, company[1],
                                    quantity, cert_raw, location(checkpoint, line_no=line_no,
                                    source='plain_text_stacked_cells'),
                                    raw=dict(source_lines=[v for _, v in lines[index:index+6]])))
    return records


def _plain_entry(checkpoint, row_no, material, raw_spec, company, quantity, cert_raw,
                 evidence, *, unit=None, raw=None):
    grade, diameter = specification(raw_spec)
    weight = _quantity(quantity, unit)
    cert = identifier(cert_raw)
    issues = output_quality(checkpoint)
    if not grade:
        issues.append('entry_grade_missing_in_ocr_output')
    if not diameter:
        issues.append('entry_diameter_unreadable')
    if weight is None:
        issues.append('entry_quantity_or_unit_unreadable')
    if cert_raw and not cert:
        issues.append('certificate_number_unreadable')
    return dict(record_id=f'p{checkpoint["page_no"]:03d}_glm_plain_r{row_no}',
                page_no=checkpoint['page_no'], row_no=int(row_no), material_name=material,
                grade=grade, diameter_mm=diameter, weight_t=weight,
                certificate_no=cert, manufacturer=company,
                raw_cells=dict(specification=raw_spec, manufacturer=company,
                               quantity=quantity, certificate_no=cert_raw, **(raw or {})),
                issues=issues, evidence=evidence)


def _plain_delivery_line_specification(line):
    """Read one delivery's physical text line without joining neighbouring rows.

    Native output can flatten a goods table into ``18*9HRB500E`` followed
    by labels, weights and remarks.  The first dimension is the diameter and
    the second is the cut length.  Several dimensions/grade occurrences on a
    line cannot establish one physical goods row, even if their values repeat.
    Such a line keeps its text for review with empty automatic matching keys.
    """
    text = unicodedata.normalize('NFKC', clean(line)).upper()
    # Preserve common literal OCR prefix variants, including the extra 3 in
    # 1级B3400E/1级B3500E. The comparison helper, rather than a rewritten grade,
    # determines whether the rightmost strength digits are usable.
    grade_matches = list(re.finditer(
        r'(?<![A-Z])(?:HRBF|HRB|HPB|RRB|CRB|HBE|IRB|[0-9]级[A-Z]{1,3})'
        r'[0-9]{3,8}[A-Z]{0,2}(?![A-Z0-9])', text))
    dimensions = list(re.finditer(
        r'(?<![\dA-Z.])(\d{1,2}(?:\.\d+)?)\s*[*×X]\s*'
        r'(\d{1,3}(?:\.\d+)?)(?![\d.])', text))
    explicit_sizes = list(re.finditer(
        r'[ΦφФфØ⌀∅]\s*\d{1,2}(?:\.\d+)?(?:\s*MM)?|'
        r'(?<!\d)\d{1,2}(?:\.\d+)?\s*MM|'
        # GLM sometimes places the diameter mark after the number (``40ϕ``)
        # in a flattened delivery row.  It is the same strict diameter key as
        # ``ϕ40``; preserve the OCR text and keep the symbol variant review flag.
        r'(?<!\d)(\d{1,2}(?:\.\d+)?)\s*[ΦφФфØ⌀∅]', text))
    _, diameter = specification(line)
    observed_grade = grade_matches[0].group() if len(grade_matches) == 1 else ''
    grade = observed_grade if grade_number(observed_grade) else ''
    issues = []
    if grade and grade_numeric_details(grade)['ignored_leading_digits']:
        issues.append('delivery_grade_leading_ocr_digits_ignored_review')
    has_line_keywords = bool(grade_matches and (dimensions or explicit_sizes))
    malformed_dimension = any(re.match(r'\s*[*×X]', text[item.end():]) for item in dimensions)
    if (len(grade_matches) > 1 or len(dimensions) > 1 or len(explicit_sizes) > 1
            or malformed_dimension):
        return '', '', ['delivery_line_multiple_or_ambiguous_records'], has_line_keywords
    if dimensions:
        observed = dimensions[0][1]
        if not Decimal('5') <= Decimal(observed) <= Decimal('50') or Decimal(dimensions[0][2]) <= 0:
            return grade, '', ['delivery_diameter_unreadable'], has_line_keywords
        observed = format(Decimal(observed).normalize(), 'f')
        if diameter and Decimal(diameter) != Decimal(observed):
            return grade, '', ['delivery_diameter_conflict'], has_line_keywords
        diameter = observed
    trailing_symbol = re.search(r'(?<!\d)(\d{1,2}(?:\.\d+)?)\s*[ΦφФфØ⌀∅]', text)
    if not diameter and trailing_symbol:
        observed = format(Decimal(trailing_symbol.group(1)).normalize(), 'f')
        if Decimal('5') <= Decimal(observed) <= Decimal('50'):
            diameter = observed
            issues.append('delivery_diameter_symbol_ocr_variant')
    # Keep visible OCR-symbol variants and their issue for final review.
    size_variant = re.search(r'(?:^|\s)(?:中(\d{1,2})|(\d{1,2})中)(?:\s|$)', text)
    if not diameter and size_variant:
        diameter = size_variant[1] or size_variant[2]
        issues.append('delivery_diameter_symbol_ocr_variant')
    if not grade:
        issues.append('delivery_grade_unreadable')
    if not diameter:
        issues.append('delivery_diameter_unreadable')
    return grade, diameter, issues, has_line_keywords


def _plain_delivery(checkpoint):
    """Keep each titled delivery form and its No. separate on a page."""
    text = checkpoint.get('raw_text') or ''
    markers = list(re.finditer(r'(?m)^[ \t]*(?:送货单|发货单|供货单)[ \t]*$', text))
    result = []
    for index, marker in enumerate(markers):
        heading, heading_line = _delivery_heading_text(text, marker)
        supplier_heading = _company_heading(checkpoint, heading, 'delivery', first_line=heading_line)
        end = markers[index+1].start() if index+1 < len(markers) else len(text)
        block = text[marker.end():end]
        numbers = list(dict.fromkeys(re.findall(
            r'(?im)^[ \t]*NO[ \t]*[.:：]?[ \t]*([A-Za-z0-9]*\d[A-Za-z0-9]*)', block)))
        supplier = supplier_heading['value']
        # Body labels are supplementary evidence, never an override of the
        # top company requested by the ledger contract.
        for offset, line in enumerate(block.splitlines(), text[:marker.end()].count('\n')+1):
            if re.match(r'^\s*(?:供应单位|供货单位|供方)\s*[:：]', line):
                for company in _company_names(line):
                    ev = location(checkpoint, line_no=offset, raw_text=line, source_role='delivery')
                    supplier_heading['candidates'].append(dict(value=company, raw_text=line,
                        line_no=offset, source_role='delivery', method='form_body_company', evidence=ev))
                    if supplier and _company_key(supplier) != _company_key(company):
                        supplier_heading['issues'].append('supplier_heading_conflict')
        rows, active = [], False
        block_first_line = text[:marker.end()].count('\n') + 1
        for line_no, raw_line in enumerate(block.splitlines(), 1):
            line = clean(raw_line)
            if line.startswith(('合计', '说明', '送货地址', '金额', '在签单', '合同说明')):
                break
            if not line or line.startswith(('购货单位', '工程名称', '开单日期', '备注', '联系电话',
                                            '地址', '炉批号', '轧制批号', '质量证明书', '产品质量证明书')):
                continue
            grade, diameter, row_issues, line_keywords = _plain_delivery_line_specification(line)
            if ((('品名' in line and '规格型号' in line) or ('产地' in line and '件数' in line))
                    and not line_keywords):
                active = True
                continue
            if not active and not line_keywords:
                continue
            if not line_keywords and not re.search(r'\d+(?:\.\d+)?$', line):
                continue
            active = True
            rows.append(dict(grade=grade, diameter_mm=diameter, source_line=raw_line,
                             logical_row=line_no, issues=row_issues,
                             evidence=location(checkpoint, source_role='delivery',
                                 line_no=block_first_line + line_no - 1, raw_text=raw_line)))
        if not numbers and not supplier and not rows:
            continue
        issues = sorted(set(output_quality(checkpoint) + supplier_heading['issues']))
        if not numbers:
            issues.append('delivery_number_unreadable')
        elif len(numbers) != 1:
            issues.append('delivery_number_conflict')
        result.append(dict(candidate_id=f'glm-delivery:p{checkpoint["page_no"]}:plain:'
                           + str(checkpoint.get('region_id', 'full')) + f':{index}',
                           page_no=checkpoint['page_no'], supplier=supplier,
                           supplier_heading=supplier_heading,
                           delivery_no=numbers[0] if len(numbers) == 1 else '',
                           delivery_number_candidates=numbers, rows=rows, issues=issues,
                           form_goods_signatures=_delivery_goods_signatures(block),
                           evidence=location(checkpoint, source='plain_text_block', block_index=index)))
    return result


def _delivery_goods_signatures(text):
    """Observed diameter/length, class digits and three quantity columns.

    This identifies repeat form views despite an unreadable grade prefix;
    it does not validate that grade or select a conflicting form number.
    """
    signatures = []
    for line in text.splitlines():
        match = re.search(r'(\d{1,2})\s*[*×X]\s*(\d{1,2})\s*[A-Z]{2,4}(400|500|600)[EBZ]?'
                          r'\s+\S+\s+(\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)(?:\s|$)', line, re.I)
        if match:
            signatures.append(tuple(match.groups()))
    return sorted(set(signatures))



def _pairs(table, aliases):
    """Read labelled neighbours and explicit table-column values, without OCR corrections."""
    values = []
    for ri, row in enumerate(table['grid']):
        for ci, text in enumerate(row):
            label = token(text)
            matched = next((alias for alias in sorted(aliases, key=len, reverse=True)
                            if label.startswith(token(alias))), None)
            if not matched:
                continue
            rest = label[len(token(matched)):].lstrip(':：.')
            if rest:
                values.append(rest)
            elif ci+1 < len(row) and row[ci+1] and row[ci+1] != text:
                values.append(row[ci+1])
            else:
                for below in table['grid'][ri+1:]:
                    if ci < len(below) and below[ci] and below[ci] != text:
                        values.append(below[ci])
    return list(dict.fromkeys(values))


def _labels(text, aliases):
    pattern = r'(?:' + '|'.join(re.escape(a) for a in aliases) + r')\s*[:：.]?\s*([A-Za-z0-9][A-Za-z0-9._/-]{2,})'
    return list(dict.fromkeys(m[1] for m in re.finditer(pattern, text, re.I)))


def _plain_labels(text, aliases):
    """Read same-line or nearby bilingual label values, retaining literals."""
    lines = [token(v) for v in str(text).splitlines() if token(v)]
    result = []
    for index, line in enumerate(lines):
        for alias in sorted(aliases, key=len, reverse=True):
            label = token(alias)
            at = line.find(label)
            if at < 0:
                continue
            rest = line[at + len(label):].lstrip(':：.')
            # Bilingual labels may be emitted as ``炉批号 Furnace Batch
            # 36102060``.  The English label is metadata, never the value.
            rest = re.sub(r'^(?:FURNACE\s*BATCH|HEAT\s*NO|BATCH\s*NO|BATCH|HEAT)\s*', '', rest, flags=re.I)
            match = re.match(r'([A-Z0-9][A-Z0-9._/-]{2,})', rest)
            if match and identifier(match[1]):
                result.append(match[1])
            elif not rest or rest in ('GRADE', 'STEELGRADE', 'FURNACEBATCH', 'HEATNO', 'BATCHNO', 'SIZE', 'DIAMETER'):
                for below in lines[index+1:index+4]:
                    if identifier(below):
                        result.append(below)
                        break
                    # Only transparent English translations may be skipped.
                    if below.rstrip(':：.') not in ('GRADE', 'STEELGRADE', 'FURNACEBATCH', 'HEATNO', 'BATCHNO', 'SIZE', 'DIAMETER', 'CERTIFICATENO', 'CERTIFICATENUMBER') and not (
                            label in ('批号', '炉批号') and below.rstrip(':：.') == 'BRITISHNO'):
                        break
            break
    return list(dict.fromkeys(result))


_DETECTION_REPORT_TITLES = ('钢筋原材料检测报告', '钢筋原材检测报告', '钢筋原材料检验报告')


def _certificate_text(text):
    compact = token(text)
    # A column named “质量证明书编号” is present on entry forms too.  An
    # explicit entry-form title wins over that column label; standalone
    # quality certificates still retain their ordinary “质量证明书编号” title.
    if any(a in compact for a in ('进场数量清单', '进场检验记录') + _DETECTION_REPORT_TITLES):
        return False
    bilingual_table = (any(a in compact for a in ('TENSILETEST', 'SMELTINGCOMPOS')) and
                       any(a in compact for a in ('HEATNO', 'LOTNO')) and
                       ('CERTIFICATENO' in compact or ('PERMISSIONNO' in compact and
                        any(a in compact for a in ('CUSTOMER', 'CONSIGNEE')))))
    return ('质量证明书' in compact or 'MILLTESTCERTIFICATE' in compact or bilingual_table or
            (any(a in compact for a in ('证书编号', '证明书号')) and
             any(a in compact for a in ('化学成份', '化学成分', '力学性能', '屈服强度'))) or
            (any(a in compact for a in ('化学成份', '化学成分')) and
             any(a in compact for a in ('力学性能', '力学和工艺性能', '屈服强度')) and
             any(a in compact for a in ('订货单位', '合同号', '交货状态', '理论重量'))))


def _plain_plate(checkpoint):
    text = checkpoint.get('raw_text') or ''
    if _certificate_text(text) or any(a in token(text) for a in ('进场数量清单', '进场检验记录') + _DETECTION_REPORT_TITLES):
        return None
    roi = bool((checkpoint.get('source_region') or {}).get('polygon_page'))
    # A mixed page may contain one plate followed by a delivery form.  A full
    # page with several sizes remains ambiguous and is not collapsed to a card.
    marker = re.search(r'(?m)^\s*(?:送货单|发货单|供货单)\s*$', text)
    plate_text = text[:marker.start()] if marker else text
    if marker:
        heading, first_line = _delivery_heading_text(text, marker)
        if _company_heading(checkpoint, heading, 'delivery')['value']:
            plate_text = '\n'.join(text.splitlines()[:first_line-1])
    grades = sorted(set(re.findall(GRADE, token(plate_text))))
    grade, diameter = specification(plate_text)
    grade_suspect = False
    grade_observations = _plain_labels(plate_text, ALIASES['grade'])
    grade_ocr_candidates = [value for value in grade_observations
                            if not re.fullmatch(GRADE, token(value))]
    if not grade:
        literal_grades = [value for value in grade_observations
                          if re.fullmatch(r'[A-Z]{1,4}\d{3,5}[EB]?', value)
                          and grade_number(value)]
        if len(set(literal_grades)) == 1:
            grade = literal_grades[0]
            grade_suspect = True
    heats = _plain_labels(plate_text, ALIASES['heat_lot_no'])
    heats = [v for v in heats if v not in grades]
    # Batch No. and smelting heat No. can be separate fields on one card.
    # Preserve both literal roles; the batch is the certificate's batch key,
    # not a second physical-card candidate or an OCR disagreement.
    batches = _plain_labels(plate_text, ('批号',))
    smelting = _plain_labels(plate_text, ('冶炼炉号', '炉号'))
    if (len(batches) == 1 and len(smelting) == 1 and batches[0] != smelting[0]
            and set(heats) == {batches[0], smelting[0]}):
        heats = batches
    # Missing size must not erase a literal heat/grade label group.  Retain
    # that card as a candidate with its observed company and original page;
    # the matcher still has to satisfy the diameter constraint separately.
    partial_size = bool(not diameter and grade and heats and
        (checkpoint.get('mixed_form_segment') or
         re.search(r'(?m)^\s*(?:送货单|发货单|供货单)\s*$', text)) and
        re.search(r'品\s*名|产品名称|热轧带肋钢筋', plate_text) and
        _company_heading(checkpoint, plate_text, 'nameplate')['value'])
    if (not grade and not roi) or (not diameter and not partial_size) or (not heats and not roi):
        return None
    if not re.search(r'品\s*名|产品名称|热轧带肋钢筋', plate_text):
        return None
    issues = output_quality(checkpoint)
    if grade_suspect or grade_ocr_candidates:
        issues.append('nameplate_grade_ocr_suspect')
    if not grade:
        issues.append('nameplate_grade_unreadable')
    if not diameter:
        issues.append('nameplate_diameter_unreadable')
    candidates = []
    if not heats:
        # A cropped label may be cut to just "号"; preserve possible literals
        # without claiming this is a confirmed heat/lot field.
        candidates = [v for v in dict.fromkeys(re.findall(
            r'(?m)^\s*号\s*[:：]?\s*([A-Z]+\d[A-Z0-9/-]+)', plate_text)) if v not in grades]
        issues.append('nameplate_heat_label_unreadable')
    manufacturer_heading = _company_heading(checkpoint, plate_text, 'nameplate',
        first_line=1 + int(checkpoint.get('text_line_offset', 0)))
    manufacturer = manufacturer_heading['value']
    issues.extend(manufacturer_heading['issues'])
    if not roi:
        issues.append('physical_card_boundary_not_verified')
    if (checkpoint.get('source_region') or {}).get('physical_card_boundary_verified') is False:
        issues.append('physical_card_boundary_not_verified')
    return dict(candidate_id=f'glm-plate:p{checkpoint["page_no"]}:plain:' + str(checkpoint.get('region_id', 'full')),
                page_no=checkpoint['page_no'], grade=grade, diameter_mm=diameter,
                heat_lots=heats, heat_lot_candidates=candidates, manufacturer=manufacturer,
                smelting_heat_lots=smelting if batches and smelting and heats == batches else [],
                grade_ocr_candidates=grade_ocr_candidates,
                manufacturer_heading=manufacturer_heading,
                scope='explicit_roi' if roi else 'model_plain_text', issues=issues,
                evidence=location(checkpoint, source='plain_text_labels'))


def _plain_plates(checkpoint):
    """Read independent card label groups before or after a delivery form.

    Some rotated pages are proposed as one broad physical-nameplate region even
    though two cards are printed side by side.  The native output repeats the
    manufacturer heading at the start of each card.  Split only on that
    company heading and product label, keep the original ROI/page evidence,
    and let the normal single-card parser validate each segment.  A delivery
    heading before the product label excludes its supplier from card starts.
    We never infer pixel sub-boxes from text, including when a wide delivery
    ROI also contains one or more cards.
    """
    text = checkpoint.get('raw_text') or ''
    if _certificate_text(text) or any(a in token(text) for a in
                                      ('进场数量清单', '进场检验记录')):
        plate = _plain_plate(checkpoint)
        return [plate] if plate else []
    lines = text.splitlines(keepends=True)
    starts = []
    offset = 0
    for index, line in enumerate(lines):
        heading = bool(_company_names(line))
        following_lines = []
        for following_line in lines[index + 1:index + 13]:
            if _company_names(following_line):
                break
            following_lines.append(following_line)
        following = ''.join(following_lines)
        product = re.search(r'产品\s*名称|品\s*名\s*[:：]?|COMMODITY\s*NAME|APPELLATION', following, re.I)
        delivery = re.search(r'(?m)^\s*(?:送货单|发货单|供货单)\s*$', following)
        if heading and product and (not delivery or product.start() < delivery.start()):
            starts.append((offset, index + 1))
        offset += len(line)
    # A normal one-card text retains its existing evidence.  A card after a
    # delivery must be parsed from its own company header, rather than the
    # prefix before the delivery title.
    mixed = bool(re.search(r'(?m)^\s*(?:送货单|发货单|供货单)\s*$', text))
    if not starts or (len(starts) == 1 and not mixed):
        plate = _plain_plate(checkpoint)
        return [plate] if plate else []
    segments = []
    for index, (start, first_line) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        segment = text[start:end].rstrip()
        if segment:
            child = dict(checkpoint, raw_text=segment,
                         text_line_offset=first_line - 1,
                         mixed_form_segment=mixed,
                         split_card_index=index + 1, split_card_count=len(starts))
            plate = _plain_plate(child)
            if plate:
                plate['candidate_id'] = (f'glm-plate:p{checkpoint["page_no"]}:split:{index}:'
                                         + str(checkpoint.get('region_id', 'full')))
                issues = ['mixed_roi_card_split_review'] if len(starts) > 1 else []
                if mixed:
                    issues += ['mixed_form_nameplate_text_segment',
                               'physical_card_boundary_not_verified']
                plate['issues'] = list(dict.fromkeys(plate.get('issues', []) + issues))
                plate['split_card_index'] = index + 1
                plate['split_card_count'] = len(starts)
                plate.setdefault('evidence', {}).update(
                    logical_card_index=index + 1, logical_card_count=len(starts),
                    text_segment_start_line=first_line,
                    text_segment_end_line=first_line + segment.count('\n'),
                    raw_text=segment)
                segments.append(plate)
    return segments


def _certificate_plain_numbers(text):
    """Read certificate-specific labels without treating a form code as its No.

    In native GLM text the actual certificate No. can follow a separate fixed
    ``编号:T100-...`` form identifier.  Only a standalone No. label is accepted;
    contract/license numbers and literal punctuation within a No. stay intact.
    """
    numbers = _plain_labels(text, ALIASES['certificate_no'] + ('证书号',))
    numbers.extend(_plain_labels(text, ('CERTIFICATE NO', 'CERTIFICATE NUMBER')))
    normalized = unicodedata.normalize('NFKC', text)
    numbers.extend(match[1] for match in re.finditer(
        r'(?im)^\s*NO\s*[.:]\s*([A-Z0-9][A-Z0-9._/-]{2,})', normalized))
    return numbers


def _certificate_heat_identifier(value):
    """A literal heat identifier cannot be a decimal weight/test result."""
    heat = identifier(value)
    return heat if heat and not re.fullmatch(r'\d+\.\d+', heat) else ''


def _certificate_plain_row_heats(checkpoint):
    """Read local heat/grade/size rows, including vertically emitted cells.

    The column labels establish the heat's role.  A grade-shaped OCR error is
    retained and flagged; it never changes the neighbouring literal heat No.
    Nor may unrelated numeric test columns become heats without a local
    grade/size row or a heat/explicit-diameter pair below the table headers.
    """
    lines = [(i + 1, clean(value)) for i, value in
             enumerate(checkpoint['raw_text'].splitlines()) if clean(value)]
    text = token(checkpoint['raw_text'])
    if not (any(token(label) in text for label in ALIASES['heat_lot_no']) and
            any(label in text for label in ('规格', 'DIMENSION', 'SIZE'))):
        return [], []
    # Unknown prefixes are observations, not a replacement for HRB/HPB/etc.
    grade_shape = r'[A-Z]{2,4}\d{3,4}[EB]?'
    dimension = r'[ΦФØ⌀∅]?\d{1,2}(?:\.\d+)?(?:MM)?(?:[X*×]\d+(?:\.\d+)?(?:MM|M)?)?'
    observations, suspect_grades = [], []

    def add(value, grade, start, end, *, rule='certificate_local_heat_grade_size_row', size=''):
        heat = _certificate_heat_identifier(value)
        if (not heat or len(heat) < 6 or re.fullmatch(grade_shape, heat) or
                not re.search(r'\d', heat)):
            return
        source = '\n'.join(line for _, line in lines[start:end])
        line_no = lines[start][0]
        observations.append(dict(value=heat, grade_literal=grade, line_no=line_no,
            raw_text=source, rule=rule, diameter_literal=size,
            evidence=location(checkpoint, line_no=line_no, raw_text=source,
                              source_role='certificate', field='heat_lot_no')))
        if grade and not re.fullmatch(GRADE, grade):
            suspect_grades.append(grade)

    for index, (_, line) in enumerate(lines):
        # A horizontal row can identify its heat even when its grade was read
        # as HBE400E.  Its explicit columns, rather than a corrected grade,
        # establish the role of the first identifier.
        horizontal = re.match(
            r'^(?:\d{1,3}\s+)?([A-Z0-9][A-Z0-9/-]{5,})\s+(' +
            grade_shape + r')\s+\S+', line, re.I)
        if horizontal:
            add(horizontal[1], token(horizontal[2]), index, index + 1)
        stacked_size = re.match(r'^([A-Z0-9][A-Z0-9/-]{5,})\s+(' + grade_shape + r')$', line, re.I)
        if (stacked_size and index + 1 < len(lines) and
                re.match(r'^\d{1,2}(?:\.\d+)?(?:\s|$)', lines[index + 1][1])):
            add(stacked_size[1], token(stacked_size[2]), index, index + 2,
                size=lines[index + 1][1].split()[0])
        # Some manufacturers put a shared grade below the table, so the
        # actual rows are heat / explicit diameter / count / weight.  The
        # diameter symbol and the preceding heat/size headers establish this
        # local two-cell role without requiring an adjacent grade.
        preceding = token('\n'.join(value for _, value in lines[:index]))
        table_headers_seen = (any(token(label) in preceding for label in ALIASES['heat_lot_no']) and
                              any(label in preceding for label in ('规格', 'DIMENSION', 'SIZE')))
        # Bilingual lot / smelting heat / dimension columns. The leading
        # alphabetic batch and following numeric furnace are distinct keys;
        # nameplates in these forms print the lot, not the furnace column.
        if (table_headers_seen and 'LOTNO' in preceding and 'HEATNO' in preceding and
                index+2 < len(lines) and re.fullmatch(r'[A-Z]{1,4}\d{6,}', token(line)) and
                re.fullmatch(r'\d{6,14}', token(lines[index+1][1])) and
                re.fullmatch(dimension,token(lines[index+2][1]))):
            add(line,'',index,index+3,rule='certificate_lot_heat_dimension_columns',size=lines[index+2][1])
        # Batch / smelting heat / diameter are three separate table columns.
        # Do not interpret the long numeric smelting heat as the diameter.
        if (table_headers_seen and '批号' in preceding and '冶炼炉号' in preceding
                and index + 2 < len(lines) and re.fullmatch(r'\d{8,14}', token(lines[index+1][1]))
                and re.fullmatch(r'[ΦФØ⌀∅]?\d{1,2}(?:\.\d+)?', token(lines[index+2][1]))
                and re.fullmatch(r'\d{6,8}[A-Z]\d{2,6}', token(line))):
            add(line, '', index, index+3, rule='certificate_batch_smelting_diameter_columns',
                size=lines[index+2][1])
        explicit_size = r'[ΦФØ⌀∅]\d{1,2}(?:\.\d+)?(?:MM)?(?:[/X*×]\d+(?:\.\d+)?(?:MM|M)?)?'
        # Keep spaces in the horizontal row so its two columns stay separate.
        horizontal_size = re.match(
            r'^([A-Z0-9][A-Z0-9/-]{5,})\s+(' + explicit_size + r')(?:\s|$)',
            unicodedata.normalize('NFKC', line).upper(), re.I)
        if table_headers_seen and horizontal_size:
            add(horizontal_size[1], '', index, index + 1,
                rule='certificate_local_heat_size_row', size=horizontal_size[2])
        if (table_headers_seen and index + 1 < len(lines) and
                re.fullmatch(explicit_size, token(lines[index + 1][1]))):
            add(line, '', index, index + 2, rule='certificate_local_heat_size_row',
                size=lines[index + 1][1])
        if table_headers_seen and index + 1 < len(lines):
            # Heat on its own line, followed by a grade (including a suspect
            # OCR prefix) and the diameter/length cells on the next line.
            next_row = re.match(r'^(' + grade_shape + r')\s+(\d{1,2}(?:\.\d+)?)(?:\s|$)',
                                lines[index + 1][1], re.I)
            if next_row:
                add(line, token(next_row[1]), index, index + 2, size=next_row[2])
        if index + 2 >= len(lines):
            continue
        current, below, size = (token(lines[i][1]) for i in range(index, index + 3))
        if (re.fullmatch(grade_shape, below) and
                re.match(r'^\d{1,2}(?:\.\d+)?\s+', lines[index + 2][1])):
            add(current, below, index, index + 3, size=lines[index + 2][1].split()[0])
        if not re.fullmatch(dimension, size):
            continue
        if re.fullmatch(grade_shape, current):
            add(below, current, index, index + 3)
        elif re.fullmatch(grade_shape, below):
            add(current, below, index, index + 3)
    return observations, list(dict.fromkeys(suspect_grades))


def _plain_certificate(checkpoint, parsed):
    text = checkpoint['raw_text']
    certs = _certificate_plain_numbers(text)
    issues = output_quality(checkpoint)
    if not certs:
        # These observed OCR spellings are retained as explicit candidates and
        # flagged.  Generic "编号" is not a certificate number (it is often a
        # fixed form code such as GH/JL-JX-45).
        certs = _plain_labels(text, ('质编号', '货编号'))
        if certs:
            issues.append('certificate_label_ocr_variant')
    heats = _plain_labels(text, ALIASES['heat_lot_no'])
    for table in parsed['tables']:
        heats.extend(_pairs(table, ALIASES['heat_lot_no']))
        certs.extend(_pairs(table, ALIASES['certificate_no']))
    row_heat_observations, suspect_grades = _certificate_plain_row_heats(checkpoint)
    heats.extend(value['value'] for value in row_heat_observations)
    # A heat under a column header is commonly separated from that header by
    # many mechanics labels.  Accept only a row beginning with an identifier
    # immediately followed by a steel grade, or by a stacked valid diameter
    # when the form contains a single grade elsewhere.
    lines = [clean(v) for v in text.splitlines() if clean(v)]
    for index, line in enumerate(lines):
        row = re.match(r'^(?:\d{1,3}\s+)?([A-Z0-9][A-Z0-9/-]{4,})\s+((?:HRBF|HRB|HPB|RRB|CRB)\d{3,4}[EB]?)\b', line, re.I)
        if row:
            heats.append(row[1])
        elif (identifier(line) and len(line) >= 6 and index+2 < len(lines) and
              re.fullmatch(GRADE, token(lines[index+1]))):
            heats.append(line)
        elif (identifier(line) and len(line) >= 6 and index+1 < len(lines) and
              re.fullmatch(r'\d{1,2}(?:\.\d+)?', lines[index+1]) and
              len(set(re.findall(GRADE, token(text)))) == 1 and
              re.search(r'批号', text)):
            heats.append(line)
        else:
            # Lot, diameter, length and numeric test columns, with a shared
            # grade above the table.  Keep only an explicit leading lot token.
            row = re.match(r'^([A-Z0-9][A-Z0-9/-]{5,})\s+(\d{1,2}(?:\.\d+)?)\s+(\d{1,2}(?:\.\d+)?)\s+', line)
            if row and re.search(r'批号', text) and re.search(r'规格', text):
                heats.append(row[1])
    certs = sorted({identifier(v) for v in certs if identifier(v)})
    heats = sorted({_certificate_heat_identifier(v) for v in heats if _certificate_heat_identifier(v) and
                    not re.fullmatch(r'[A-Z]{2,4}\d{3,4}[EB]', token(v))})
    if not certs:
        issues.append('certificate_number_unreadable')
    if not heats:
        issues.append('certificate_heat_unreadable')
    grades = sorted(set(re.findall(GRADE, token(text))))
    if suspect_grades or any(not re.fullmatch(r'(?:HRBF|HRB|HPB|RRB|CRB)(?:235|300|335|400|500|600|700)[EB]?', value)
           for value in grades):
        issues.append('certificate_grade_ocr_suspect')
    if re.search(r'(.{1,8})\1{30,}', token(text)):
        issues.append('glm_repeated_text')
    return dict(page_no=checkpoint['page_no'], certificate_numbers=certs,
                heat_lots=heats, grade_candidates=grades, grade_ocr_candidates=suspect_grades,
                heat_observations=row_heat_observations, issues=issues, evidence=location(checkpoint))


def _certificate_with_region_views(checkpoint, parsed):
    """Keep one certificate entity and its same-page crop observations.

    A table crop may omit the title/No. present on its full page. It still
    supplies literal labelled table cells for that certificate, never a
    separate certificate candidate or text copied from an adjacent page.
    Conflicting observations remain candidates on the same entity.
    """
    result = _plain_certificate(checkpoint, parsed)
    views = [result]
    for region in checkpoint.get('regions', []):
        text = region.get('raw_text', '')
        if not text or re.search(r'(?m)^\s*(?:送货单|发货单|供货单)\s*$', text):
            continue
        child = dict(checkpoint, raw_text=text, source_region=region,
                     truncated=region.get('truncated', False))
        for key in ('checkpoint_reference', 'checkpoint_sha256'):
            if key in region:
                child[key] = region[key]
        views.append(_plain_certificate(child, parse_tables(text)))
    result['evidence_views'] = [view['evidence'] for view in views]
    result['certificate_number_observations'] = [dict(value=value, evidence=view['evidence'])
        for view in views for value in view['certificate_numbers']]
    for key in ('certificate_numbers', 'heat_lots', 'grade_candidates', 'grade_ocr_candidates'):
        result[key] = list(dict.fromkeys(value for view in views for value in view.get(key, [])))
    result['heat_observations'] = [observation for view in views
                                   for observation in view.get('heat_observations', [])]
    # Preserve quality problems on the views; absence in one view is not
    # absence in the entity. A disagreement is visible and never corrected.
    result['issues'] = list(dict.fromkeys(issue for view in views for issue in view['issues']
        if not (issue == 'certificate_number_unreadable' and result['certificate_numbers'])
        and not (issue == 'certificate_heat_unreadable' and result['heat_lots'])))
    if len(result['certificate_numbers']) > 1:
        result['issues'].append('certificate_number_view_conflict')
    return result


def _plate(checkpoint, table, *, roi=False):
    rows = table['grid']
    flat = '\n'.join(' '.join(r) for r in rows)
    form_text = table.get('context', '')+'\n'+flat
    # A geometry proposal alone never turns a certificate/delivery table into a card.
    # A certificate title often sits outside its table.  Keep this decision
    # local to this table/context so an independent card ROI on a mixed page
    # can still be read.
    if _certificate_text(form_text) or any(t in token(form_text) for t in (
            '质量证明书', '进场数量清单', '进场检验记录', '送货单', '发货单') + _DETECTION_REPORT_TITLES):
        return None
    heats = [identifier(v) for v in _pairs(table, ALIASES['heat_lot_no'])]
    heats = sorted(set(v for v in heats if v))
    grade_values = _pairs(table, ALIASES['grade'])
    size_values = _pairs(table, ALIASES['specification']+('SIZE', 'DIAMETER'))
    grade, diameter = specification(' '.join(size_values), ' '.join(grade_values))
    if not grade:
        matches = sorted(set(re.findall(GRADE, token(flat))))
        grade = matches[0] if len(matches) == 1 else ''
    if not heats or not grade or (not size_values and not roi):
        return None
    manufacturer_heading = _company_heading(checkpoint, form_text, 'nameplate')
    return dict(candidate_id=f'glm-plate:p{checkpoint["page_no"]}:t{table["table_index"]}:' + str(checkpoint.get('region_id', 'full')),
        page_no=checkpoint['page_no'], grade=grade, diameter_mm=diameter,
        heat_lots=heats, manufacturer=manufacturer_heading['value'], manufacturer_heading=manufacturer_heading,
        scope='explicit_roi' if roi else 'model_logical_table',
        issues=list(table['issues']) + manufacturer_heading['issues'] + ([] if roi else ['physical_card_boundary_not_verified']),
        evidence=location(checkpoint, table_index=table['table_index']))


def _delivery(checkpoint, table):
    text = table.get('context', '')+'\n'+'\n'.join(' '.join(r) for r in table['grid'])
    if not re.search(r'送货单|发货单|供货单', text):
        return None
    # Only a company before/in the form heading is a supplier candidate.
    title = re.search(r'送货单|发货单|供货单', text)
    heading, heading_line = _delivery_heading_text(text, title)
    supplier_heading = _company_heading(checkpoint, heading, 'delivery', first_line=heading_line)
    numbers = list(dict.fromkeys(re.findall(r'(?i)(?<![A-Za-z])NO\s*[.:：]?\s*([A-Za-z0-9]*\d[A-Za-z0-9]*)', text)))
    rows = []
    for ri, row in enumerate(table['grid']):
        grade, diameter = specification(' '.join(row))
        if grade and diameter:
            rows.append(dict(grade=grade, diameter_mm=diameter, logical_row=ri))
    return dict(candidate_id=f'glm-delivery:p{checkpoint["page_no"]}:t{table["table_index"]}',
        page_no=checkpoint['page_no'], supplier=supplier_heading['value'], supplier_heading=supplier_heading,
        delivery_no=numbers[0] if len(numbers) == 1 else '', delivery_number_candidates=numbers,
        rows=rows, issues=list(table['issues'])+supplier_heading['issues'], evidence=location(checkpoint, table_index=table['table_index']))


def extract_document(checkpoints):
    """Build experimental resources only from new GLM text, not baseline values."""
    output = dict(records=[], nameplates=[], deliveries=[], certificates=[], issues=[], pages=[])
    for checkpoint in checkpoints:
        # Explicit upstream pages have already been parsed by their own backend.
        # Recovery reparses attachments only; it preserves upstream records.
        if checkpoint.get('upstream_role') in ('entry', 'submission'):
            output['pages'].append(dict(page_no=checkpoint['page_no'],
                upstream_role=checkpoint['upstream_role'], full_page_available=True))
            continue
        if checkpoint.get('status') == 'failed' or (not checkpoint.get('raw_text') and not checkpoint.get('regions')):
            output['issues'].append(dict(code='glm_page_failed', **location(checkpoint)))
            continue
        parsed = parse_tables(checkpoint.get('raw_text', ''))
        region = checkpoint.get('source_region') or {}
        roi = bool(region.get('polygon_page'))
        if not roi:
            table_records = _entry_tables(checkpoint, parsed, output['issues'])
            entry_records = table_records or _plain_entry_records(checkpoint)
            output['records'].extend(entry_records)
            if (not entry_records and any(t in token(checkpoint.get('raw_text', ''))
                                         for t in ('进场数量清单', '进场检验记录'))):
                output['issues'].append(dict(code='entry_table_not_parsed', **location(checkpoint),
                    reason='进场表标题存在，但表格/纯文本行未能可靠解析；保留整页原文待核验'))
            output['deliveries'].extend(_plain_delivery(checkpoint))
            output['nameplates'].extend(_plain_plates(checkpoint))
            output['pages'].append(dict(page_no=checkpoint['page_no'], table_count=len(parsed['tables']),
                text_characters=len(checkpoint.get('raw_text', '')), truncated=checkpoint.get('truncated', False),
                full_page_available=bool(checkpoint.get('raw_text'))))
        else:
            output['nameplates'].extend(_plain_plates(checkpoint))
        # Region OCR is stored inside the page checkpoint rather than as a
        # second checkpoint file.  Reuse the same parser with the measured ROI
        # as its evidence location; this is essential for pages containing
        # several physical nameplates.
        for region in checkpoint.get('regions', []):
            if not region.get('raw_text'):
                continue
            region_checkpoint = dict(checkpoint, raw_text=region['raw_text'],
                                      source_region=region, region_id=region.get('region_id'),
                                      truncated=region.get('truncated', False))
            for key in ('checkpoint_reference', 'checkpoint_sha256'):
                if key in region:
                    region_checkpoint[key] = region[key]
            output['deliveries'].extend(_plain_delivery(region_checkpoint))
            if region.get('truncated'):
                output['issues'].append(dict(code='glm_output_truncated',
                    **location(region_checkpoint), reason='裁区生成达到token上限，字段仅为候选，需按该区域核验'))
            region_plates = _plain_plates(region_checkpoint)
            # A certificate's table crop often omits its title, yet repeats
            # product/grade/size labels. It is not an independent nameplate.
            # Keep genuine separately proposed card inputs on mixed pages.
            if (_certificate_text(checkpoint.get('raw_text', '')) and
                    region.get('kind') == 'form_candidate' and
                    not _company_heading(region_checkpoint, region['raw_text'], 'nameplate')['value']):
                region_plates = []
            if region_plates:
                output['nameplates'].extend(region_plates)
            elif _certificate_text(region['raw_text']):
                output['issues'].append(dict(code='nameplate_roi_rejected_certificate',
                    **location(region_checkpoint), reason='几何候选含证书编号及化学/力学表头，不能作为铭牌'))
            if re.search(r'(.{1,8})\1{30,}', token(region['raw_text'])):
                output['issues'].append(dict(code='glm_repeated_text', **location(region_checkpoint),
                    reason='输出存在长段重复，truncated=false 也不能证明内容可靠'))
        for table in parsed['tables']:
            plate = _plate(checkpoint, table, roi=roi)
            if plate: output['nameplates'].append(plate)
            delivery = _delivery(checkpoint, table) if not roi else None
            if delivery: output['deliveries'].append(delivery)
        if (not roi and not any(t in token(checkpoint.get('raw_text', ''))
                                for t in ('进场数量清单', '进场检验记录')) and
                (_certificate_text(checkpoint.get('raw_text', '')) or
                 any(_certificate_text(r.get('raw_text','')) for r in checkpoint.get('regions', [])))):
            output['certificates'].append(_certificate_with_region_views(checkpoint, parsed))
    # A scan can have the left part of the batch label physically cut off.
    # Keep the literal as a candidate unless a single certificate explicitly
    # confirms it. This never creates a heat from a number/weight token.
    for plate in output['nameplates']:
        if (plate.get('evidence', {}).get('kind') != 'independent_panel_view' or
                plate.get('heat_lots') or len(plate.get('heat_lot_candidates', [])) != 1):
            continue
        heat = plate['heat_lot_candidates'][0]
        certs = [c for c in output['certificates'] if heat in c.get('heat_lots', [])
                 and len(c.get('certificate_numbers', [])) == 1]
        if len(certs) == 1:
            plate['heat_lots'] = [heat]
            plate['issues'].append('nameplate_damaged_heat_label_confirmed_by_certificate')
    output['nameplates'] = merge_nameplate_views(output['nameplates'])
    # A measured crop can miss its own heading.  Use a full-page header only
    # for a single identified card with the same unique heat and grade.  This
    # is not a same-page or neighbouring-card company fallback.
    for checkpoint in checkpoints:
        cards = [p for p in output['nameplates'] if p['page_no'] == checkpoint['page_no']]
        if len(cards) != 1 or cards[0].get('manufacturer') or not checkpoint.get('raw_text'):
            continue
        card = cards[0]
        # Count proposals even when their OCR is empty/failed or their fields
        # could not be parsed.  One parsed card does not prove one physical
        # card.  Unknown ROI kinds are conservatively possible cards; explicit
        # delivery regions are excluded from this count.
        possible_card_regions = [r for r in checkpoint.get('regions', [])
            if r.get('kind') != 'delivery_note_candidate' and
            not re.search(r'delivery(?:_note)?', str(r.get('region_id', '')), re.I)]
        if len(possible_card_regions) > 1:
            card['issues'].append('manufacturer_full_page_header_ambiguous_cards')
            continue
        text = checkpoint['raw_text']
        full_heats = set(_plain_labels(text, ALIASES['heat_lot_no']))
        full_grades = set(re.findall(GRADE, token(text)))
        if (len(set(card['heat_lots'])) != 1 or full_heats != set(card['heat_lots']) or
                full_grades != {card['grade']} or _certificate_text(text)):
            continue
        heading = _company_heading(checkpoint, text, 'nameplate')
        if heading['value']:
            heading['recovery_rule'] = 'single_card_same_unique_heat_and_grade_full_page_header'
            card['manufacturer'] = heading['value']
            card['manufacturer_heading'] = heading
            card['issues'] = [i for i in card['issues'] if i != 'nameplate_manufacturer_unreadable']
            card['issues'].extend(heading['issues']+['manufacturer_header_recovered_from_full_page'])
    output['deliveries'] = merge_delivery_views(output['deliveries'])
    output['note'] = 'Experimental native GLM parsing; page/ROI locations only, no invented pixel cell boxes; human review pending.'
    return output


def merge_nameplate_views(nameplates):
    """Reconcile full-page and ROI readings of the same uniquely keyed card.

    A broad ROI can contain two cards and misread one field even when the
    full-page text reads it correctly. Join views only by one identical heat
    lot and diameter on the same PDF page. Keep the ROI as the primary
    location, preserve both OCR spellings and flag disagreements. Unmatched
    full-page readings on pages with ROIs remain candidates rather than being
    silently discarded.
    """
    values = [deepcopy(item) for item in nameplates]
    # Independently measured panel inputs supersede broad mixed-page views
    # only for a unique same-page single heat. Preserve the other observations
    # (including incorrect size readings) and their locations for review.
    panels = [p for p in values if p.get('evidence', {}).get('kind') == 'independent_panel_view'
              and len(p.get('heat_lots', [])) == 1 and p.get('grade') and p.get('diameter_mm')]
    consumed_panels = set()
    for index, broad in enumerate(values):
        evidence = broad.get('evidence', {})
        if evidence.get('kind') == 'independent_panel_view' or len(broad.get('heat_lots', [])) != 1:
            continue
        if not (evidence.get('logical_card_count', 1) > 1 or broad.get('scope') == 'model_plain_text'
                or evidence.get('kind') == 'form_candidate'):
            continue
        choices = [p for p in panels if p['page_no'] == broad['page_no']
                   and p['heat_lots'] == broad['heat_lots']]
        if len(choices) != 1:
            continue
        target = choices[0]
        polygon, panel = evidence.get('polygon_page'), target['evidence'].get('polygon_page')
        if polygon:
            ax,ay,ar,ab = min(x for x,y in polygon),min(y for x,y in polygon),max(x for x,y in polygon),max(y for x,y in polygon)
            bx,by,br,bb = min(x for x,y in panel),min(y for x,y in panel),max(x for x,y in panel),max(y for x,y in panel)
            area = (br-bx)*(bb-by)
            if not area or max(0,min(ar,br)-max(ax,bx))*max(0,min(ab,bb)-max(ay,by)) < area*.65:
                continue
        target.setdefault('evidence_views', [deepcopy(target['evidence'])]).extend(
            deepcopy(broad.get('evidence_views', [evidence])))
        target.setdefault('source_candidate_ids', [target['candidate_id']]).append(broad['candidate_id'])
        target.setdefault('superseded_view_observations', []).append(dict(
            grade=broad.get('grade'), diameter_mm=broad.get('diameter_mm'),
            manufacturer=broad.get('manufacturer'), evidence=deepcopy(evidence)))
        if any(broad.get(k) and broad.get(k) != target.get(k) for k in ('grade', 'diameter_mm', 'manufacturer')):
            target['issues'] = list(dict.fromkeys(target.get('issues', []) + ['nameplate_panel_view_conflict']))
        consumed_panels.add(index)
    values = [v for i,v in enumerate(values) if i not in consumed_panels]
    seen = set()
    repeated_roi_views = {}
    deduplicated = []
    for item in values:
        signature = (item.get('candidate_id'), tuple(item.get('heat_lots', [])),
                     item.get('diameter_mm'), item.get('grade'))
        if signature in seen:
            continue
        seen.add(signature)
        evidence = item.get('evidence', {})
        if (item.get('scope') == 'explicit_roi' and evidence.get('polygon_page') and
                'mixed_form_nameplate_text_segment' in item.get('issues', [])):
            # A delivery entity and its row ROI can be the same measured crop
            # with identical OCR.  They are views of one label group, not
            # separate physical cards.  Differing polygons or OCR text remain
            # independent candidates for final review.
            key = (item.get('page_no'), tuple(tuple(point) for point in evidence['polygon_page']),
                   tuple(item.get('heat_lots', [])), item.get('diameter_mm'), item.get('grade'),
                   item.get('manufacturer'), evidence.get('raw_text'))
            if key in repeated_roi_views:
                existing = repeated_roi_views[key]
                views = existing.setdefault('evidence_views', [deepcopy(existing.get('evidence', {}))])
                if evidence not in views:
                    views.append(deepcopy(evidence))
                sources = existing.setdefault('source_candidate_ids', [existing['candidate_id']])
                if item['candidate_id'] not in sources:
                    sources.append(item['candidate_id'])
                continue
            repeated_roi_views[key] = item
        deduplicated.append(item)
    values = deduplicated
    # Recovery viewports can all be emitted as explicit ROI candidates.  When
    # one page has exactly one complete card key (heat, grade, diameter and
    # manufacturer), treat those ROIs as views of that card even if neither
    # viewport is labelled ``model_plain_text``.  The OCR evidence for every
    # view is retained; this does not invent a physical card boundary.
    grouped = {}
    for item in values:
        key = (item.get('page_no'), tuple(item.get('heat_lots', [])),
               item.get('grade'), item.get('diameter_mm'), item.get('manufacturer'))
        grouped.setdefault(key, []).append(item)
    merged_values = []
    for key, group in grouped.items():
        if len(group) <= 1 or not key[1] or not key[2] or not key[3] or not key[4]:
            merged_values.extend(group)
            continue
        # A same-key group is safe only when it is the sole physical key on
        # that page.  Distinct cards sharing a heat remain separate.
        page = key[0]
        page_keys = { (x.get('page_no'), tuple(x.get('heat_lots', [])),
                       x.get('grade'), x.get('diameter_mm'), x.get('manufacturer'))
                      for x in values if x.get('page_no') == page }
        if len(page_keys) != 1:
            merged_values.extend(group)
            continue
        evidence_group = [view for item in group for view in
                          item.get('evidence_views', [item.get('evidence', {})])]
        anchors = [{(e.get('checkpoint_reference'), e.get('text_segment_start_line'), e.get('text_segment_end_line'))
                    for e in item.get('evidence_views', [item.get('evidence', {})])
                    if e.get('precision') == 'page' and e.get('text_segment_start_line')}
                   for item in group]
        common_anchor = bool(anchors and set.intersection(*anchors))
        def overlapping(a, b):
            polygons = [x.get('evidence', {}).get('polygon_page') for x in (a, b)]
            if not all(polygons):
                return False
            boxes = [(min(x for x, y in p), min(y for x, y in p),
                      max(x for x, y in p), max(y for x, y in p)) for p in polygons]
            (ax, ay, ar, ab), (bx, by, br, bb) = boxes
            smaller = min((ar-ax)*(ab-ay), (br-bx)*(bb-by))
            intersection = max(0, min(ar, br)-max(ax, bx))*max(0, min(ab, bb)-max(ay, by))
            return smaller > 0 and intersection/smaller >= .9
        common_region = all(overlapping(a, b) for i, a in enumerate(group) for b in group[i+1:])
        if (not any(e.get('kind') == 'attachment_recovery_view' for e in evidence_group) or
                not (common_anchor or common_region) or
                (not common_anchor and any(e.get('logical_card_count', 1) > 1 for e in evidence_group)) or
                any('physical_card_boundary_not_verified' not in item.get('issues', []) for item in group)):
            merged_values.extend(group)
            continue
        target = deepcopy(group[0])
        target['evidence_views'] = []
        target['source_candidate_ids'] = []
        target['issues'] = []
        for view in group:
            for evidence in view.get('evidence_views', [view.get('evidence', {})]):
                if evidence and evidence not in target['evidence_views']:
                    target['evidence_views'].append(deepcopy(evidence))
            for candidate_id in view.get('source_candidate_ids', [view.get('candidate_id')]):
                if candidate_id and candidate_id not in target['source_candidate_ids']:
                    target['source_candidate_ids'].append(candidate_id)
            target['issues'].extend(view.get('issues', []))
        target['issues'] = list(dict.fromkeys(target['issues']))
        target['candidate_id'] = target['source_candidate_ids'][0]
        target['certificate_view_resolution'] = 'same_page_unique_attachment_key_views'
        merged_values.append(target)
    values = merged_values
    consumed = set()
    for roi_index, roi in enumerate(values):
        if roi.get('scope') != 'explicit_roi':
            continue
        roi_heats = sorted(set(roi.get('heat_lots', [])))
        if len(roi_heats) != 1 or not roi.get('diameter_mm'):
            continue
        full_matches = [(index, item) for index, item in enumerate(values)
            if index != roi_index and item.get('scope') == 'model_plain_text'
            and item.get('page_no') == roi.get('page_no')
            and sorted(set(item.get('heat_lots', []))) == roi_heats
            and item.get('diameter_mm') == roi.get('diameter_mm')]
        if len(full_matches) != 1:
            continue
        full_index, full = full_matches[0]
        consumed.add(full_index)
        views = roi.get('evidence_views', [deepcopy(roi.get('evidence', {}))])
        for view in full.get('evidence_views', [full.get('evidence', {})]):
            if view not in views:
                views.append(deepcopy(view))
        roi['evidence_views'] = views
        roi['grade_observations'] = [dict(value=roi.get('grade', ''),
                source='roi', evidence=deepcopy(roi.get('evidence', {}))),
            dict(value=full.get('grade', ''), source='full_page',
                 evidence=deepcopy(full.get('evidence', {})))]
        observed_grades = [item['value'] for item in roi['grade_observations'] if item['value']]
        valid_full_grade = (full.get('grade') and
                            re.fullmatch(GRADE, token(full.get('grade'))))
        if not roi.get('grade') and valid_full_grade:
            roi['grade'] = full['grade']
            roi['grade_source'] = 'same_page_full_page_view_by_exact_heat_and_diameter'
            roi['issues'] = [issue for issue in roi.get('issues', [])
                             if issue != 'nameplate_grade_unreadable']
            roi['issues'] = list(dict.fromkeys(roi.get('issues', []) +
                ['nameplate_grade_recovered_from_matching_full_page_view']))
        if len(set(observed_grades)) > 1 or (roi.get('grade_ocr_candidates') and valid_full_grade):
            roi['grade_ocr_candidates'] = list(dict.fromkeys(
                roi.get('grade_ocr_candidates', []) + [value for value in observed_grades
                    if value != roi.get('grade')]))
            roi['issues'] = list(dict.fromkeys(roi.get('issues', []) +
                ['nameplate_grade_view_conflict']))
        full_issues = [issue for issue in full.get('issues', [])
                       if issue != 'physical_card_boundary_not_verified']
        roi['issues'] = list(dict.fromkeys(roi.get('issues', []) + full_issues))
    result = [item for index, item in enumerate(values) if index not in consumed]
    # Full-page reconciliation adds the common text-segment anchor used by
    # recovery ROIs. Revisit the groups after consuming those page views.
    return merge_nameplate_views(result) if consumed else result


def merge_delivery_views(deliveries):
    """Merge repeated views of one form while preserving conflicting numbers.

    Exact page/number views are merged first. A full-page view and one ROI view
    can also be bound to the same physical form when they uniquely share a
    supplier and the same long delivery-number suffix with a one-letter prefix
    difference. The form and supplier may then be associated, but the number
    remains empty with both OCR readings retained for review. Separate forms
    stay separate when the view pairing is not unique.
    """
    deliveries = deepcopy(deliveries)
    # A missing No. is a field failure, not the absence of a physical form.
    # Bind only a unique full-page/ROI pair with the same observed supplier;
    # multiple titled forms on either view must remain separate candidates.
    for page in {d['page_no'] for d in deliveries}:
        local = [d for d in deliveries if d['page_no'] == page]
        suppliers = {str(d.get('supplier') or '').strip() for d in local if d.get('supplier')}
        if len(suppliers) != 1 or not local or any(not d.get('supplier') for d in local):
            continue
        if len(local) > 1 and all((d.get('evidence') or {}).get('precision') == 'page' for d in local):
            # Multiple full-page titled forms are independent documents; no
            # crop evidence exists to establish that they are repeat views.
            continue
        view_keys = [(d.get('evidence', {}).get('checkpoint_reference'),
                      str(d.get('evidence', {}).get('polygon_page'))) for d in local]
        if len(view_keys) != len(set(view_keys)):
            # Several titled forms seen by the same viewport must remain
            # independent even if their suppliers or numbers agree.
            continue
        # Recovery produces several overlapping views.  They can represent
        # one form when all readable numbers share the same numeric suffix;
        # different suffixes remain separate physical forms.
        suffixes = set()
        for item in local:
            for number in item.get('delivery_number_candidates', []):
                match = re.search(r'(\d{8,})$', str(number))
                if match:
                    suffixes.add(match.group(1))
        if len(suffixes) > 1:
            # A unique page/crop pair with the same explicit goods key is
            # one form even when OCR disagrees on its No.  Preserve both
            # number readings as a field conflict.
            full = [d for d in local if d['evidence'].get('precision') == 'page']
            crops = [d for d in local if d['evidence'].get('precision') == 'region']
            keys = [{(r.get('grade'), r.get('diameter_mm')) for r in d.get('rows', [])
                     if r.get('grade') and r.get('diameter_mm')} for d in local]
            goods = [d.get('form_goods_signatures', []) for d in local]
            same_goods = bool(goods[0] and all(g == goods[0] for g in goods))
            measured = [d for d in crops if d.get('evidence', {}).get('boundary_method') ==
                        'physical_outline_and_table_rules']
            if not ((len(full) == 1 and len(crops) == 1 and len(local) == 2 and
                     keys[0] and keys[0] == keys[1]) or
                    (len(full) == 1 and len(measured) == 1 and same_goods)):
                continue
        shared = next((d.get('delivery_no') for d in local if d.get('delivery_no')), None)
        shared = shared or next((d.get('delivery_number_candidates', [None])[0]
                                 for d in local if d.get('delivery_number_candidates')), None)
        shared = shared or local[0]['candidate_id']
        for item in local:
            item['_form_view_key'] = shared
    merged = {}
    for source in deliveries:
        key = (source['page_no'], source.get('_form_view_key') or source.get('delivery_no') or source['candidate_id'])
        if key not in merged:
            merged[key] = deepcopy(source)
            merged[key]['evidence_views'] = [deepcopy(source['evidence'])]
            merged[key]['supplier_candidates'] = [source['supplier']] if source.get('supplier') else []
            merged[key]['supplier_heading_views'] = [deepcopy(source.get('supplier_heading', {}))]
            merged[key]['delivery_number_observations'] = [dict(value=n,
                precision=source['evidence'].get('precision', 'page'), evidence=deepcopy(source['evidence']),
                candidate_id=source.get('candidate_id'), issues=deepcopy(source.get('issues', [])))
                for n in source.get('delivery_number_candidates', [])]
            continue
        target = merged[key]
        target['delivery_number_observations'].extend(dict(value=n,
            precision=source['evidence'].get('precision', 'page'), evidence=deepcopy(source['evidence']),
            candidate_id=source.get('candidate_id'), issues=deepcopy(source.get('issues', [])))
            for n in source.get('delivery_number_candidates', []))
        numbers = list(dict.fromkeys(target.get('delivery_number_candidates', []) +
                                     source.get('delivery_number_candidates', [])))
        target['delivery_number_candidates'] = numbers
        target['delivery_no'] = numbers[0] if len(numbers) == 1 else ''
        if source['evidence'] not in target['evidence_views']:
            target['evidence_views'].append(deepcopy(source['evidence']))
        suppliers = list(dict.fromkeys(target['supplier_candidates'] +
                                      ([source['supplier']] if source.get('supplier') else [])))
        target['supplier_candidates'] = suppliers
        target.setdefault('supplier_heading_views', []).append(deepcopy(source.get('supplier_heading', {})))
        if len(suppliers) <= 1:
            target['supplier'] = suppliers[0] if suppliers else ''
        elif (target['evidence'].get('precision') == 'page' and target.get('supplier')):
            # Preserve the complete form's header value, mark the disagreement
            # red, and retain the ROI reading instead of erasing useful text.
            pass
        elif source['evidence'].get('precision') == 'page' and source.get('supplier'):
            target['supplier'] = source['supplier']
            target['supplier_heading'] = deepcopy(source.get('supplier_heading', {}))
        else:
            target['supplier'] = ''
        for row in source.get('rows', []):
            if row not in target['rows']:
                target['rows'].append(deepcopy(row))
        target['issues'] = sorted(set(target['issues'] + source['issues']))
        if target['supplier']:
            target['issues'] = [i for i in target['issues'] if i != 'supplier_missing']
        if target.get('delivery_no'):
            target['issues'] = [i for i in target['issues'] if i != 'delivery_number_unreadable']
        if len(numbers) > 1:
            target['issues'] = sorted(set(target['issues'] + ['delivery_number_conflict']))
        if len(suppliers) > 1:
            target['issues'].append('supplier_conflict')
    values = list(merged.values())
    by_page = {}
    consumed = set()
    for item in values:
        by_page.setdefault(item['page_no'], []).append(item)
    for page_values in by_page.values():
        if len(page_values) < 2:
            continue
        page_views = [v for v in page_values if v.get('evidence', {}).get('precision') == 'page']
        region_views = [v for v in page_values if v.get('evidence', {}).get('precision') == 'region']

        def number_values(item):
            return list(dict.fromkeys([str(item.get('delivery_no') or '')] +
                [str(value) for value in item.get('delivery_number_candidates', []) if value]))

        def same_supplier(left, right):
            a, b = str(left.get('supplier') or '').strip(), str(right.get('supplier') or '').strip()
            return bool(a and b and a == b)

        def related_numbers(left, right):
            a_values, b_values = [v for v in number_values(left) if v], [v for v in number_values(right) if v]
            if len(a_values) != 1 or len(b_values) != 1:
                return False
            a, b = a_values[0].upper(), b_values[0].upper()
            a_match, b_match = re.match(r'^([A-Z]+)(\d{8,})$', a), re.match(r'^([A-Z]+)(\d{8,})$', b)
            if not a_match or not b_match or a_match.group(2) != b_match.group(2):
                return False
            prefix_a, prefix_b = a_match.group(1), b_match.group(1)
            return len(prefix_a) == len(prefix_b) and sum(x != y for x, y in zip(prefix_a, prefix_b)) == 1

        possible = [(left, right) for left in page_views for right in region_views
                    if same_supplier(left, right) and related_numbers(left, right)]
        left_counts = {id(left): sum(pair[0] is left for pair in possible) for left in page_views}
        right_counts = {id(right): sum(pair[1] is right for pair in possible) for right in region_views}
        paired = [(left, right) for left, right in possible
                  if left_counts[id(left)] == 1 and right_counts[id(right)] == 1]
        for left, right in paired:
            # Prefer the full-page view as the stable entity identity. Keep all
            # OCR readings and source locations so the number stays reviewable.
            target, source = left, right
            target['evidence_views'] = list(target.get('evidence_views', []))
            for view in source.get('evidence_views', [source.get('evidence')]):
                if view and view not in target['evidence_views']:
                    target['evidence_views'].append(deepcopy(view))
            suppliers = list(dict.fromkeys(target.get('supplier_candidates', []) +
                                            source.get('supplier_candidates', [])))
            target['supplier_candidates'] = suppliers
            target['supplier_heading_views'] = list(target.get('supplier_heading_views', []))
            for view in source.get('supplier_heading_views', [source.get('supplier_heading', {})]):
                if view not in target['supplier_heading_views']:
                    target['supplier_heading_views'].append(deepcopy(view))
            if len(suppliers) == 1:
                target['supplier'] = suppliers[0]
            for row in source.get('rows', []):
                if row not in target.get('rows', []):
                    target.setdefault('rows', []).append(deepcopy(row))
            target_numbers, source_numbers = number_values(target), number_values(source)
            numbers = list(dict.fromkeys([v for v in target_numbers + source_numbers if v]))
            target['delivery_number_candidates'] = numbers
            target['delivery_no'] = numbers[0] if len(numbers) == 1 else ''
            observations = []
            for view, observed_numbers in ((target, target_numbers), (source, source_numbers)):
                view_numbers = [v for v in observed_numbers if v]
                for number in view_numbers:
                    item = dict(value=number, precision=(view.get('evidence') or {}).get('precision', 'page'),
                        evidence=deepcopy(view.get('evidence') or {}),
                        candidate_id=view.get('candidate_id'), issues=deepcopy(view.get('issues', [])))
                    if not any(existing['value'] == item['value'] and
                               existing['precision'] == item['precision'] and
                               existing['candidate_id'] == item['candidate_id'] for existing in observations):
                        observations.append(item)
            target['delivery_number_observations'] = observations
            target['merged_view_candidate_ids'] = list(dict.fromkeys(
                target.get('merged_view_candidate_ids', [target.get('candidate_id')]) +
                source.get('merged_view_candidate_ids', [source.get('candidate_id')])))
            target['issues'] = sorted(set(target.get('issues', []) + source.get('issues', []) +
                (['delivery_number_conflict'] if len(numbers) > 1 else [])))
            if len(suppliers) > 1:
                target['supplier'] = ''
                target['issues'] = sorted(set(target['issues'] + ['supplier_conflict']))
            if target.get('supplier'):
                target['issues'] = [issue for issue in target['issues'] if issue != 'supplier_missing']
            consumed.add(id(source))
        paired_ids = {id(item) for pair in paired for item in pair}
        for item in page_values:
            if page_views and region_views and id(item) not in paired_ids:
                # Keep this on genuinely separate/ambiguous views. A uniquely
                # paired form already carries the number conflict on its target.
                item['issues'] = sorted(set(item.get('issues', []) + ['delivery_number_conflict']))
    values = [item for item in values if id(item) not in consumed]
    return values
