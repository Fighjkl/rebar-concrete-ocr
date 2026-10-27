"""Rebar ledger fields from the selected physical plate, delivery row and certificate.

Saved OCR remains immutable. Specification uses the selected nameplate's grade
and diameter, with its selected delivery row as fallback. Inspection arrival
quantity takes priority over delivery weight; conflicts remain for field review.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import re


COLUMNS = [
    dict(key='material_name', label='材料名称', role='approval', required=True),
    dict(key='specification', label='规格品种', role='nameplate', required=True),
    dict(key='manufacturer', label='生产单位', role='nameplate', required=True),
    dict(key='supplier', label='供应单位', role='delivery', required=True),
    dict(key='supply_date', label='供货日期', role='approval', required=True, type='date'),
    dict(key='usage_location', label='使用部位', role='approval', required=True),
    dict(key='heat_lot_no', label='送货单位编号炉批号', role='nameplate', required=True),
    dict(key='quantity_t', label='验收数量', role='inspection', required=True, type='number', unit='t'),
    dict(key='certificate_no', label='质保书编号', role='certificate', required=True),
    dict(key='file_no', label='文件编号', role='approval', required=False),
]
# A short printed group heading is a valid original value. Keep 集团 last so
# full legal company names are retained when both forms occur in the OCR.
COMPANY = re.compile(r'[\u4e00-\u9fffA-Za-z（）()·]{2,}(?:有限责任公司|股份有限公司|有限公司|集团)')
# Longest labels must come first: stripping ``生产厂`` from ``生产厂家``
# would leave 家 attached to an otherwise valid, unpunctuated company name.
COMPANY_LABEL = re.compile(r'^(?:生产厂家|制造厂家|生产单位|生产企业|生产厂商|制造商|生产厂|厂家|供应单位|供货单位|供应商|供货商|供方|(?:MANUFACTURER|PRODUCER|MILL|SUPPLIER|VENDOR|SELLER)(?:NAME)?(?![A-Z]))[:：]?',re.I)
PRODUCER_LABEL = re.compile(r'^(?:生产厂家|制造厂家|生产单位|生产企业|生产厂商|制造商|生产厂|厂家|(?:MANUFACTURER|PRODUCER|MILL)(?:NAME)?(?![A-Z]))[:：]?',re.I)
SUPPLIER_LABEL = re.compile(r'^(?:供应单位|供货单位|供应商|供货商|供方|(?:SUPPLIER|VENDOR|SELLER)(?:NAME)?(?![A-Z]))[:：]?',re.I)
CUSTOMER_LABEL = re.compile(r'购货|收货|客户|需方|建设单位|监理单位|(?<![A-Z])(?:BUYER|CUSTOMER|PURCHASER|CONSIGNEE|SOLDTO|SHIPTO)(?:NAME)?(?![A-Z])',re.I)
COMPANY_LEGAL_SUFFIX = re.compile(r'(?:有限责任公司|股份有限公司|有限公司)$')
# Legal-company extraction deliberately excludes bare group/industry labels
# unless a reviewed delivery form explicitly identifies the printed heading as
# its supplier.  The 2025.12.6 forms use this exact association title.
GENERIC_COMPANY_HEADINGS = frozenset(('钢铁集团','特钢集团','行业集团','钢铁行业集团','钢筋行业集团'))
GENERIC_DELIVERY_HEADINGS = frozenset(('上海市钢筋混凝土结构用钢筋行业',))
DELIVERY_TITLE = re.compile(r'^(?:.*公司)?(?:送货单|发货单|送货清单|发货清单)$')
DELIVERY_NUMBER = re.compile(r'^N[OＯ0][.．:：#\-]*', re.I)
# Unicode word boundaries treat Chinese labels and Ф as letters. ASCII-only
# guards allow ``牌号HRB400EФ10`` and delivery cells ``16*12HRB500E`` without
# accepting a damaged prefix or a four-digit OCR grade.
GRADE = re.compile(r'(?<![A-Z])(?:HRBF?(?:335|400|500|600|700)E?|HPB(?:235|300))(?![A-Z0-9])', re.I)
DIAMETER = re.compile(r'(?:[ΦφФØ⌀]\s*(\d{1,2}(?:\.\d+)?)(?![\d.])|(?<!\d)(\d{1,2}(?:\.\d+)?)\s*mm)', re.I)
SIZE_LABEL = re.compile(r'^(?:钢筋规格|公称直径|直径|规格(?:型号)?|SIZE)[:：]?', re.I)
DATE = re.compile(r'(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?')


def compact(value):
    return re.sub(r'\s+', '', str(value or ''))


def _company_names(value, *, allow_generic=False):
    text = COMPANY_LABEL.sub('',compact(value),count=1)
    names = [name for name in COMPANY.findall(text) if name not in GENERIC_COMPANY_HEADINGS]
    if allow_generic and text in GENERIC_DELIVERY_HEADINGS:
        names.append(text)
    return names


def decimal_text(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return None
        return format(number, 'f').rstrip('0').rstrip('.') if '.' in format(number, 'f') else format(number, 'f')
    except InvalidOperation:
        return None


def date_text(value):
    match = DATE.fullmatch(compact(value))
    if not match:
        return None
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None


def bbox(block):
    polygon = block.get('polygon_ocr') or block.get('polygon_page')
    if not polygon:
        return None
    xs, ys = zip(*polygon)
    return min(xs), min(ys), max(xs), max(ys)


def center(block):
    box = bbox(block)
    return ((box[0]+box[2])/2, (box[1]+box[3])/2) if box else None


def page_blocks(page, candidate=None):
    # Use page/entity OCR rather than tiny field crops with different local axes.
    regions = [r for r in page.get('regions', []) if r['region_id'] == f"p{page['page_no']:03d}_full"]
    form_region = candidate.get('form_region_id') if candidate else None
    if form_region:
        scoped = [r for r in page.get('regions', []) if r['region_id'] == form_region]
        if scoped:
            return [dict(b,region_id=r['region_id']) for r in scoped for b in r.get('blocks',[])]
    entity = candidate.get('entity_key') if candidate else None
    if entity and entity != 'full' and entity != 'raw-labelled':
        scoped = [r for r in page.get('regions', []) if r.get('entity_key') == entity
                  and ('_field_' not in r['region_id'])]
        if scoped:
            regions = scoped
    if not regions and candidate and candidate.get('entity_key') not in (None, 'full', 'raw-labelled'):
        # Some older saved inventories encoded the entity only in the region ID.
        token = str(candidate['entity_key'])
        regions = [r for r in page.get('regions', []) if token in r['region_id'] and '_field_' not in r['region_id']]
    if not regions:
        # Classification replay can replace full-page OCR with split entities.
        # Those entities cover the original page once; adding full-form retries
        # would duplicate headings at slightly different coordinates.
        regions = [r for r in page.get('regions', [])
                   if re.fullmatch(r'p\d+_full(?:_entity\d+)+', r['region_id'])]
    if not regions:
        regions = [r for r in page.get('regions', []) if '_full' in r['region_id'] and not r.get('row_id')]
    if not regions:
        regions = page.get('regions', [])
    unique = {}
    for region in regions:
        for block in region.get('blocks', []):
            key = compact(block.get('text')), str(block.get('polygon_page'))
            unique.setdefault(key, dict(block, region_id=region['region_id']))
    return list(unique.values())


def evidence(page, block, method):
    return dict(page_no=page['page_no'], region_id=block.get('region_id'),
                polygon_page=block.get('polygon_page'), polygon_ocr=block.get('polygon_ocr'),
                source_image=page.get('source_image'), rotation_ccw=page.get('rotation_ccw', 0),
                raw_text=block.get('text'), score=block.get('score'), method=method)


def observation_evidence(candidate, observation):
    return dict(deepcopy(observation.get('value_evidence') or {}),
                page_no=candidate['page_no'], candidate_id=candidate['candidate_id'],
                row_id=candidate.get('row_id'), raw_text=observation.get('ocr_raw_value') or observation.get('raw_value'),
                method='selected_inventory_observation')


def field(candidates=(), warnings=()):
    """Keep every distinct OCR value and all its evidence, never pick a best guess."""
    unique = {}
    for item in candidates:
        if not item or not item.get('value'):
            continue
        value = str(item['value'])
        slot = unique.setdefault(value, dict(value=value, evidence=[]))
        for ev in item.get('evidence', []):
            if ev not in slot['evidence']:
                slot['evidence'].append(deepcopy(ev))
    values = list(unique.values())
    return dict(value=values[0]['value'] if len(values) == 1 else '', candidates=values,
                status='missing' if not values else ('ambiguous' if len(values) > 1 else 'observed'),
                warnings=list(warnings))


def candidate_value(page, block, value, method):
    return dict(value=value, evidence=[evidence(page, block, method)])


def diameter_text(value):
    value = decimal_text(value)
    return value if value and Decimal('5') <= Decimal(value) <= Decimal('50') else None


def specification_field(grades, sizes, warnings=()):
    return field([dict(value=f"{g['value']}/Ф{d['value']}", evidence=g['evidence']+d['evidence'])
                  for g in grades['candidates'] for d in sizes['candidates']], warnings)


def bind_source(fields, candidate, role):
    for value in fields.values():
        value['source_role'] = role
        for ev in value.get('review_locations', []):
            ev.update(source_role=role, candidate_id=candidate['candidate_id'], row_id=candidate.get('row_id'))
        for item in value.get('candidates', []):
            for ev in item.get('evidence', []):
                ev.update(source_role=role, candidate_id=candidate['candidate_id'], row_id=candidate.get('row_id'))
    return fields


def specification_parts(page, blocks, method, *, specification_column=False):
    """Read explicit diameter/grade text; plain sizes need a size label or cell.

    The first number in ``16*12HRB500E`` is diameter; its second number is
    length. Numbers beside length, weight and piece-count labels are excluded.
    """
    grades, sizes = [], []
    for block in blocks:
        text = compact(block['text'])
        grades.extend(candidate_value(page, block, match.group().upper(), method+'_grade')
                      for match in GRADE.finditer(text))
        diameter_matches = () if re.match(r'^(?:长度|重量|件重|单重|支数|件数|LENGTH|WEIGHT)[:：]?', text, re.I) else DIAMETER.finditer(text)
        for match in diameter_matches:
            value = diameter_text(match.group(1) or match.group(2))
            if value:
                sizes.append(candidate_value(page, block, value, method+'_diameter'))
        label = SIZE_LABEL.match(text)
        suffix = text[label.end():] if label else (text if specification_column else '')
        plain = re.match(r'^[ΦφФØ⌀]?(\d{1,2}(?:\.\d+)?)(?=$|mm|毫米|[*×xX+/]|盘螺|盘圆|螺纹钢|螺纹|圆钢|钢筋|HRBF?|HPB)', suffix, re.I)
        if plain and diameter_text(plain.group(1)):
            sizes.append(candidate_value(page, block, diameter_text(plain.group(1)), method+'_size_cell'))
        # A grade-first specification can also spell ``HRB400E/10``.
        if specification_column or label:
            for match in GRADE.finditer(text):
                number = re.match(r'^[/／:：][ΦφФØ⌀]?(\d{1,2}(?:\.\d+)?)(?=$|mm|毫米|[*×xX])', text[match.end():], re.I)
                if number and diameter_text(number.group(1)):
                    sizes.append(candidate_value(page, block, diameter_text(number.group(1)), method+'_grade_size_cell'))
        # ``规格`` and its plain value may occupy separate OCR boxes. Choose
        # the nearest value on that line, keeping both label and value evidence.
        if label and not suffix and bbox(block):
            right = [b for b in same_line(blocks, block) if b is not block and bbox(b)
                     and bbox(b)[0] >= bbox(block)[2]-8]
            if right:
                neighbour = right[0]
                raw = compact(neighbour['text'])
                number = re.match(r'^[ΦφФØ⌀]?(\d{1,2}(?:\.\d+)?)(?=$|mm|毫米|[*×xX+/])', raw, re.I)
                gap = bbox(neighbour)[0]-bbox(block)[2]
                if number and diameter_text(number.group(1)) and gap < max(160, (bbox(block)[3]-bbox(block)[1])*8):
                    sizes.append(dict(value=diameter_text(number.group(1)), evidence=[
                        evidence(page, block, method+'_size_label'), evidence(page, neighbour, method+'_labelled_size')]))
        # An unlabelled ``18`` followed immediately by ``mm`` remains explicit.
        if not specification_column and re.fullmatch(r'\d{1,2}(?:\.\d+)?', text) and bbox(block):
            right = [b for b in same_line(blocks, block) if b is not block and bbox(b)
                     and bbox(b)[0] >= bbox(block)[2]-8]
            if right and re.match(r'^mm(?:$|[*×xX/])', compact(right[0]['text']), re.I) and bbox(right[0])[0]-bbox(block)[2] < 80:
                value = diameter_text(text)
                if value:
                    sizes.append(dict(value=value, evidence=[evidence(page, block, method+'_split_mm'),
                        evidence(page, right[0], method+'_diameter_unit')]))
    return field(grades), field(sizes)


def delivery_grade_candidates(page, blocks, method, *, grade_column=False):
    """Retain OCR grade words only inside a delivery grade/specification cell.

    A labelled grade column can preserve its entire short OCR cell even when
    the alphabetic prefix is damaged.  A combined specification cell requires
    an explicit grade-like token, so its diameter/length/weight numbers are
    never borrowed as a grade.  No alphabetic OCR character is corrected here.
    """
    from .matching import grade_number
    candidates = []
    # A preceding length digit is valid in merged cells such as 16*12HBE500E.
    token_pattern = re.compile(r'(?<![A-Z])(?:[A-Z]{1,8}|[0-9]级[A-Z]{0,8})'
                               r'[0-9]{3,8}[A-Z]{0,3}(?![A-Z0-9])', re.I)
    for block in blocks:
        raw = compact(block.get('text'))
        if not raw or len(raw) > 40:
            continue
        if grade_column:
            # Keep contradictory/damaged numeric values as raw evidence too.
            # The join helper will reject missing/nonunique strength digits.
            if re.search(r'[0-9]{3}', raw) and not GRADE.fullmatch(raw):
                candidates.append(candidate_value(page, block, raw, method + '_raw_grade_column'))
            continue
        for match in token_pattern.finditer(raw):
            value = match.group()
            if grade_number(value) and not GRADE.fullmatch(value):
                candidates.append(candidate_value(page, block, value, method + '_raw_grade_token'))
    return candidates


def same_line(blocks, label):
    box = bbox(label)
    if not box:
        return [label]
    yc, height = (box[1]+box[3])/2, box[3]-box[1]
    return sorted((b for b in blocks if center(b) and abs(center(b)[1]-yc) <= max(15, height*.8)),
                  key=lambda b: bbox(b)[0])


def approval_context(page):
    blocks = page_blocks(page)
    fields = {key: field() for key in ('material_name', 'usage_location', 'supply_date', 'file_no')}
    anchors = [b for b in blocks if '进场' in compact(b.get('text')) and '工程' in compact(b.get('text'))]
    for anchor in anchors:
        line = same_line(blocks, anchor)
        text = ''.join(compact(b['text']) for b in line)
        usage = re.search(r'(?:拟用于工程|用于工程)(.*?)部位的', text)
        material = re.search(r'部位的([^,，。；;]+)', text)
        for key, match in [('usage_location', usage), ('material_name', material)]:
            if match and match.group(1):
                value = match.group(1).strip(':：')
                fields[key] = field([dict(value=value, evidence=[evidence(page, b, 'approval_arrival_clause')
                                                              for b in line if compact(b['text']) in value or value in compact(b['text'])])])
        dates = [candidate_value(page, b, date_text(m.group()), 'approval_arrival_date')
                 for b in line for m in DATE.finditer(compact(b['text'])) if date_text(m.group())]
        fields['supply_date'] = field(dates)
    # Older OCR may retain labelled clause values although the line is split.
    for key in ('material_name', 'usage_location'):
        if fields[key]['candidates']:
            continue
        values = []
        for region in page.get('regions', []):
            for item in region.get('extracted_fields', {}).get(key, []):
                if item.get('validation_status') == 'accepted':
                    values.append(dict(value=item.get('value'), evidence=[dict(item.get('evidence') or {},
                        page_no=page['page_no'], raw_text=item.get('raw_value'), method='approval_labelled_field')]))
        fields[key] = field(values)
    numbers = []
    for block in blocks:
        text = compact(block['text'])
        if not re.match(r'^编号[:：]?$', text) and not re.match(r'^编号[:：].+', text):
            continue
        inline = re.sub(r'^编号[:：]?', '', text)
        values = [block] if inline else [b for b in same_line(blocks, block)
            if bbox(b)[0] >= bbox(block)[2]-5 and bbox(b)[0]-bbox(block)[2] < 12*(bbox(block)[3]-bbox(block)[1])]
        for value_block in values[:1]:
            value = inline or compact(value_block['text'])
            if value:
                numbers.append(candidate_value(page, value_block, value, 'approval_file_number_label'))
    fields['file_no'] = field(numbers, ['文件编号如有手写或 OCR 残缺，请按报审原图补正。'] if numbers else [])
    return dict(context_id=f"approval:{page['page_no']}", page_no=page['page_no'], fields=fields)


def submission_context(form):
    """Adapt saved fixed-template observations to the existing ledger contract.

    Page polygons remain in their own 300DPI rendered-page coordinate system;
    they are not silently reused as 180DPI attachment coordinates.
    """
    mapping = dict(material_name='material_name', usage_location='use_location',
                   supply_date='supply_date', file_no='document_no')
    fields = {}
    for key, source_key in mapping.items():
        observed = form.get('fields', {}).get(source_key, {})
        value = form.get('values', {}).get(source_key)
        warnings = list(observed.get('issues', [])) + list(form.get('issues', []))
        ev = dict(page_no=form['page_no'], region_id=f"submission:{source_key}",
                  polygon_page=deepcopy(observed.get('polygon_page')),
                  raw_text=observed.get('raw_text'), score=observed.get('min_score'),
                  source_result=form.get('source_result'), crop_path=observed.get('crop_path'),
                  coordinate_system=form.get('coordinate_system', 'original_300dpi_page_pixels_top_left'),
                  render_dpi=form.get('render_dpi', 300), page_pixel_size=form.get('page_pixel_size'),
                  method='approval_fixed_template_ocr')
        fields[key] = field([dict(value=value, evidence=[ev])] if value else [], list(dict.fromkeys(warnings)))
    return dict(context_id=f"approval:{form['page_no']}", page_no=form['page_no'], fields=fields,
                extraction_method='fixed_template_ocr', source_record_id=form.get('record_id'),
                ready_for_search=False)


def heat_anchor(blocks, heat):
    pattern = re.compile(r'(?<![A-Z0-9])'+re.escape(compact(heat).upper())+r'(?![A-Z0-9])')
    matches = [b for b in blocks if pattern.search(compact(b['text']).upper()) and center(b)]
    return min(matches, key=lambda b: len(compact(b['text']))) if matches else None


def _excluded_company_owner(block, blocks, role):
    """Read ownership on the value itself or its neighbouring label box."""
    opposite = PRODUCER_LABEL if role == 'delivery' else SUPPLIER_LABEL
    def excluded(text):
        return CUSTOMER_LABEL.search(text) or opposite.match(text)
    if excluded(compact(block.get('text'))):
        return True
    return any(excluded(compact(other.get('text')))
               and bbox(other)[2] <= bbox(block)[0]+3
               and bbox(block)[0]-bbox(other)[2] <= max(160,(bbox(block)[3]-bbox(block)[1])*12)
               for other in same_line(blocks,block) if other is not block)


def _plate_heading_before_heat(blocks, target, *, after=float('-inf')):
    """Keep the first company inside one card, beyond legacy goods above it."""
    target_y = center(target)[1] if target else float('inf')
    headers = [b for b in blocks if _company_names(b.get('text')) and center(b)
               and after < center(b)[1] < target_y
               and not _excluded_company_owner(b,blocks,'nameplate')]
    if not headers:
        return None
    grades = [b for b in blocks if center(b) and center(b)[1] > after
              and GRADE.search(compact(b.get('text')))]
    grade = (min(grades, key=lambda b: abs(center(b)[1]-target_y)) if target
             else max(grades, key=lambda b: center(b)[1])) if grades else None
    limit = min(target_y,center(grade)[1]) if grade else target_y
    preceding = [b for b in headers if center(b)[1] < limit]
    older_grades = [center(b)[1] for b in grades if grade is not None
                    and center(b)[1] < center(grade)[1]
                    and any(center(b)[1] < center(h)[1] for h in preceding)]
    if older_grades:
        preceding = [b for b in preceding if center(b)[1] > max(older_grades)]
    return min(preceding or headers, key=lambda b: center(b)[1])


def plate_scope(page, candidate, peers):
    blocks = page_blocks(page, candidate)
    if str(candidate.get('entity_key', '')).startswith('card-'):
        # Older physical crops can begin in the delivery table above a pasted
        # card. Its customer company and goods grade must not become card data.
        heats = candidate.get('heat_lots', [])
        target = heat_anchor(blocks, heats[0]) if len(heats) == 1 else None
        if target:
            header = _plate_heading_before_heat(blocks,target)
            if header:
                blocks = [b for b in blocks if center(b) and center(b)[1] >= bbox(header)[1]-15]
        return blocks, []
    heats = candidate.get('heat_lots', [])
    if len(heats) != 1:
        return [], ['铭牌炉批号未唯一定位，请按原图补正字段。']
    anchors = [(p, heat_anchor(blocks, p['heat_lots'][0])) for p in peers if len(p.get('heat_lots', [])) == 1]
    target = heat_anchor(blocks, heats[0])
    unique_entity = candidate.get('entity_key') not in (None, 'full', 'raw-labelled') and all(
        p['candidate_id'] == candidate['candidate_id'] or p.get('entity_key') != candidate['entity_key'] for p in peers)
    if len(peers) <= 1 or unique_entity:
        global_blocks = page_blocks(page)
        # Layout splitting can put a plate's company header in the delivery crop.
        # Recall only a nearby header below the delivery title and above this heat.
        if target and not any(_company_names(b['text']) for b in blocks):
            ty = center(target)[1]
            titles_before = [center(b)[1] for b in global_blocks if DELIVERY_TITLE.fullmatch(compact(b['text'])) and center(b) and center(b)[1] < ty]
            low = max(titles_before) if titles_before else float('-inf')
            headers = [b for b in global_blocks if _company_names(b['text']) and center(b)
                       and low < center(b)[1] < ty and ty-center(b)[1] < 550]
            if headers:
                blocks = blocks + [min(headers, key=lambda b: ty-center(b)[1])]
        # A mixed sheet contains a plate above its delivery title.
        titles = [bbox(b)[1] for b in global_blocks if DELIVERY_TITLE.fullmatch(compact(b['text'])) and bbox(b)]
        if titles and target and center(target)[1] < min(titles):
            title_y = min(titles)
            supplier_headers = [bbox(b)[1] for b in blocks if _company_names(b['text'])
                                and center(b) and 0 < title_y-center(b)[1] < 240]
            boundary = max(supplier_headers) if supplier_headers else title_y
            return [b for b in blocks if center(b) and center(b)[1] < boundary-15], []
        if titles and target and center(target)[1] > max(titles):
            preceding_title = max(y for y in titles if y < center(target)[1])
            headers = [bbox(b)[1] for b in blocks if _company_names(b['text']) and center(b)
                       and preceding_title < center(b)[1] < center(target)[1]
                       and center(target)[1]-center(b)[1] < 550]
            if headers:
                return [b for b in blocks if center(b) and center(b)[1] >= max(headers)-15], []
            # A clipped producer heading may survive only below the heat.
            # Keep the neighbourhood of this card, never the delivery header.
            nearby_grades = [b for b in blocks if center(b) and GRADE.search(compact(b.get('text')))
                             and center(b)[1]>preceding_title]
            near = min(nearby_grades,key=lambda b:abs(center(b)[1]-center(target)[1])) if nearby_grades else target
            top = min(center(target)[1],center(near)[1])-max(80,(bbox(near)[3]-bbox(near)[1])*3)
            return [b for b in blocks if center(b) and center(b)[1]>=top], []
        return blocks, []
    if not target or any(a is None for _, a in anchors) or len(anchors) != len(peers):
        return [], ['同页多个铭牌的 OCR 范围未可靠区分，请核验原图后填写。']
    # Each card starts with a company header before its labelled heat.
    companies = [b for b in blocks if _company_names(b['text']) and center(b)]
    starts = []
    for peer, anchor in anchors:
        ax, ay = center(anchor)
        headers = [b for b in companies if center(b)[1] < ay and abs(center(b)[0]-ax) < max(450, bbox(b)[2]-bbox(b)[0])]
        if not headers:
            return [], ['同页多个铭牌缺少独立边界，请核验原图后填写。']
        header = min(headers, key=lambda b: (ay-center(b)[1])**2+(ax-center(b)[0])**2)
        starts.append((peer['candidate_id'], header))
    if len({str(bbox(b)) for _, b in starts}) != len(starts):
        return [], ['同页铭牌边界重叠，请核验原图后填写。']
    xs, ys = [center(b)[0] for _, b in starts], [center(b)[1] for _, b in starts]
    axis = 1 if max(ys)-min(ys) >= max(xs)-min(xs) else 0
    ordered = sorted(starts, key=lambda pair: center(pair[1])[axis])
    pos = next(i for i, pair in enumerate(ordered) if pair[0] == candidate['candidate_id'])
    if axis == 1:
        low = bbox(ordered[pos][1])[1]-15
        high = bbox(ordered[pos+1][1])[1]-15 if pos+1 < len(ordered) else float('inf')
    else:
        low = (center(ordered[pos-1][1])[0]+center(ordered[pos][1])[0])/2 if pos else float('-inf')
        high = (center(ordered[pos+1][1])[0]+center(ordered[pos][1])[0])/2 if pos+1 < len(ordered) else float('inf')
    return [b for b in blocks if center(b) and low <= center(b)[axis] < high], []


def source_locations(page, candidate, blocks, role):
    """Keep a useful source anchor even when OCR found no field value."""
    regions = [r for r in page.get('regions', []) if r.get('entity_key') == candidate.get('entity_key')
               and r.get('polygon_page')]
    if regions:
        return [dict(page_no=page['page_no'], region_id=r['region_id'],
                     polygon_page=deepcopy(r['polygon_page']), source_role=role,
                     method='missing_field_source_region') for r in regions[:1]]
    located = [b for b in blocks if center(b)]
    if located:
        top = min(center(b)[1] for b in located)
        located = [b for b in located if center(b)[1] <= top+70]
        return [evidence(page, b, 'missing_field_source_heading') for b in located[:3]]
    return [dict(page_no=page['page_no'], polygon_page=None, source_role=role,
                 source_image=page.get('source_image'), method='missing_field_source_page')]


def company_heading(page, blocks, method, *, role, title=None):
    """Read a company's own heading; never substitute customer/body companies."""
    located = [b for b in (delivery_nonplate_blocks(page, blocks) if role == 'delivery' else blocks) if center(b)]
    candidates = []
    for b in located:
        text = compact(b.get('text'))
        # Labels can occupy a separate OCR box from their company value. Such
        # customers and opposite-role body fields are neither headings nor
        # conflicting companies for this role.
        if _excluded_company_owner(b,located,role):
            continue
        # OCR may split a centred heading into a company prefix and suffix.
        # Only contiguous boxes on that same line can supply the prefix.
        prefix_blocks = [other for other in same_line(located,b) if other is not b
                         and bbox(other)[2] <= bbox(b)[0]+3
                         and 0 <= bbox(b)[0]-bbox(other)[2] <= max(25,(bbox(b)[3]-bbox(b)[1])*2)
                         and re.fullmatch(r'[\u4e00-\u9fff（）()·]{2,}',compact(other.get('text')))
                         and not re.search(r'单位|生产厂|公司|品名|材质|名称|牌号',compact(other.get('text')))
                         and not COMPANY_LABEL.match(compact(other.get('text')))
                         and not CUSTOMER_LABEL.search(compact(other.get('text')))]
        if prefix_blocks and re.match(r'^(?:供应链|钢铁|特钢|实业|贸易|建材|商贸|材料|集团|有限|股份)',text):
            prefix=max(prefix_blocks,key=lambda other:bbox(other)[2])
            joined=compact(prefix['text'])+text
            if COMPANY.fullmatch(joined) and _company_names(joined):
                b=deepcopy(b)
                b['text']=joined
                for coordinate in ('polygon_page','polygon_ocr'):
                    points=(prefix.get(coordinate) or [])+(b.get(coordinate) or [])
                    if points:
                        xs,ys=zip(*points);b[coordinate]=[[min(xs),min(ys)],[max(xs),min(ys)],[max(xs),max(ys)],[min(xs),max(ys)]]
                text=joined
        # Strip labels before the regex so they do not become part of a name.
        for company in _company_names(text, allow_generic=role == 'delivery'):
            candidates.append((b, company))
    all_candidates = candidates
    if title is not None:
        ty = center(title)[1]
        th = bbox(title)[3]-bbox(title)[1]
        candidates = [(b,c) for b,c in candidates if ty-max(240,th*7) <= center(b)[1] <= ty+th*.6]
    if not candidates:
        return [], ['送货单顶部供应公司未识别，请核对送货单原图。' if role == 'delivery'
                    else '独立铭牌顶部生产公司未识别，请核对该铭牌原图。'], []
    top = min(center(b)[1] for b,_ in candidates)
    # Same-line distinct companies are a genuine ambiguity; a lower company
    # in a footer is not the top heading specified by the ledger contract.
    first = [(b,c) for b,c in candidates if center(b)[1]-top <= max(15,(bbox(b)[3]-bbox(b)[1])*.8)]
    same_company = {}
    for block,company in sorted(first,key=lambda item:(center(item[0])[1],bbox(item[0])[0])):
        same_company.setdefault(COMPANY_LEGAL_SUFFIX.sub('',company),(block,company))
    first = list(same_company.values())
    warnings = []
    if role == 'delivery' and title is not None and len(first) == 1:
        company_box, title_box = bbox(first[0][0]), bbox(title)
        gap = max(title_box[0]-company_box[2], company_box[0]-title_box[2], 0)
        if gap > max((title_box[2]-title_box[0])*.75, (title_box[3]-title_box[1])*4):
            # A neighbouring card can have the highest company on this page.
            # Without a reliable physical boundary, keep locations for review
            # instead of treating that off-axis heading as a supplier value.
            return [], ['公司名称横向明显偏离送货单标题，可能属于同页铭牌；供应单位需核对送货抬头。'], [
                evidence(page,b,method+'_off_axis_review') for b,_ in candidates]
    if role == 'nameplate':
        labels = [b for b in located if GRADE.search(compact(b.get('text')))]
        if labels and top > min(center(b)[1] for b in labels):
            warnings.append('铭牌顶部公司未完整识别，仅识别到同卡生产厂公司，请核对铭牌顶部。')
    top_companies = {COMPANY_LEGAL_SUFFIX.sub('',company) for _,company in first}
    body_conflicts = [(b,c) for b,c in all_candidates if COMPANY_LEGAL_SUFFIX.sub('',c) not in top_companies
                      and center(b)[1]-top > max(15,(bbox(b)[3]-bbox(b)[1])*.8)]
    if body_conflicts:
        source = '送货单' if role == 'delivery' else '铭牌'
        destination = '供应单位' if role == 'delivery' else '生产单位'
        others = '、'.join(dict.fromkeys(company for _,company in body_conflicts))
        warnings.append(f'{source}顶部公司与正文公司（{others}）不一致，{destination}保留顶部公司，请核对原图。')
    locations = [evidence(page,b,method) for b,_ in first]
    locations.extend(evidence(page,b,method+'_body_company_conflict') for b,_ in body_conflicts)
    return [candidate_value(page,b,c,method) for b,c in first], warnings, locations


def _upright_evidence_center(page, ev):
    if ev.get('polygon_ocr'):
        return center(ev)
    polygon = ev.get('polygon_page')
    if not polygon:
        return None
    size = page.get('pixel_size')
    angle = int(page.get('rotation_ccw',0)) % 360
    if angle and not size:
        return None
    def turn(p):
        x,y = p
        if angle == 90:return y,size[0]-1-x
        if angle == 180:return size[0]-1-x,size[1]-1-y
        if angle == 270:return size[1]-1-y,x
        return x,y
    points = [turn(p) for p in polygon]
    return (sum(p[0] for p in points)/len(points),sum(p[1] for p in points)/len(points))


def delivery_nonplate_blocks(page, blocks):
    """Exclude known physical cards before reading a neighbouring dispatch form.

    Region polygons use original PDF-image axes; card IDs and OCR boxes use
    upright axes. Compare in upright axes without modifying saved evidence.
    """
    rectangles = []
    owned = set()
    global_blocks = page_blocks(page)
    for region in page.get('regions', []):
        entity = str(region.get('entity_key') or '')
        if not (entity.startswith('card-') or region.get('layout_role') == 'physical_nameplate'):
            continue
        card_blocks = region.get('blocks', [])
        polygon = region.get('polygon_page')
        points = [_upright_evidence_center(page, {'polygon_page':[p]}) for p in polygon] if polygon else []
        points = [p for p in points if p]
        if points:
            xs,ys = zip(*points)
            rectangle = (min(xs),min(ys),max(xs),max(ys))
        else:
            match = re.fullmatch(r'card-(\d+)-(\d+)-(\d+)-(\d+)',entity)
            rectangle = tuple(map(int,match.groups())) if match else None
        # Old card crops sometimes begin in the delivery table or even its
        # heading. Only remove the actual plate below its producer heading;
        # ownership of an oversized crop is not ownership of every OCR block.
        markers = [b for b in global_blocks+card_blocks
                   if (DELIVERY_TITLE.fullmatch(compact(b.get('text')))
                       or rectangle and center(b) and center(b)[1] < rectangle[1]
                       and DELIVERY_NUMBER.match(compact(b.get('text'))))
                   and center(b) and (not rectangle or
                       rectangle[0] <= center(b)[0] < rectangle[2]
                       and center(b)[1] < rectangle[3])]
        floor = None
        if markers:
            anchors = [heat_anchor(card_blocks,item.get('value'))
                       for item in region.get('extracted_fields',{}).get('heat_lot_no',[])
                       if item.get('value') and item.get('validation_status') in (None,'accepted')]
            anchors = [b for b in anchors if b] or [b for b in card_blocks if center(b)
                       and re.search(r'炉(?:批)?号|批号|(?:BA)?TCHNO',compact(b.get('text')),re.I)]
            target_y = min(center(b)[1] for b in anchors) if anchors else float('inf')
            preceding = [center(b)[1] for b in markers if center(b)[1] < target_y]
            low = max(preceding) if preceding else float('-inf')
            target = min(anchors,key=lambda b:center(b)[1]) if anchors else None
            header = _plate_heading_before_heat(card_blocks,target,after=low)
            if not header:
                # An unbounded mixed crop cannot safely suppress source text.
                continue
            floor = bbox(header)[1]-15
            if rectangle:
                rectangle = (rectangle[0],max(rectangle[1],floor),rectangle[2],rectangle[3])
        for block in card_blocks:
            if floor is None or center(block) and center(block)[1] >= floor:
                owned.add((compact(block.get('text')),str(block.get('polygon_page'))))
        if rectangle:
            rectangles.append(rectangle)
    def belongs(block):
        if (compact(block.get('text')),str(block.get('polygon_page'))) in owned:
            return True
        point = _upright_evidence_center(page,block)
        return bool(point and any(x0<=point[0]<x1 and y0<=point[1]<y1 for x0,y0,x1,y1 in rectangles))
    return [block for block in blocks if not belongs(block)]


def delivery_form_scopes(page, candidate=None):
    """Locate the selected physical delivery form, using its row coordinates.

    A row number alone is insufficient when two forms restart at row 1. On
    ambiguous legacy inventories all source forms survive for final review.
    """
    all_blocks = delivery_nonplate_blocks(page,
        page_blocks(page,candidate) if candidate and candidate.get('form_region_id') else page_blocks(page))
    titles = [b for b in all_blocks if DELIVERY_TITLE.fullmatch(compact(b.get('text'))) and center(b)]
    titles.sort(key=lambda b:(center(b)[1],center(b)[0]))
    scopes = []
    for index,title in enumerate(titles):
        tx,ty = center(title); th = bbox(title)[3]-bbox(title)[1]
        low = ty-max(240,th*7)
        below = [center(t)[1]-max(240,(bbox(t)[3]-bbox(t)[1])*7)
                 for t in titles if center(t)[1] > ty+th*2]
        high = min(below) if below else float('inf')
        same_level = [t for t in titles if t is not title and abs(center(t)[1]-ty) <= th*2]
        left = max(((center(t)[0]+tx)/2 for t in same_level if center(t)[0]<tx),default=float('-inf'))
        right = min(((center(t)[0]+tx)/2 for t in same_level if center(t)[0]>tx),default=float('inf'))
        local = [b for b in all_blocks if center(b) and left <= center(b)[0] < right and low <= center(b)[1] < high]
        scopes.append(dict(form_id=f"p{page['page_no']:03d}_delivery{index+1}",title=title,
                           blocks=local,bounds=(left,low,right,high)))
    if candidate and len(scopes)>1:
        points = [_upright_evidence_center(page,o.get('value_evidence') or {})
                  for o in candidate.get('field_observations',[]) if o.get('accepted')]
        points = [p for p in points if p]
        selected = [s for s in scopes if any(s['bounds'][0]<=x<s['bounds'][2] and
                    center(s['title'])[1]<=y<s['bounds'][3] for x,y in points)]
        if selected:
            return selected
        # Some scoped entity inventories omit row polygons; use their OCR
        # title only when that entity contains exactly one physical title.
        entity_titles = [b for b in page_blocks(page,candidate)
                         if DELIVERY_TITLE.fullmatch(compact(b.get('text'))) and center(b)]
        if len(entity_titles)==1:
            nearest = min(scopes,key=lambda s:sum((a-b)**2 for a,b in zip(center(s['title']),center(entity_titles[0]))))
            return [nearest]
    return scopes


def delivery_identity_fields(page, candidate):
    scopes = delivery_form_scopes(page,candidate)
    suppliers,numbers,supplier_locations,number_locations = [],[],[],[]
    supplier_warnings,number_warnings = [],[]
    for scope in scopes:
        blocks,title = scope['blocks'],scope['title']
        values,warnings,locations = company_heading(page,blocks,'delivery_company_heading',role='delivery',title=title)
        suppliers.extend(values);supplier_warnings.extend(warnings)
        supplier_locations.extend(locations or [evidence(page,title,'delivery_supplier_heading_missing')])
        tx,ty = center(title); th = bbox(title)[3]-bbox(title)[1]
        # No. must be a standalone label in the upper-right title area. Batch
        # No., telephone numbers and labels inside goods rows are excluded.
        labels = [b for b in blocks if center(b) and DELIVERY_NUMBER.match(compact(b.get('text')))
                  and center(b)[0]>=tx and ty-max(120,th*4)<=center(b)[1]<=ty+max(100,th*3)]
        for label in labels:
            number_locations.append(evidence(page,label,'delivery_number_label'))
            raw = DELIVERY_NUMBER.sub('',compact(label.get('text')),count=1)
            value_blocks = [(label,raw)] if raw else [(b,compact(b.get('text'))) for b in same_line(blocks,label)
                if b is not label and bbox(b)[0]>=bbox(label)[2]-8 and bbox(b)[0]-bbox(label)[2]<max(120,th*5)][:1]
            for b,value in value_blocks:
                if re.fullmatch(r'[A-Za-z0-9]+',value) and any(c.isdigit() for c in value):
                    item=candidate_value(page,b,value,'delivery_number_after_no')
                    item['evidence'].append(evidence(page,label,'delivery_number_label'))
                    numbers.append(item)
                else:
                    number_warnings.append('送货单右上No.编号模糊或含非字母数字内容，请核对原图，未自动纠正字符。')
        if not labels:
            number_locations.append(evidence(page,title,'delivery_number_upper_right_missing'))
    if not scopes:
        fallback=source_locations(page,candidate,page_blocks(page,candidate),'delivery')
        supplier_locations=number_locations=fallback
        supplier_warnings.append('送货/发货单标题未可靠定位，供应公司需核对送货页。')
    if len(scopes)>1:
        warning='同页多个送货单未唯一定位所选物理行，保留候选供最终核验。'
        supplier_warnings.append(warning);number_warnings.append(warning)
    if not numbers:
        number_warnings.append('送货单右上No.后的字母数字编号未识别，请核对送货页。')
    supplier=field(suppliers,list(dict.fromkeys(supplier_warnings)))
    number=field(numbers,list(dict.fromkeys(number_warnings)))
    for value,locations in ((supplier,supplier_locations),(number,number_locations)):
        value['review_locations']=locations
        value['source_form_ids']=[s['form_id'] for s in scopes]
        if len(scopes)>1:
            value.update(value='',status='ambiguous')
    return supplier,number


def plate_fields(page, candidate, peers):
    blocks, warnings = plate_scope(page, candidate, peers)
    grade_field, size_field = specification_parts(page, blocks, 'selected_plate')
    grades, sizes = grade_field['candidates'], size_field['candidates']
    # The top company inside this card is authoritative. A footer mentioning
    # another supplier/manufacturer cannot override that physical heading.
    manufacturers, company_warnings, company_locations = company_heading(
        page, blocks, 'selected_plate_company_heading', role='nameplate')
    # Only one physical plate permits inventory fallback. Multi-card page fields
    # can contain another card's diameter or its length in metres.
    if len(peers) == 1:
        for key, dest in [('grade', grades), ('diameter_mm', sizes)]:
            if dest:
                continue
            for obs in candidate.get('field_observations', []):
                if obs.get('field') == key and obs.get('accepted'):
                    value = obs.get('normalized_value')
                    if key == 'diameter_mm':
                        value = diameter_text(value)
                    elif not GRADE.fullmatch(compact(value)):
                        value = None
                    else:
                        value = compact(value).upper()
                    if value:
                        dest.append(dict(value=value, evidence=[observation_evidence(candidate, obs)]))
    grade_field, size_field = field(grades), field(sizes)
    specs = specification_field(grade_field, size_field, warnings)
    if not specs['candidates']:
        specs['warnings'].append('铭牌牌号或直径未完整提取，请核对关联送货单规格或原图。')
    heats = []
    for heat in candidate.get('heat_lots', []):
        anchor = heat_anchor(blocks, heat)
        evs = [evidence(page, anchor, 'selected_plate_heat')] if anchor else [observation_evidence(candidate, o)
            for o in candidate.get('field_observations', []) if o.get('field') == 'heat_lot_no']
        heats.append(dict(value=heat, evidence=evs))
    producer = field(manufacturers, warnings+company_warnings)
    producer['review_locations'] = company_locations or source_locations(page, candidate, blocks, 'nameplate')
    return bind_source(dict(specification=specs, grade=grade_field, diameter_mm=size_field,
                heat_lot_no=field(heats, warnings), manufacturer=producer), candidate, 'nameplate')


def delivery_table_rows(page):
    """Read physical specification/weight rows, excluding totals and signatures."""
    blocks = page_blocks(page)
    titles = [b for b in blocks if '送货单' in b['text'] and center(b)]
    if len(titles) != 1:
        return []
    title_y = center(titles[0])[1]
    headers = [b for b in blocks if center(b) and center(b)[1] > title_y
               and re.fullmatch(r'(?:规格(?:型号)?(?:[/／]材质)?|格?[/／]材质)', compact(b['text']))]
    if len(headers) > 1:
        return []
    if headers:
        header = headers[0]
    else:
        # Left-edge clipped scans retain the weight and neighbouring headings.
        weights = [b for b in blocks if center(b) and re.search(r'^重量[（(](?:吨|t)[）)]$', compact(b['text']), re.I)]
        if len(weights) != 1:
            return []
        line = same_line(blocks, weights[0])
        if sum(any(k in b['text'] for k in ['件重','件数','产地','签收']) for b in line) < 2:
            return []
        header = min(line,key=lambda b:center(b)[0])
    columns = same_line(blocks, header)
    weight_headers = [b for b in columns if re.search(r'^重量[（(](?:吨|t)[）)]$', compact(b['text']), re.I)]
    if len(weight_headers) != 1:
        return []
    weight_header = weight_headers[0]
    wx, hy = center(weight_header)
    positions = sorted({center(b)[0] for b in columns if center(b) and re.search(
        r'件重|签收件数|签收数量|重量|规格|件数|产地|备注', b['text'])})
    left = max((x for x in positions if x < wx), default=wx-180)
    right = min((x for x in positions if x > wx), default=wx+180)
    xmin, xmax = (left+wx)/2, (wx+right)/2
    sx = center(header)[0]
    spec_left = max((center(b)[0] for b in columns if center(b) and center(b)[0] < sx), default=sx-220)
    spec_right = min((center(b)[0] for b in columns if center(b) and center(b)[0] > sx), default=sx+220)
    sxmin, sxmax = (spec_left+sx)/2, (sx+spec_right)/2
    footers = [center(b)[1] for b in blocks if center(b) and center(b)[1] > hy and
               re.match(r'合计|送货地址|联系电话|数量[（(]大写|金额',compact(b['text']))]
    bottom = min(footers) if footers else hy+350
    spec_cells = [b for b in blocks if center(b) and hy+12 < center(b)[1] < bottom
                  and sxmin <= center(b)[0] < sxmax]
    specs = []
    for cell in spec_cells:
        grade, diameter = specification_parts(page, [cell], 'delivery_specification_column', specification_column=True)
        if grade['candidates'] or diameter['candidates'] or re.search(
                r'(?:HRBF|HRB|HPB|RB|B)[0-9O]{3}E?', compact(cell['text']), re.I):
            specs.append(cell)
    unique_specs = []
    for spec in sorted(specs, key=lambda b:center(b)[1]):
        if not unique_specs or abs(center(spec)[1]-center(unique_specs[-1])[1]) > max(12,(bbox(spec)[3]-bbox(spec)[1])*.65):
            unique_specs.append(spec)
    # A damaged specification cell must not remove a physical row and shift
    # every later row ID. Positive numbers in the goods-weight column provide
    # an independent row anchor; totals are outside the footer boundary.
    weight_blocks = [b for b in blocks if center(b) and hy+12 < center(b)[1] < bottom
                     and xmin <= center(b)[0] < xmax
                     and re.fullmatch(r'\d{1,6}(?:[.,]\d{1,6})?', compact(b['text']))
                     and Decimal(compact(b['text']).replace(',', '.')) > 0]
    # Clipped scans may omit the "合计" label. A weight-only line cannot
    # establish a goods row: require a specification or an adjacent numeric
    # piece-count/piece-weight cell on that line.
    weight_blocks = [b for b in weight_blocks if any(
        s in same_line(blocks, b) for s in unique_specs) or any(
            center(other) and sx+80 < center(other)[0] < xmin
            and re.fullmatch(r'\d{1,6}(?:[.,]\d{1,6})?', compact(other['text']))
            and Decimal(compact(other['text']).replace(',', '.')) > 0
            for other in same_line(blocks, b))]
    anchors = sorted(unique_specs+weight_blocks, key=lambda b: center(b)[1])
    lines = []
    for anchor in anchors:
        if not lines or abs(center(anchor)[1]-center(lines[-1][0])[1]) > max(
                15, (bbox(anchor)[3]-bbox(anchor)[1])*.65):
            lines.append([anchor])
        else:
            lines[-1].append(anchor)
    rows = []
    for line in lines:
        anchor = line[0]
        cells = [b for b in spec_cells if abs(center(b)[1]-center(anchor)[1]) <= max(
            15, (bbox(anchor)[3]-bbox(anchor)[1])*.65, (bbox(b)[3]-bbox(b)[1])*.65)]
        cells.sort(key=lambda b: bbox(b)[0])
        raw_specification = ''.join(b['text'] for b in cells)
        grade, diameter = specification_parts(page, cells, 'delivery_specification_column', specification_column=True)
        spec_field = specification_field(grade, diameter)
        for value in (grade, diameter, spec_field):
            for item in value['candidates']:
                for ev in item['evidence']:
                    ev.update(label=header['text'], label_polygon_page=header.get('polygon_page'),
                              physical_row_index=len(rows)+1)
        values = [b for b in line if b in weight_blocks]
        candidates = []
        for value_block in values:
            raw = compact(value_block['text'])
            value = decimal_text(raw.replace(',', '.'))
            if value and Decimal(value) > 0:
                ev = evidence(page,value_block,'delivery_goods_weight_column')
                ev.update(label=weight_header['text'],label_polygon_page=weight_header.get('polygon_page'),
                          specification_raw_text=raw_specification or None,
                          specification_polygon_page=cells[0].get('polygon_page') if cells else None,
                          column_unit='t', physical_row_index=len(rows)+1)
                candidates.append(dict(value=value,evidence=[ev]))
        rows.append(dict(physical_row_index=len(rows)+1, specification=raw_specification,
                         specification_field=spec_field, grade=grade, diameter_mm=diameter,
                         quantity=field(candidates)))
    return rows


def delivery_fields(page, candidate):
    blocks = page_blocks(page, candidate)
    supplier,delivery_no = delivery_identity_fields(page,candidate)
    quantities = []
    grade, diameter = field(), field()
    # Physical-resource candidates already carry independently extracted row
    # observations. Legacy page row ordinals can refer to another form/total.
    table_rows = [] if candidate.get('form_region_id') else delivery_table_rows(page)
    row_number = re.search(r'row(\d+)$', candidate.get('row_id') or '')
    selected_rows = [r for r in table_rows if r['physical_row_index'] == int(row_number.group(1))] if row_number else (
        table_rows if len(table_rows) == 1 else [])
    if len(selected_rows) == 1:
        quantities = selected_rows[0]['quantity']['candidates']
        grade, diameter = selected_rows[0]['grade'], selected_rows[0]['diameter_mm']
    observations = candidate.get('field_observations', []) if candidate.get('row_id') else []
    weight_obs = [o for o in observations if o.get('field') in ('quantity_t', 'weight_t') and o.get('accepted')]
    # Prefer the goods weight column over a separate received-quantity column.
    goods_weight = [o for o in weight_obs if '重量' in str((o.get('value_evidence') or {}).get('label', ''))]
    for obs in ([] if quantities else goods_weight or weight_obs):
        value = decimal_text(obs.get('value_t') or obs.get('normalized_value'))
        if value and Decimal(value) > 0:
            quantities.append(dict(value=value, evidence=[observation_evidence(candidate, obs)]))
    # Inventory observations are row-scoped. Never borrow a page-wide grade or
    # diameter when the selected delivery has several physical goods rows.
    for key, target in [('grade', grade), ('diameter_mm', diameter)]:
        if target['candidates']:
            continue
        values, unverified = [], []
        for obs in observations:
            if obs.get('field') != key or not obs.get('accepted'):
                continue
            raw = compact(obs.get('normalized_value'))
            # A grade observation is already bound to this physical goods
            # row and field. Preserve its original OCR wording; matching may
            # compare its strength digits without requiring a correct prefix.
            if key == 'diameter_mm':
                value = diameter_text(raw)
            else:
                # A damaged prefix is usable only when its grade column has a
                # single recognized strength class. Preserve the observation
                # itself on the physical row, but keep conflicting OCR text
                # out of the scalar field so the ledger shows review needed.
                from .matching import grade_number
                value = raw if (GRADE.fullmatch(raw) or grade_number(raw)) else None
            if value:
                values.append(dict(value=value, evidence=[observation_evidence(candidate, obs)]))
            elif key == 'grade' and raw:
                unverified.append(dict(value=raw, evidence=[observation_evidence(candidate, obs)]))
        target.update(field(values))
        if unverified:
            target.update(value='', status='ambiguous', alternatives=unverified,
                review_locations=[deepcopy(ev) for item in unverified for ev in item['evidence']])
            target['warnings'].append('送货行牌号数字未唯一识别或存在冲突；保留原OCR文字待最终核验。')
    specs = specification_field(grade, diameter)
    if not candidate.get('row_id') and len(selected_rows) != 1:
        specs['warnings'].append('送货单包含多个货物行且未定位所选行，不能借用其他行规格。')
    return bind_source(dict(supplier=supplier, delivery_no=delivery_no, grade=grade, diameter_mm=diameter,
        specification=specs, quantity_t=field(quantities,
        ['送货行未定位，请按所选铭牌对应规格核对送货原图后填写重量（吨）。'] if not candidate.get('row_id') and not quantities else [])), candidate, 'delivery')


CERT_ID = re.compile(r'(?<![A-Z0-9])(?:[A-Z]{1,4}[A-Z0-9._/-]{5,}|\d{7,})(?![A-Z0-9])', re.I)
EXPLICIT_CERT_ID = re.compile(r'(?<![A-Z0-9])(?=[A-Z0-9._/-]*\d)[A-Z0-9][A-Z0-9._/-]{2,}(?![A-Z0-9])', re.I)


def certificate_fields(candidate, page=None):
    numbers = []
    raw_values = [(value, None, 'selected_certificate_inventory') for value in candidate.get('certificate_numbers', [])]
    if page is not None:
        # Compare label neighbours inside each OCR region. Cross-region
        # deduplication otherwise keeps only one of several conflicting reads
        # at the same label/value coordinates.
        for region in page.get('regions', []):
            blocks = [dict(b, region_id=region['region_id']) for b in region.get('blocks', [])]
            for block in blocks:
                raw_values.extend(certificate_label_values(page, block, blocks))
    dedup = {}
    for value, block, method in raw_values:
        if not value:
            continue
        evs = []
        if block is not None:
            evs = [evidence(page, block, method)]
        else:
            evs = [observation_evidence(candidate, o) for o in candidate.get('field_observations', [])
                   if o.get('field') == 'certificate_no' and compact(o.get('normalized_value') or o.get('raw_value')) == compact(value)]
        dedup.setdefault(value, []).extend(evs)
    for value, evs in dedup.items():
        if not evs:
            evs = [observation_evidence(candidate, o) for o in candidate.get('field_observations', [])
                   if o.get('field') == 'certificate_no']
        numbers.append(dict(value=value, evidence=evs))
    return dict(certificate_no=field(numbers, ['保留质证书编号原 OCR 值；缺字、多点等情况需核对原图补正。']))


def certificate_label_values(page, block, blocks):
    values, result = [], []
    text = compact(block.get('text'))
    # Keep the exact OCR token beside an explicit certificate label.
    label = re.search(r'(?:质量证明书编号|质保书编号|证书编号|质编号|证明书号|货编号|(?<![A-Z])NO)\s*[:：.]?', text, re.I)
    if label:
        # Explicit Chinese labels also carry identifiers such as
        # ``20251022GX610``. Keep the entire OCR token, including letters in
        # the middle, without widening the generic No. fallback.
        identifier = CERT_ID if label.group().upper().startswith('NO') else EXPLICIT_CERT_ID
        labelled_values = lambda value: [v for v in identifier.findall(value)
                                         if not re.fullmatch(r'\d+\.\d+', v)]
        values.extend(labelled_values(text[label.end():]))
        if not values and bbox(block):
            box = bbox(block); yc=(box[1]+box[3])/2; h=max(1,box[3]-box[1])
            neighbours = []
            for other in blocks:
                obox=bbox(other)
                if other is block or not obox or obox[0] < box[2]-h*.25:
                    continue
                if abs((obox[1]+obox[3])/2-yc) <= h*.9 and obox[0]-box[2] < h*14:
                    neighbours.append((obox[0]-box[2], other))
            if neighbours:
                value_block=min(neighbours,key=lambda x:x[0])[1]
                for value in labelled_values(compact(value_block.get('text'))):
                    result.append((value,value_block,'certificate_label_neighbour'))
    # Some scans merge ``No`` with the value (for example ``NOK...``).
    if re.match(r'^NO', text, re.I):
        values.extend(CERT_ID.findall(re.sub(r'^NO[:：.]?', '', text, flags=re.I)))
    result.extend((value,block,'certificate_label_inline') for value in dict.fromkeys(values))
    return result


def build_filling_profile(result, index, classification, *, inspection_evidence=None,
                          submission_forms=(), submission_duplicates=()):
    """Build ledger evidence while retaining both approval and inspection sources.

    ``submission_forms`` is optional for legacy V6 saved records. Inspection
    evidence is independently optional because older records point to their
    original cell result through ``source_result`` and ``source_cells``.
    """
    from .ledger_sources import inspection_fields
    pages = {p['page_no']: p for p in index['pages']}
    saved = {}
    for form in submission_forms:
        if form.get('pdf_sha256') != index['pdf_sha256']:
            raise ValueError('报审表与附件OCR不是同一PDF')
        if form.get('page_no') not in pages or form['page_no'] in saved:
            raise ValueError('报审表来源页不存在或重复')
        saved[form['page_no']] = form
    context_pages = sorted(set(saved) | {p['page_no'] for p in classification['pages']
                                        if 'approval_form' in p['classification']})
    contexts = [submission_context(saved[n]) if n in saved else approval_context(pages[n]) for n in context_pages]
    for context in contexts:
        groups = [deepcopy(g) for g in submission_duplicates if context['page_no'] in g.get('page_nos', [])]
        if groups:
            context['duplicate_candidates'] = groups
            context['fields']['file_no']['warnings'].append('重复报审候选的编号可能不同；请按所选页原图核对，未自动合并或替换。')
    resources = {role: {} for role in ('nameplate', 'delivery', 'certificate', 'inspection')}
    for entry in result.get('entries', []):
        resources['inspection'][entry['record_id']] = inspection_fields(
            entry['record'], entry['record_id'], (inspection_evidence or {}).get(entry['record_id']))
    for candidate in result.get('nameplate_inventory', []):
        page = pages[candidate['page_no']]
        peers = [c for c in result['nameplate_inventory'] if c['page_no'] == candidate['page_no']]
        resources['nameplate'][candidate['candidate_id']] = plate_fields(page, candidate, peers)
    for candidate in result.get('delivery_inventory', []):
        resources['delivery'][candidate['candidate_id']] = delivery_fields(pages[candidate['page_no']], candidate)
    for candidate in result.get('certificate_page_inventory', []):
        resources['certificate'][candidate['candidate_id']] = certificate_fields(candidate, pages[candidate['page_no']])
    return dict(schema_version='rebar-ledger-evidence-v2', material_id='rebar', columns=deepcopy(COLUMNS),
                approval_contexts=contexts, resources=resources,
                submission_duplicate_candidates=deepcopy(list(submission_duplicates)),
                template_reference='报审表字文字内容提取项目说明.docx / word/media/image2.png',
                quantity_unit='t', row_identity='one_selected_physical_nameplate')
