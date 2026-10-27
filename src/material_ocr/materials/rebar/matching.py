"""Constrain quantity candidates using separately identified same-page heat lots.

Consumes saved OCR evidence. No fuzzy ID correction or original-field replacement.
All returned chains are candidates for review, including conflicting recovery paths.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
import re
import unicodedata


def norm(value):
    return re.sub(r"\s+", "", str(value or "")).upper()


def heat_valid(value):
    # Do not turn OCR strings containing labels or trailing fractions into join keys.
    # Preserve observed vendor formats such as C26040080, CS-6060351,
    # C3-6060372 and CN-60-0245 without correcting their prefixes.
    value = norm(value)
    if re.match(r'^(?:HRB|HRBF|HPB|RRB|CRB)', value):
        return False
    return bool(re.fullmatch(r"(?:\d{6,12}|\d{4,}[A-Z]\d{3,}[A-Z0-9]*(?:-\d+)?|[A-Z]{1,4}[-/]?\d{5,}[A-Z0-9]*|[A-Z]\d[-/]\d{5,}[A-Z0-9]*|[A-Z]{1,4}[-/]\d{2}[-/]\d{4,}[A-Z0-9]*)", value))


def same_number(a, b):
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except InvalidOperation:
        return False


# Delivery OCR can prepend a spurious digit to the strength class, e.g.
# ``HRB400E`` -> ``1级B3400E``. Read each grade number from right to left, but
# retain its literal text and reject conflicting classes. This helper is only
# a delivery comparison key; entry/nameplate and ID joins remain strict.
_GRADE_NUMBER_RE = re.compile(r'(?<![0-9])(235|300|335|400|500|600|700)(?![0-9])')
_GRADE_CLASSES = frozenset(('235', '300', '335', '400', '500', '600', '700'))


def grade_numeric_details(value):
    """Describe a unique strength class and any ignored leading OCR digits.

    A long digit run must follow alphabetic grade text; a bare ``3400`` or a
    weight cannot supply a repaired grade. Only its rightmost three digits
    may be used, and another recognized strength anywhere in the same run
    makes it conflicting. Thus ``HRB3400E`` or ``1级B3400E`` yields 400,
    while a bare ``B3400E`` has no accepted grade prefix and remains
    unresolved; ``B400500E`` and separate 400/500 observations also remain
    unresolved. Original OCR is never rewritten by this comparison operation.
    """
    text = unicodedata.normalize('NFKC', str(value or '')).upper()
    numbers, ignored = set(_GRADE_NUMBER_RE.findall(text)), []
    for match in re.finditer(r'[0-9]{4,}', text):
        digits = match.group()
        prefix = re.search(r'(?:[0-9]级)?[A-Z]{1,8}$', text[:match.start()])
        # Extra digit removal needs a grade-like prefix, never an invoice or
        # heat identifier that happens to end in 400/500. Short OCR variants
        # observed in these handwritten cells retain B (HBE, IRB, 1级B).
        valid_prefix = bool(prefix and re.fullmatch(
            r'(?:HRBF|HRB|HPB|RRB|CRB|HBE|IRB)', prefix.group()))
        valid_prefix = valid_prefix or bool(re.search(r'[0-9]级B$', text[:match.start()]))
        if len(digits) > 8 or not valid_prefix:
            continue
        strength = digits[-3:]
        if strength not in _GRADE_CLASSES:
            continue
        observed = {digits[i:i+3] for i in range(len(digits)-2)} & _GRADE_CLASSES
        if observed != {strength}:
            return dict(numeric_key=None, ignored_leading_digits=[])
        numbers.add(strength)
        ignored.append(dict(raw_digits=digits, ignored_prefix=digits[:-3], strength_digits=strength))
    key = next(iter(numbers)) if len(numbers) == 1 else None
    return dict(numeric_key=key, ignored_leading_digits=ignored if key else [])


def grade_numeric_key(value):
    """Return the unique recognized numeric grade class in *value*.

    ``HRB500E``, ``HBE500E`` and ``1级B500E`` all yield ``"500"``.  The
    leading level number in the last OCR form is ignored. ``1级B3500E`` also
    yields ``"500"`` by reading the numeric grade from its right end. Missing
    or conflicting classes return ``None`` and cannot establish a match.
    """
    return grade_numeric_details(value)['numeric_key']


# Descriptive alias for callers that make the route decision themselves.
grade_number = grade_numeric_key


def observations(items, role, field, allowed_pages):
    found = {}
    for item in items:
        if item.get('role') != role or item.get('field') != field:
            continue
        value = norm(item.get('normalized_value')).rstrip('.。')
        if item['page_no'] not in allowed_pages or not item.get('accepted') or not heat_valid(value):
            continue
        key = (item['page_no'], value)
        obj = found.setdefault(key, {'page_no': key[0], 'value': key[1], 'evidence': []})
        # Save the value-specific coordinates, not a merged entity containing two plates.
        ev = {k: deepcopy(item.get(k)) for k in (
            'region_id', 'entity_key', 'row_id', 'raw_value', 'ocr_raw_value',
            'normalized_value', 'status', 'accepted', 'value_evidence', 'value_evidence_refs')}
        if ev not in obj['evidence']:
            obj['evidence'].append(ev)
    return list(found.values())


def delivery_rows(catalog, page):
    """Keep field observations on their own entity/row; never select a quantity here."""
    rows = {}
    for x in catalog:
        if x.get('role') != 'delivery_note' or x['page_no'] != page or not x.get('accepted'):
            continue
        if x.get('row_id') is None or x.get('field') not in ('quantity_t', 'diameter_mm', 'grade'):
            continue
        key = (x.get('entity_key'), x['row_id'])
        row = rows.setdefault(key, {'page_no': page, 'entity_key': key[0], 'row_id': key[1],
                                    'quantities_t': [], 'diameters_mm': [], 'grades': [], 'evidence': [],
                                    'quantity_scores': []})
        field = {'quantity_t': 'quantities_t', 'diameter_mm': 'diameters_mm', 'grade': 'grades'}[x['field']]
        value = x.get('value_t') if field == 'quantities_t' else x.get('normalized_value')
        if value is not None and str(value) not in row[field]:
            row[field].append(str(value))
        row['evidence'].append(deepcopy(x))
        if field == 'quantities_t':
            polygon = (x.get('value_evidence') or {}).get('polygon_page')
            for block in (x.get('evidence') or {}).get('blocks', []):
                if polygon and block.get('polygon_page') == polygon and block.get('score') is not None:
                    row['quantity_scores'].append(float(block['score']))
    return list(rows.values())


def constrain_shared_heat(record, plates, catalog, *, low_confidence_threshold=.90):
    """Compare physical delivery rows; never pool a page's weight and spec.

    Exact, unambiguous quantity first. Missing, competing or low-confidence
    quantity permits a spec fallback. A clear contradictory weight stays a
    contradiction even if the diameter agrees.
    """
    options = []
    for plate in plates:
        for row in delivery_rows(catalog, plate['nameplate_page']):
            values = {Decimal(v) for v in row['quantities_t']}
            uncertain = len(values) != 1 or (row['quantity_scores'] and max(row['quantity_scores']) < low_confidence_threshold)
            exact = record.get('weight_t') is not None and len(values) == 1 and any(same_number(record['weight_t'], v) for v in values)
            specs = row['diameters_mm']
            spec_match = record.get('diameter_mm') is not None and bool(specs) and all(same_number(record['diameter_mm'], v) for v in specs)
            grade = record.get('grade')
            if grade and row['grades']:
                spec_match = spec_match and all(norm(grade) == norm(v) for v in row['grades'])
            options.append(dict(plate=deepcopy(plate), delivery=row, quantity_exact=exact,
                                quantity_uncertain=bool(uncertain), specification_matches=bool(spec_match),
                                selection='retained_for_review'))
    exact = [o for o in options if o['quantity_exact'] and not o['quantity_uncertain']]
    mode = None
    selected = exact
    if exact:
        mode = 'entry_quantity'
        if len({(o['delivery']['page_no'], o['delivery']['entity_key'], o['delivery']['row_id']) for o in exact}) > 1:
            by_spec = [o for o in exact if o['specification_matches']]
            if by_spec:
                selected, mode = by_spec, 'entry_quantity_and_specification'
    elif record.get('diameter_mm') is not None:
        selected = [o for o in options if o['quantity_uncertain'] and o['specification_matches']]
        if selected:
            mode = 'specification_with_uncertain_quantity'
    identities = {(o['delivery']['page_no'], o['delivery']['entity_key'], o['delivery']['row_id'],
                   o['plate']['plate_key'], o['plate']['certificate_page']) for o in selected}
    unique = len(identities) == 1
    for o in options:
        o.update(selection='selected_candidate' if unique and o in selected else 'retained_for_review',
                 constraint=mode, write_allowed=False)
        if unique and o not in selected:
            o.update(selection='excluded_by_entry_fields', reason='quantity_or_same_row_specification_not_selected')
    return dict(unique=unique, mode=mode, options=options, selected=selected if unique else [])


def number_suffix(value):
    m = re.search(r'(\d{6,})$', norm(value))
    return m.group(1) if m else None


def review_differences(record, certificate, row):
    conflicts = []
    if record.get('certificate_no') and norm(record['certificate_no']) != norm(certificate['value']):
        conflicts.append({'type': 'original_certificate_number_mismatch', 'original': record['certificate_no'],
                          'retrieved': [certificate['value']], 'certificate_page': certificate['page_no']})
    quantities = row['quantities_t']
    if len({Decimal(v) for v in quantities}) > 1:
        conflicts.append({'type': 'multiple_delivery_quantity_values', 'page_no': row['page_no'],
                          'row_id': row['row_id'], 'entity_key': row['entity_key'],
                          'observed_quantities_t': quantities})
    if record.get('weight_t') is not None:
        for value in quantities:
            if not same_number(record['weight_t'], value):
                conflicts.append({'type': 'delivery_quantity_mismatch', 'page_no': row['page_no'],
                                  'row_id': row['row_id'], 'entity_key': row['entity_key'],
                                  'original_quantity_t': record['weight_t'], 'observed_quantity_t': value,
                                  'difference_t': str(Decimal(value) - Decimal(str(record['weight_t'])))})
    specs = row['diameters_mm']
    if record.get('diameter_mm') is not None and specs:
        if not all(same_number(record['diameter_mm'], v) for v in specs):
            conflicts.append({'type': 'delivery_row_diameter_mismatch', 'page_no': row['page_no'],
                              'row_id': row['row_id'], 'entity_key': row['entity_key'], 'original_diameter_mm': record['diameter_mm'],
                              'delivery_diameters_mm': specs})
    return conflicts


def build_manual_options(entry, inventory, plates, cert_heats, catalog):
    """Search every certificate without a strict ID link first, preserving old paths."""
    original_suffix = number_suffix(entry['record'].get('certificate_no'))
    unused = [c for c in inventory if not c['exact_record_ids']]
    prior_pages = {c['page_no'] for c in entry['recovered_certificate_candidates']}
    used_recovery = [c for c in inventory if c['exact_record_ids'] and c['page_no'] in prior_pages]
    def similar(c):
        return bool(original_suffix and original_suffix == number_suffix(c['value']))
    candidates = sorted(unused, key=lambda c: (not similar(c), c['page_no'], c['value'])) + used_recovery
    options = []
    for c in candidates:
        linked = [(heat, plate) for heat in cert_heats if heat['page_no'] == c['page_no']
                  for plate in plates if plate['value'] == heat['value']]
        for heat, plate in linked or [(None, None)]:
            rows = delivery_rows(catalog, plate['page_no']) if plate else []
            if not rows:
                rows = [{'page_no': plate['page_no'] if plate else None, 'row_id': None, 'entity_key': None,
                         'quantities_t': [], 'diameters_mm': [], 'grades': [], 'evidence': []}]
            for row in rows:
                differences = review_differences(entry['record'], c, row)
                reasons = ['same_numeric_suffix_different_prefix'] if similar(c) else []
                reasons.append('no_strict_identifier_association' if not c['exact_record_ids'] else 'prior_quantity_recovery')
                options.append({'priority': 1 if not c['exact_record_ids'] and similar(c) else (2 if not c['exact_record_ids'] else 3),
                                'recall_reasons': reasons, 'status': 'manual_candidate', 'write_allowed': False,
                                'certificate_page': c['page_no'], 'certificate_numbers': [c['value']],
                                'certificate_identifiers': [deepcopy(c)],
                                'heat_lot_no': heat['value'] if heat else None,
                                'nameplate_page': plate['page_no'] if plate else None,
                                'plate_key': f"p{plate['page_no']:03d}:{plate['value']}" if plate else None,
                                'delivery_page': row['page_no'], 'delivery_row_id': row['row_id'],
                                'delivery': row, 'conflicts': differences,
                                'row_selection_verified': False,
                                'evidence': {'certificate_heat': heat, 'nameplate': plate},
                                'exact_record_ids': c['exact_record_ids']})
    return options


def manual_plate_observations(classifications, catalog):
    """Recall explicit raw labels only for manual inventory, never join keys."""
    extra = []
    label = re.compile(r'(?:炉批号|炉号|轧制批号|轧制号|批号)\s*[:：]?\s*([A-Z0-9][A-Z0-9/-]{5,})', re.I)
    for page in classifications.get('pages', []):
        if 'nameplate' not in page.get('classification', []):
            continue
        observed = {norm(x.get('normalized_value')).rstrip('.。') for x in catalog
                    if x.get('role') == 'nameplate' and x.get('field') == 'heat_lot_no' and x['page_no'] == page['page_no']}
        for match in label.finditer(page.get('raw_text', '')):
            value = norm(match.group(1))
            if value in observed or not heat_valid(value):
                continue
            blocks = [deepcopy(b) for b in page.get('ocr_blocks', []) if value in norm(b.get('text', ''))]
            extra.append(dict(role='nameplate',field='heat_lot_no',page_no=page['page_no'],
                entity_key='raw-labelled',region_id=f'p{page["page_no"]:03d}_raw_label',
                row_id=None,normalized_value=value,raw_value=match.group(1),ocr_raw_value=match.group(0),
                accepted=False,status='manual_raw_observation',
                value_evidence=dict(label=match.group(0), blocks=blocks, source='classified_page_raw_text')))
            observed.add(value)
    return extra


def manual_inventory(role, pages, catalog, identifiers, entries):
    """Include every classified page, with independent heat/row resources.

    Full-page OCR duplicates a cropped plate only when that crop observes the
    same heat. Distinct heats sharing a page must remain independently usable.
    """
    result = []
    for page in sorted(pages):
        fields = [x for x in catalog if x.get('role') == role and x['page_no'] == page]
        if role == 'nameplate':
            cropped_heats = {norm(x.get('normalized_value')).rstrip('.。') for x in fields
                             if x.get('field') == 'heat_lot_no' and x.get('entity_key') not in (None, 'full')}
            groups = {(x.get('entity_key'), norm(x.get('normalized_value') or x.get('raw_value')).rstrip('.。'))
                      for x in fields if x.get('field') == 'heat_lot_no' and
                      not (x.get('entity_key') in (None, 'full') and
                           norm(x.get('normalized_value')).rstrip('.。') in cropped_heats)}
            heat_entities = {entity for entity, heat in groups}
            groups.update((x.get('entity_key'), None) for x in fields
                          if x.get('entity_key') not in heat_entities and x.get('entity_key') not in (None, 'full'))
            if not groups:
                groups = {(x.get('entity_key'), None) for x in fields}
        elif role == 'delivery_note':
            groups = {(x.get('entity_key'), x.get('row_id')) for x in fields}
        else:
            groups = {(None, None)}
        for entity, resource in sorted(groups or {(None, None)}, key=str):
            observed = [x for x in fields if role == 'quality_certificate' or (
                x.get('entity_key') == entity and (role != 'delivery_note' or x.get('row_id') == resource))]
            if role == 'nameplate' and resource:
                observed = [x for x in observed if x.get('field') != 'heat_lot_no' or
                            norm(x.get('normalized_value') or x.get('raw_value')).rstrip('.。') == resource]
            def values(field):
                return sorted({str(x.get('value_t') if field == 'quantity_t' and x.get('value_t') is not None
                                   else x.get('normalized_value') or x.get('raw_value'))
                               for x in observed if x.get('field') == field and
                               (x.get('normalized_value') or x.get('raw_value'))})
            numbers = sorted({c['value'] for c in identifiers if c['page_no'] == page})
            inventory_field = {'quality_certificate': 'direct_certificate_candidates',
                               'nameplate': 'nameplate_candidates', 'delivery_note': 'delivery_note_candidates'}[role]
            associations = sorted({e['record_id'] for e in entries if any(
                (c.get('nameplate_page') if role == 'nameplate' else c.get('page_no')) == page
                and (role != 'nameplate' or not resource or norm(c.get('heat_lot_no')) == resource)
                and (role != 'delivery_note' or (c.get('entity_key'), c.get('row_id')) == (entity, resource))
                for c in e[inventory_field])})
            cid = f'{role}:{page}:{entity}' + (f':{resource or "page-only"}' if role != 'quality_certificate' else '')
            result.append(dict(candidate_id=cid, page_no=page, entity_key=entity,
                row_id=resource if role == 'delivery_note' else None,
                certificate_numbers=numbers if role == 'quality_certificate' else [],
                heat_lots=values('heat_lot_no'), quantities_t=values('quantity_t'),
                diameters_mm=values('diameter_mm'), grades=values('grade'),
                associated_record_ids=associations, status='manual_candidate', write_allowed=False,
                field_observations=[{k: deepcopy(x.get(k)) for k in ('field','raw_value','ocr_raw_value',
                    'normalized_value','value_t','accepted','status','region_id','entity_key','row_id','value_evidence')}
                    for x in observed]))
    if role == 'nameplate':
        # Match keys are page + heat; repeated crop observations are evidence.
        merged = {}
        for c in result:
            heat = norm(c['heat_lots'][0]).rstrip('.。') if c['heat_lots'] else None
            physical = str(c.get('entity_key', '')).startswith('card-')
            key = (c['page_no'], c['entity_key']) if physical else ((c['page_no'], heat) if heat else (c['page_no'], c['entity_key']))
            if key not in merged:
                merged[key] = c
                c['candidate_id'] = f'nameplate:{c["page_no"]}:{c["entity_key"] if physical else heat or c["entity_key"] or "page-only"}'
                c['entity_keys'] = [c['entity_key']]
            else:
                target = merged[key]
                target['entity_keys'].append(c['entity_key'])
                target['field_observations'].extend(c['field_observations'])
                for field in ('heat_lots', 'diameters_mm', 'grades', 'associated_record_ids'):
                    target[field] = sorted(set(target[field] + c[field]))
        result = list(merged.values())
    return result


def rank_manual_candidates(entry, inventory, role, catalog):
    record = entry['record']
    original = norm(record.get('certificate_no'))
    compact = lambda value: re.sub(r'[^A-Z0-9]', '', norm(value))
    expected = set(entry['expected_certificate_heat_lots'])
    ranked = []
    for source in inventory:
        candidate = deepcopy(source)
        reasons, score = [], 0
        if role == 'quality_certificate':
            for value in candidate['certificate_numbers']:
                observed = norm(value)
                if observed == original and original:
                    reasons.append('原编号严格一致'); score = max(score, 100)
                elif original and compact(observed) == compact(original):
                    reasons.append('编号标点差异，需人工核验'); score = max(score, 90)
                elif number_suffix(compact(original)) and number_suffix(compact(original)) == number_suffix(compact(observed)):
                    reasons.append('数字后缀相同、字母前缀不同或缺失，需人工核验'); score = max(score, 85)
                elif original and SequenceMatcher(None, compact(original), compact(observed)).ratio() >= .8:
                    reasons.append('编号近似，需逐字核验'); score = max(score, 60)
            if not candidate['certificate_numbers']:
                reasons.append('未提取出质证编号，请查看原图')
        if expected & {norm(v).rstrip('.。') for v in candidate['heat_lots']}:
            reasons.append('与已检索质证单炉批号一致'); score += 30
        if record.get('diameter_mm') and any(same_number(record['diameter_mm'], v) for v in candidate['diameters_mm']):
            reasons.append('规格一致'); score += 10
        if role == 'delivery_note' and any(same_number(record.get('weight_t'), v) for v in candidate['quantities_t']):
            reasons.append('进场数量一致'); score += 35
        if role == 'nameplate':
            candidate['same_page_delivery_rows'] = delivery_rows(catalog, candidate['page_no'])
            if any(any(same_number(record.get('weight_t'), q) for q in row['quantities_t'])
                   for row in candidate['same_page_delivery_rows']):
                reasons.append('同页送货行进场数量一致'); score += 15
            if not candidate['heat_lots']:
                reasons.append('未提取出炉批号，请查看原图')
        if not candidate['associated_record_ids']:
            reasons.append('尚未建立严格关联'); score += 5
        candidate.update(rank_score=score, recall_reasons=list(dict.fromkeys(reasons)), automatically_confirmed=False)
        ranked.append(candidate)
    return sorted(ranked, key=lambda c: (-c['rank_score'], c['page_no'], str(c['entity_key'])))


def rematch(baseline, catalog, classifications, certificate_ids, *, low_confidence_threshold=.90):
    roles = classifications['metrics']['category_pages']
    plate_pages = set(roles['nameplate'])
    cert_pages = set(roles['quality_certificate'])
    plates = observations(catalog, 'nameplate', 'heat_lot_no', plate_pages)
    cert_heats = observations(catalog, 'quality_certificate', 'heat_lot_no', cert_pages)
    cert_ids = list({(c['page_no'], norm(c['value'])): deepcopy(c)
                     for c in certificate_ids if c['page_no'] in cert_pages}.values())
    entries = []
    for old in baseline['entries']:
        r = deepcopy(old['record'])
        direct = deepcopy(old['quality_certificate_candidates'])
        direct_pages = {c['page_no'] for c in direct}
        expected = {c['value'] for c in cert_heats if c['page_no'] in direct_pages}
        quantity = deepcopy(old['delivery_note_candidates'])
        repeated = len({(d['page_no'], d.get('row_id')) for d in quantity}) > 1
        accepted, rejected, paths, conflicts = [], [], [], []
        for d in quantity:
            on_page = [p for p in plates if p['page_no'] == d['page_no']]
            shared = sorted(expected & {p['value'] for p in on_page})
            d.update(same_page_heat_lots=sorted({p['value'] for p in on_page}),
                     matching_certificate_heat_lots=shared)
            if repeated and expected and on_page and not shared:
                d.update(selection='rejected', reason='same_page_heat_mismatch',
                         expected_heat_lots=sorted(expected), constraint_evidence=on_page)
                rejected.append(d)
                continue
            d.update(selection='candidate', reason='quantity_and_same_page_heat' if shared else 'quantity_only')
            row_specs = [x for x in catalog if x.get('accepted') and x.get('role') == 'delivery_note'
                         and x.get('field') == 'diameter_mm' and x['page_no'] == d['page_no']
                         and d.get('row_id') is not None and x.get('row_id') == d['row_id']
                         and x.get('entity_key') == d.get('entity_key')]
            if row_specs and r.get('diameter_mm'):
                specs = sorted({str(x['normalized_value']) for x in row_specs})
                if not any(same_number(r['diameter_mm'], v) for v in specs):
                    conflicts.append({'type': 'delivery_row_diameter_mismatch', 'page_no': d['page_no'],
                                      'row_id': d['row_id'], 'entity_key': d.get('entity_key'), 'original_diameter_mm': r['diameter_mm'],
                                      'delivery_diameters_mm': specs, 'evidence': row_specs})
            if expected and on_page and not shared:
                conflicts.append({'type': 'same_page_heat_mismatch', 'page_no': d['page_no'],
                                  'expected': sorted(expected), 'observed': d['same_page_heat_lots']})
            accepted.append(d)
            # An exact quantity hit does not resolve competing OCR observations
            # on that same physical row. Preserve those differences for review.
            for row in delivery_rows(catalog, d['page_no']):
                if row['row_id'] == d.get('row_id') and row['entity_key'] == d.get('entity_key'):
                    cert = direct[0] if direct else {'value': r.get('certificate_no'), 'page_no': None}
                    for difference in review_differences(r, cert, row):
                        if difference not in conflicts:
                            conflicts.append(difference)
            eligible = [p for p in on_page if not direct or p['value'] in expected]
            for plate in eligible:
                linked = [h for h in cert_heats if h['value'] == plate['value']
                          and (not direct or h['page_no'] in direct_pages)]
                for heat in linked or [None]:
                    ids = [c for c in cert_ids if heat and c['page_no'] == heat['page_no']]
                    path = {'delivery_page': d['page_no'], 'delivery_row_id': d.get('row_id'),
                            'quantity_t': d.get('value_t'), 'nameplate_page': plate['page_no'],
                            'plate_key': f"p{plate['page_no']:03d}:{plate['value']}",
                            'heat_lot_no': plate['value'], 'certificate_page': heat['page_no'] if heat else None,
                            'certificate_numbers': sorted({c['value'] for c in ids}),
                            'certificate_identifiers': deepcopy(ids),
                            'origin': 'direct_certificate_same_page' if direct else 'delivery_same_page_recovery',
                            'evidence': {'delivery': deepcopy(d), 'nameplate': plate, 'certificate_heat': heat}}
                    paths.append(path)
        recovered = [p for p in paths if p['origin'] == 'delivery_same_page_recovery']
        for path in recovered:
            if r.get('certificate_no') and path['certificate_numbers'] and norm(r['certificate_no']) not in path['certificate_numbers']:
                conflict = {'type': 'original_certificate_number_mismatch',
                            'original': r['certificate_no'], 'retrieved': path['certificate_numbers'],
                            'certificate_page': path['certificate_page'], 'heat_lot_no': path['heat_lot_no']}
                if conflict not in conflicts:
                    conflicts.append(conflict)
        ambiguities = []
        # Preserve certificate→plate evidence even when a delivery quantity is absent.
        chosen_plates = []
        for plate in plates:
            connections = [h for h in cert_heats if h['page_no'] in direct_pages and h['value'] == plate['value']]
            for h in connections:
                chosen_plates.append({'nameplate_page': plate['page_no'], 'certificate_page': h['page_no'],
                                      'heat_lot_no': plate['value'], 'plate_key': f"p{plate['page_no']:03d}:{plate['value']}",
                                      'origin': 'direct_certificate_heat', 'evidence': {'nameplate': plate, 'certificate': h}})
        for path in recovered:
            candidate = dict(nameplate_page=path['nameplate_page'], certificate_page=path['certificate_page'],
                             heat_lot_no=path['heat_lot_no'], plate_key=path['plate_key'],
                             origin='delivery_same_page_recovery', evidence=deepcopy(path['evidence']))
            if candidate not in chosen_plates:
                chosen_plates.append(candidate)
        all_heat_plates = deepcopy(chosen_plates)
        field_constraint = {'unique': False, 'mode': None, 'options': [], 'selected': []}
        if len({p['plate_key'] for p in chosen_plates}) > 1 or (chosen_plates and len({
                (d['page_no'], d.get('entity_key'), d.get('row_id')) for d in accepted}) > 1):
            field_constraint = constrain_shared_heat(r, chosen_plates, catalog, low_confidence_threshold=low_confidence_threshold)
            if field_constraint['unique']:
                option = field_constraint['selected'][0]
                chosen_plates = [deepcopy(option['plate'])]
                row = option['delivery']
                kept, removed = [], []
                for d in accepted:
                    if (d['page_no'], d.get('entity_key'), d.get('row_id')) == (row['page_no'], row['entity_key'], row['row_id']):
                        kept.append(d)
                    else:
                        d.update(selection='rejected', reason='entry_quantity_or_specification_constraint')
                        removed.append(d)
                accepted = kept
                rejected.extend(removed)
                paths = [p for p in paths if (p['delivery_page'], p.get('delivery_row_id'), p['plate_key']) ==
                         (row['page_no'], row['row_id'], option['plate']['plate_key'])]
                conflicts = [c for c in conflicts if c.get('page_no') is None or (
                    c['page_no'] == row['page_no'] and (c.get('row_id') is None or
                    (c['row_id'], c.get('entity_key')) == (row['row_id'], row['entity_key'])))]
        # A reused heat identifies several physical plates. Quantity recall can
        # retain a path, but cannot silently confirm one plate or delivery page.
        if len({p['plate_key'] for p in chosen_plates}) > 1:
            ambiguities.append('multiple_physical_nameplates_for_certificate_heat')
        supplemented = []
        quantity_review = []
        if field_constraint['unique']:
            option = field_constraint['selected'][0]
            row, plate = option['delivery'], option['plate']
            verified = option['quantity_exact'] and not option['quantity_uncertain']
            d = dict(page_no=row['page_no'], row_id=row['row_id'], entity_key=row['entity_key'],
                     value_t=row['quantities_t'][0] if verified else None, selection='candidate',
                     reason=field_constraint['mode'], origin='entry_fields_same_heat_constraint',
                     quantity_ocr_verified=verified, quantity_matches_record=True if verified else None,
                     observed_delivery_rows=[deepcopy(row)], same_page_heat_lots=[plate['heat_lot_no']],
                     matching_certificate_heat_lots=[plate['heat_lot_no']], evidence=deepcopy(option))
            accepted = [d]
            if not verified:
                quantity_review.append('specification_selected_delivery_quantity_not_verified')
            selected_certs = [c for c in (direct or cert_ids) if c['page_no'] == plate['certificate_page']]
            cert = selected_certs[0] if selected_certs else dict(value=r.get('certificate_no'), page_no=None)
            for difference in review_differences(r, cert, row):
                if difference not in conflicts:
                    conflicts.append(difference)
            paths = []
            paths.append(dict(delivery_page=row['page_no'], delivery_row_id=row['row_id'], quantity_t=d['value_t'],
                              nameplate_page=plate['nameplate_page'], plate_key=plate['plate_key'],
                              heat_lot_no=plate['heat_lot_no'], certificate_page=plate['certificate_page'],
                              certificate_numbers=[c['value'] for c in selected_certs], certificate_identifiers=deepcopy(selected_certs),
                              origin='entry_fields_same_heat_constraint' if direct else 'delivery_same_page_recovery', constraint=field_constraint['mode'],
                              quantity_ocr_verified=verified, evidence=deepcopy(option)))
        if not accepted and chosen_plates:
            independent_keys = {p['plate_key'] for p in chosen_plates}
            if (len(independent_keys) == 1 and len(direct_pages) == 1
                    and chosen_plates[0]['nameplate_page'] in roles['delivery_note']):
                plate = chosen_plates[0]
                page = plate['nameplate_page']
                d = {'page_no': page, 'row_id': None, 'entity_key': None, 'value_t': None,
                     'selection': 'candidate', 'reason': 'unique_certificate_heat_same_page_delivery',
                     'origin': 'nameplate_same_page_supplement', 'relation_scope': 'page',
                     'quantity_ocr_verified': False, 'quantity_matches_record': None,
                     'same_page_heat_lots': [plate['heat_lot_no']],
                     'matching_certificate_heat_lots': [plate['heat_lot_no']],
                     'observed_delivery_rows': delivery_rows(catalog, page),
                     'evidence': {'nameplate_certificate_chain': deepcopy(plate),
                                  'delivery_classification': {'page_no': page, 'role': 'delivery_note'}}}
                accepted.append(d)
                supplemented.append(deepcopy(d))
                quantity_review.append('supplemented_delivery_quantity_not_ocr_verified')
                paths.append({'delivery_page': page, 'delivery_row_id': None, 'quantity_t': None,
                              'nameplate_page': page, 'plate_key': plate['plate_key'],
                              'heat_lot_no': plate['heat_lot_no'], 'certificate_page': plate['certificate_page'],
                              'certificate_numbers': [c['value'] for c in direct],
                              'certificate_identifiers': deepcopy(direct),
                              'origin': 'nameplate_same_page_supplement', 'quantity_ocr_verified': False,
                              'evidence': deepcopy(d['evidence'])})
            elif len(independent_keys) > 1:
                ambiguities.append('multiple_certificate_plate_paths_for_delivery_supplement')
        if len({(p['delivery_page'], p['delivery_row_id'], p['plate_key'], p['certificate_page']) for p in paths}) > 1:
            ambiguities.append('multiple_delivery_plate_certificate_paths')
        if len({(d['page_no'], d.get('row_id')) for d in accepted}) > 1:
            ambiguities.append('multiple_delivery_rows')
        recovered = [p for p in paths if p['origin'] == 'delivery_same_page_recovery']
        recovered_ids = []
        for path in recovered:
            plate = {'nameplate_page': path['nameplate_page'], 'certificate_page': path['certificate_page'],
                     'heat_lot_no': path['heat_lot_no'], 'plate_key': path['plate_key'],
                     'origin': 'delivery_same_page_recovery', 'evidence': path['evidence']}
            if plate not in chosen_plates:
                chosen_plates.append(plate)
            for c in path['certificate_identifiers']:
                c = dict(c, origin='delivery_same_page_recovery', heat_lot_no=path['heat_lot_no'],
                         method='same_page_nameplate_heat_retrieval', status='recovered_candidate')
                if c not in recovered_ids:
                    recovered_ids.append(c)
        supplemented_plates = []
        plate_review = []
        delivery_pages = {d['page_no'] for d in accepted}
        if not chosen_plates and len(delivery_pages) == 1:
            page = next(iter(delivery_pages))
            # Same-page membership supplements the role, never a missing or
            # contradictory heat identifier. Physical class must exist.
            if page in set(roles['nameplate']):
                plate = dict(nameplate_page=page, certificate_page=None, heat_lot_no=None,
                             value='炉批号未OCR核验',
                             plate_key=f'p{page:03d}:page-only', origin='delivery_same_page_nameplate_supplement',
                             relation_scope='page', heat_ocr_verified=False, write_allowed=False,
                             observed_heat_lots=[p['value'] for p in plates if p['page_no'] == page],
                             evidence={'delivery': deepcopy(accepted), 'nameplate_classification': {'page_no': page}})
                chosen_plates.append(plate)
                supplemented_plates.append(deepcopy(plate))
                plate_review.append('supplemented_nameplate_heat_not_verified')
        missing = []
        if not accepted: missing.append('delivery_quantity_unresolved')
        if not direct and not recovered_ids: missing.append('certificate_unresolved')
        if not chosen_plates: missing.append('nameplate_unresolved')
        status = 'conflict_requires_review' if conflicts else ('ambiguous' if ambiguities else ('unresolved' if missing else 'candidate_complete'))
        for path in recovered:
            path['status'] = status if conflicts or ambiguities else 'recovered_candidate'
        entries.append({'record_id': old['record_id'], 'record': r, 'status': status, 'write_allowed': False,
                        'direct_certificate_candidates': direct, 'recovered_certificate_candidates': recovered_ids,
                        'quality_certificate_candidates': direct + recovered_ids,
                        'delivery_note_candidates': accepted, 'rejected_delivery_note_candidates': rejected,
                        'supplemented_delivery_note_candidates': supplemented,
                        'quantity_review_reasons': quantity_review,
                        'nameplate_review_reasons': plate_review,
                        'supplemented_nameplate_candidates': supplemented_plates,
                        'shared_heat_constraint': field_constraint,
                        'original_same_heat_nameplate_candidates': all_heat_plates,
                        'nameplate_candidates': chosen_plates, 'paths': paths, 'recovered_paths': recovered,
                        'conflicts': conflicts, 'ambiguous_reasons': ambiguities, 'unresolved_reasons': missing,
                        'expected_certificate_heat_lots': sorted(expected),
                        'original_candidates': {k: deepcopy(old[k]) for k in ('quality_certificate_candidates', 'delivery_note_candidates', 'nameplate_candidates')},
                        'existing_pipeline_cross_checks': deepcopy(old.get('existing_pipeline_cross_checks', []))})
    inventory = []
    for c in cert_ids:
        item = deepcopy(c)
        item.update(exact_record_ids=sorted({e['record_id'] for e in entries
                    if any(d['page_no'] == c['page_no'] and norm(d['value']) == norm(c['value'])
                           for d in e['direct_certificate_candidates'])}),
                    recovery_record_ids=sorted({e['record_id'] for e in entries
                    if any(d['page_no'] == c['page_no'] and norm(d['value']) == norm(c['value'])
                           for d in e['recovered_certificate_candidates'])}),
                    manual_candidate_record_ids=[], human_confirmed_record_ids=[],
                    human_confirmation_status='not_imported')
        inventory.append(item)
    certificate_pages = manual_inventory('quality_certificate', cert_pages, catalog, cert_ids, entries)
    manual_plate_catalog = catalog + manual_plate_observations(classifications, catalog)
    nameplate_inventory = manual_inventory('nameplate', plate_pages, manual_plate_catalog, [], entries)
    delivery_inventory = manual_inventory('delivery_note', set(roles['delivery_note']), catalog, [], entries)
    queue = []
    for e in entries:
        e['manual_certificate_candidates'] = rank_manual_candidates(e, certificate_pages, 'quality_certificate', catalog)
        e['manual_nameplate_candidates'] = rank_manual_candidates(e, nameplate_inventory, 'nameplate', catalog)
        e['manual_delivery_candidates'] = rank_manual_candidates(e, delivery_inventory, 'delivery_note', catalog)
        e['manual_review_options'] = []
        if not e['direct_certificate_candidates']:
            e['manual_review_options'] = build_manual_options(e, inventory, plates, cert_heats, catalog)
            e['certificate_search'] = {'status': 'strict_identifier_not_found',
                                       'searched_unassociated_identifiers': [
                                           {'page_no': c['page_no'], 'value': c['value']} for c in inventory if not c['exact_record_ids']],
                                       'automatically_corrected': False}
            e['review_reasons'] = ['strict_certificate_identifier_not_found']
            # Missing/incorrect original IDs remain an explicit manual task, even with a recovery chain.
            if e['record'].get('certificate_no'):
                e['status'] = 'conflict_requires_review'
            for c in inventory:
                if any(o['certificate_page'] == c['page_no'] and c['value'] in o['certificate_numbers']
                       for o in e['manual_review_options']):
                    c['manual_candidate_record_ids'].append(e['record_id'])
        else:
            e['review_reasons'] = []
        e['review_reasons'] += [c['type'] for c in e['conflicts']] + e['ambiguous_reasons'] + e['unresolved_reasons'] + e['quantity_review_reasons'] + e['nameplate_review_reasons']
        if e['review_reasons']:
            queue.append({'record_id': e['record_id'], 'status': 'pending_human_review',
                          'reasons': e['review_reasons'], 'options': deepcopy(e['manual_review_options']),
                          'manual_certificate_candidates': deepcopy(e['manual_certificate_candidates']),
                          'manual_nameplate_candidates': deepcopy(e['manual_nameplate_candidates']),
                          'write_allowed': False})
    metrics = {'record_count': len(entries), 'date_matching_included': False, 'new_ocr_executed': False,
               'formal_write_allowed': 0, 'association_package_generated': False,
               'same_page_disambiguated_records': sum(bool(e['rejected_delivery_note_candidates']) and bool(e['delivery_note_candidates']) for e in entries),
               'recovered_records': sum(bool(e['recovered_paths']) for e in entries),
               'conflict_records': sum(bool(e['conflicts']) for e in entries),
               'unresolved_records': sum(bool(e['unresolved_reasons']) for e in entries),
               'ambiguous_records': sum(bool(e['ambiguous_reasons']) for e in entries),
               'complete_candidate_records': sum(e['status'] == 'candidate_complete' for e in entries),
               'direct_certificate_records': sum(bool(e['direct_certificate_candidates']) for e in entries),
               'retrieved_certificate_records': sum(bool(e['quality_certificate_candidates']) for e in entries),
               'delivery_candidate_records': sum(bool(e['delivery_note_candidates']) for e in entries),
               'nameplate_candidate_records': sum(bool(e['nameplate_candidates']) for e in entries)}
    metrics.update(supplemented_delivery_records=sum(bool(e['supplemented_delivery_note_candidates']) for e in entries),
                   quantity_constrained_shared_heat_records=sum(e['shared_heat_constraint']['unique'] and e['shared_heat_constraint']['mode'] in ('entry_quantity', 'entry_quantity_and_specification') for e in entries),
                   specification_constrained_shared_heat_records=sum(e['shared_heat_constraint']['unique'] and e['shared_heat_constraint']['mode'] == 'specification_with_uncertain_quantity' for e in entries),
                   supplemented_nameplate_records=sum(bool(e['supplemented_nameplate_candidates']) for e in entries),
                   quantity_unverified_supplement_records=sum(bool(e['quantity_review_reasons']) for e in entries),
                   unmatched_certificate_identifier_count=sum(not c['exact_record_ids'] for c in inventory),
                   manual_certificate_review_records=sum(bool(e['manual_review_options']) for e in entries),
                   review_task_count=len(queue))
    result = {'schema_version': 'same-page-constrained-match-v5', 'pdf_sha256': baseline['pdf_sha256'],
            'excluded_relations': ['inspection_date'], 'entries': entries, 'metrics': metrics,
            'certificate_inventory': inventory, 'certificate_page_inventory': certificate_pages,
            'nameplate_inventory': nameplate_inventory, 'delivery_inventory': delivery_inventory, 'review_queue': queue,
            'same_page_nameplate_heat_observations': plates, 'certificate_heat_observations': cert_heats}

    from material_ocr.review.associations import prepare_associations
    return prepare_associations(result)
