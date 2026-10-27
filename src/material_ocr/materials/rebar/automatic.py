"""Automatic physical-form association; human review only before Excel export."""
from collections import defaultdict
from copy import deepcopy
from .matching import norm, same_number, heat_valid, grade_number, grade_numeric_details
from .filling import field, plate_fields, delivery_fields, certificate_fields, bind_source
from .ledger_sources import inspection_fields, resolve_specification
from .delivery_resources import build_delivery_resources
from .entry_duplicates import entry_duplicate_diagnostics, deduplicate_certificate_records
from .heat_review import single_heat_difference

LABELS = dict(material_name='材料名称', specification='规格品种', manufacturer='生产单位',
              supplier='供应单位', delivery_no='送货单编号', supply_date='供货日期',
              usage_location='使用部位', heat_lot_no='炉批号', quantity_t='验收数量',
              certificate_no='质保书编号', file_no='文件编号')


def evs(info):
    found = []
    for candidate in info.get('candidates', []) + info.get('alternatives', []):
        for ev in candidate.get('evidence', []):
            if ev not in found:
                found.append(deepcopy(ev))
    for ev in info.get('review_locations', []):
        if ev not in found:
            found.append(deepcopy(ev))
    return found


def diagnostic(code, field_name, reason, locations=(), **extra):
    return dict(code=code, field=field_name, reason=reason,
                locations=deepcopy(list(locations)), **extra)


def source_locations(candidate, role):
    if not candidate:
        return []
    locations = []
    for item in candidate.get('field_observations', []):
        ev = deepcopy(item.get('value_evidence') or {})
        if ev.get('polygon_page'):
            ev.update(page_no=candidate['page_no'], source_role=role,
                      candidate_id=candidate.get('candidate_id'), form_id=candidate.get('form_id'),
                      row_id=candidate.get('row_id'))
            if ev not in locations:
                locations.append(ev)
    return locations or [dict(page_no=candidate['page_no'], source_role=role,
                 candidate_id=candidate.get('candidate_id'), form_id=candidate.get('form_id'),
                 row_id=candidate.get('row_id'), polygon_page=None)]


def certificate_number(value):
    value = norm(value)
    return '' if value in {'', '-', '--', '/', '无', '未填', '未填写', '暂无', 'NONE', 'NULL'} else value


def entry_certificate_field(record, certificates, certificate_info):
    """Build the certificate-number field without losing either source.

    The inspection/acceptance row remains the authoritative value when the
    certificate is missing, conflicting, or ambiguous.  When exactly one
    certificate page contains the same number and has no contradictory number
    observation, the value is still the entry value, but the certificate page
    becomes the primary locator.  The entry-cell evidence is retained so a
    reviewer can compare both documents.  This distinction is deliberately
    independent of the later certificate-heat-nameplate-delivery chain.
    """
    original = record.get('certificate_no')
    number = certificate_number(original)
    if not number:
        return None
    ev = deepcopy(record.get('field_evidence', {}).get('certificate_no') or {})
    ev.update(page_no=record.get('page_no'), source_role='inspection',
              record_id=record['record_id'], row_no=record.get('row_no'),
              cell_id=record.get('source_cells', {}).get('certificate_no'),
              source_result=record.get('source_result'), raw_text=ev.get('raw_text') or str(original),
              method='inspection_entry_certificate_number')
    comparison_candidates = []
    for cert in certificates:
        candidates = deepcopy(certificate_info[cert['candidate_id']].get('certificate_no', {}).get('candidates', []))
        for item in candidates:
            if not item.get('evidence'):
                item['evidence'] = source_locations(cert, 'certificate')
            for source in item['evidence']:
                source.update(page_no=cert['page_no'], source_role='certificate', candidate_id=cert['candidate_id'])
        comparison_candidates.extend(candidates)
    comparisons = field(comparison_candidates)
    observed = {certificate_number(item['value']) for item in comparisons['candidates']}
    observed.discard('')
    locations = [ev] + evs(comparisons)
    for cert in certificates:
        if not any(loc.get('page_no') == cert['page_no'] and loc.get('source_role') != 'inspection'
                   for loc in locations):
            locations.extend(source_locations(cert, 'certificate'))
    conflicts = sorted(value for value in observed if value != number)
    exact_certificates = []
    for cert in certificates:
        cert_values = {certificate_number(value)
                       for value in cert.get('certificate_numbers', [])}
        cert_values.discard('')
        cert_field = certificate_info.get(cert['candidate_id'], {}).get('certificate_no', {})
        cert_values.update(certificate_number(item.get('value'))
                           for item in cert_field.get('candidates', []))
        cert_values.discard('')
        if number in cert_values:
            exact_certificates.append(cert)
    exact_match = (len(certificates) == 1 and len(exact_certificates) == 1 and not conflicts and
                   observed == {number})
    certificate_locations = []
    if exact_match:
        exact = exact_certificates[0]
        exact_info = certificate_info.get(exact.get('candidate_id'), {})
        exact_field = exact_info.get('certificate_no', {})
        certificate_locations = evs(exact_field)
        if not certificate_locations:
            certificate_locations = source_locations(exact, 'certificate')
        for location in certificate_locations:
            location.update(page_no=exact['page_no'], source_role='certificate',
                            candidate_id=exact['candidate_id'])
    # Keep both evidence streams in the field, with the verified certificate
    # locator first only for an unambiguous exact match.
    evidence = (certificate_locations + [ev]) if exact_match else ([ev] + evs(comparisons))
    primary = field([dict(value=str(original), evidence=evidence)])
    primary['source_role'] = 'certificate' if exact_match else 'inspection'
    primary['alternatives'] = deepcopy(comparisons['candidates'])
    if conflicts:
        issue = diagnostic('certificate_number_conflict', '质保书编号',
            f'进场清单质保书编号 {original} 与质量证明书OCR编号 {"、".join(sorted(observed))} '
            '未完全一致；保留进场清单值，待导出前核验', locations,
            entry_value=str(original), certificate_values=sorted(observed))
    elif number not in observed:
        issue = diagnostic('certificate_number_not_matched', '质保书编号',
            f'进场清单质保书编号 {original} 未在对应质量证明书中精确确认；'
            '保留已提取值，待导出前核验', locations, entry_value=str(original), certificate_values=sorted(observed))
    elif len(certificates) != 1:
        issue = diagnostic('certificate_not_unique', '质保书编号',
            f'进场清单质保书编号 {original} 对应多份质量证明书，尚未唯一确认；保留清单值待核验',
            locations, entry_value=str(original), certificate_values=sorted(observed))
    else:
        issue = None
    if issue:
        primary['diagnostics'] = [issue]
        primary['status'] = 'needs_review'
    return primary


def row_key(row):
    return row['page_no'], row.get('entity_key'), row.get('row_id')


def form_key(candidate):
    return candidate['page_no'], candidate.get('form_id') or candidate.get('form_region_id') or candidate.get('entity_key')


def row_agreement(row, pi, *, numeric_grade=False):
    """Compare a nameplate with one physical delivery row.

    Cross-page matching may compare the unique three-digit strength class,
    while a same-page row retains its original full-grade comparison.  Both
    paths require every observed diameter to agree exactly; this function never
    guesses missing digits or changes saved OCR values.
    """
    grade, diameter = pi['grade']['value'], pi['diameter_mm']['value']
    grades, diameters = row.get('grades', []), row.get('diameters_mm', [])
    if numeric_grade:
        plate_grade_key = grade_number(grade)
        grade_keys = [grade_number(value) for value in grades]
        grade_ok = bool(plate_grade_key and grade_keys) and all(
            key == plate_grade_key for key in grade_keys)
    else:
        grade_ok = bool(grade and grades) and all(norm(value) == norm(grade) for value in grades)
    diameter_ok = bool(diameter and diameters) and all(same_number(diameter, value) for value in diameters)
    return grade_ok, diameter_ok


def nameplate_keyword_issues(record, pi, *, require_complete=True):
    """Return blocking keyword problems for a candidate nameplate.

    The entry-list-first route (no certificate number) requires both entry
    keywords and both nameplate keywords.  A certificate-number route already
    has an independent certificate -> heat -> nameplate key, so an unreadable
    grade/diameter is retained as a review diagnostic but does not block that
    heat-chain join.  Explicit conflicts, multiple OCR values and spatial/OCR
    warnings remain blocking on both routes.
    """
    reasons = []
    for key, label in (('grade', '牌号'), ('diameter_mm', '规格型号（直径）')):
        entry = record.get(key)
        observed = pi.get(key, {})
        value = observed.get('value')
        if require_complete and not entry:
            reasons.append('验收清单未识别到' + label)
        if require_complete and not value:
            reasons.append('铭牌未识别到唯一' + label)
        if observed.get('warnings') or len(observed.get('candidates', [])) > 1:
            reasons.append('铭牌' + label + '存在冲突或未检验成功')
        if entry and value and not (norm(entry) == norm(value) if key == 'grade' else same_number(entry, value)):
            reasons.append(f'验收清单{label} {entry} 与铭牌 {value} 不一致')
    # plate_fields emits this warning when either keyword is simply absent.
    # That is expected evidence on a certificate-first route; other
    # specification warnings indicate a conflict or unreliable scope and must
    # still block automatic association.
    spec_warnings = pi.get('specification', {}).get('warnings', [])
    incomplete_warning = '铭牌牌号或直径未完整提取，请核对关联送货单规格或原图。'
    if ((require_complete and spec_warnings) or
            any(str(warning) != incomplete_warning for warning in spec_warnings) or
            len(pi.get('specification', {}).get('candidates', [])) > 1):
        reasons.append('铭牌规格与牌号未检验成功')
    if set(record.get('issues', [])) & {'conflicting_grade_columns', 'model_or_grade_unreadable'}:
        reasons.append('验收清单牌号存在冲突或未清晰识别')
    return reasons


def delivery_choices(plate, pi, all_rows, deliveries, peers):
    """Select a physical row, giving physical co-location precedence over search.

    A sole same-page form can establish the header relationship even if no row
    is readable. Multiple cards never share an unidentified physical row.
    """
    same = [r for r in all_rows if r['page_no'] == plate['page_no']]
    local = [d for d in deliveries if d['page_no'] == plate['page_no']]
    # A page-local nameplate owns the delivery form on that page according to
    # the same-page-first rule. Do not reuse those forms as cross-page matches
    # for another plate that happens to share the same grade and diameter.
    if not local:
        occupied_pages = {d['page_no'] for d in deliveries
            if any(peer['page_no'] == d['page_no'] for peer in peers)}
        deliveries = [d for d in deliveries if d['page_no'] not in occupied_pages]
        all_rows = [r for r in all_rows if r['page_no'] not in occupied_pages]
        same = [r for r in same if r['page_no'] not in occupied_pages]
    # A saved legacy row can coexist with a reconstructed physical header when
    # the goods table is unreadable. That row is not evidence for a second
    # form. Prefer the explicitly located forms as the page's form inventory.
    located = [d for d in local if d.get('form_id') or d.get('form_region_id')]
    forms = {form_key(d) for d in located or local}
    if located:
        same = [r for r in same if form_key(r) in forms]

    def headers_for(rows=()):
        selected_forms = {form_key(r) for r in rows if form_key(r) in forms}
        keys = forms if len(forms) == 1 else selected_forms
        if len(keys) != 1:
            return []
        # Read header identity from the scoped physical form, never from an
        # unscoped legacy row if a physical header is available.
        candidates = [d for d in located or local if form_key(d) in keys]
        return sorted(candidates, key=lambda d: (not d.get('header_only'),
            not bool(d.get('form_region_id')), str(d.get('candidate_id'))))

    if forms:
        exact = [r for r in same if all(row_agreement(r, pi))]
        if exact:
            headers = headers_for(exact) if len(exact) > 1 else []
            return exact, 'same_page_exact', headers
        compatible = []
        for row in same:
            g, d = row_agreement(row, pi)
            if ((g or d) and (not row.get('grades') or g) and
                    (not row.get('diameters_mm') or d)):
                compatible.append(row)
        if compatible:
            # Co-location confirms a sole form, not its unreadable goods row.
            # Keep its company/No. while leaving incomplete row keywords for
            # final review. Partial keywords cannot choose among several forms.
            return [], 'same_page_partial_keywords', headers_for() if len(forms) == 1 else []
        if len(forms) == 1:
            # Header may be certain while allocation of several physical rows is not.
            return [], 'same_page_header', headers_for()
        return [], 'same_page_ambiguous', []
    return [r for r in all_rows if all(row_agreement(r, pi, numeric_grade=True))], 'grade_digits_and_exact_diameter', []


def ranked_delivery_candidates(pi, plate, all_rows, deliveries):
    by_id = {d['candidate_id']: d for d in deliveries}
    by_key = {row_key(d): d for d in deliveries}
    options = []
    for row in all_rows:
        same_page = bool(plate and row['page_no'] == plate['page_no'])
        g, d = row_agreement(row, pi, numeric_grade=not same_page)
        score = int(g) + int(d)
        if not score and not same_page:
            continue
        candidate = by_id.get(row.get('candidate_id')) or by_key.get(row_key(row))
        if not candidate:
            continue
        reasons = []
        if not g:
            reasons.append('牌号数字部分未识别、存在冲突或不一致' if not same_page else '牌号未识别、存在冲突或不一致')
        if not d:
            reasons.append('规格直径未识别、存在冲突或不一致')
        options.append(dict(kind='delivery_near_match', delivery=candidate, delivery_row=row,
                            score=score, same_page=same_page, write_allowed=False,
                            reason='；'.join(reasons) or '多个物理行具有相同规格和牌号数字部分',
                            locations=source_locations(candidate, 'delivery')))
    for candidate in deliveries:
        if not candidate.get('header_only'):
            continue
        options.append(dict(kind='delivery_near_match', delivery=candidate, delivery_row=None,
            score=0, same_page=bool(plate and candidate['page_no'] == plate['page_no']),
            write_allowed=False, reason='送货单表头已定位，货物行或规格牌号未能识别',
            locations=source_locations(candidate, 'delivery')))
    return sorted(options, key=lambda a: (-int(a['same_page']), -a['score'],
                                         a['delivery']['page_no'], str(a['delivery'].get('row_id'))))


def certificate_conflict_query(record, certificate, page):
    """Use a labelled certificate grade only for a single identified heat row.

    This is evidence for an exception candidate, never a replacement entry value
    or a matching key. A page-wide grade cannot complete another heat's row.
    """
    from .filling import GRADE, compact, page_blocks, bbox, same_line, evidence
    grade, diameter = norm(record.get('grade')), record.get('diameter_mm')
    if grade and diameter:
        return grade, diameter, []
    heats = {norm(v) for v in certificate.get('heat_lots', []) if heat_valid(v)}
    numbers = {certificate_number(v) for v in certificate.get('certificate_numbers', [])}
    numbers.discard('')
    diameters = {str(v) for v in certificate.get('diameters_mm', []) if v}
    heat_rows = {o.get('row_id') or (o.get('value_evidence') or {}).get('row_id')
        for o in certificate.get('field_observations', [])
        if o.get('field') == 'heat_lot_no' and o.get('accepted') is not False}
    if (len(heats) != 1 or len(numbers) != 1 or len(diameters) != 1 or
            len(heat_rows) != 1 or None in heat_rows):
        return '', '', []
    cert_diameter = next(iter(diameters))
    if diameter and not same_number(diameter, cert_diameter):
        return '', '', []
    blocks = page_blocks(page, certificate)
    labelled = []
    for label in blocks:
        text = compact(label.get('text'))
        if not text.startswith(('牌号:', '牌号：')):
            continue
        suffix = text[3:]
        value_block = label
        match = GRADE.fullmatch(suffix)
        if not suffix and bbox(label):
            right = [b for b in same_line(blocks, label) if b is not label and bbox(b)
                     and b.get('region_id') == label.get('region_id')
                     and bbox(b)[0] >= bbox(label)[2]-8]
            if right:
                neighbour = right[0]
                gap = bbox(neighbour)[0]-bbox(label)[2]
                if gap <= max(160, (bbox(label)[3]-bbox(label)[1])*8):
                    match = GRADE.fullmatch(compact(neighbour.get('text')))
                    value_block = neighbour
        if match and label.get('polygon_page') and value_block.get('polygon_page'):
            locations = [evidence(page, b, 'certificate_single_heat_labelled_grade')
                         for b in (label, value_block)]
            for ev in locations:
                ev.update(source_role='certificate', candidate_id=certificate['candidate_id'],
                          source_pixel_size=page.get('pixel_size'))
            labelled.append((match.group().upper(), locations))
    grades = {value for value, _ in labelled}
    if len(grades) != 1 or (grade and grade not in grades):
        return '', '', []
    return next(iter(grades)), diameter or cert_diameter, [ev for _, evs_ in labelled for ev in evs_]


def certificate_heat_conflicts(record, certificate, peers, plate_info, page):
    """Recall heat-conflict evidence, never an automatic association.

    Complete grade/specification queries retain their existing conflict
    candidates. If exactly one nameplate has a single-letter heat difference
    from the uniquely identified certificate, the caller may use that plate
    as an association while preserving this contradiction for review. If the
    grade is unreadable, an independently equal diameter can still locate the
    candidate; this partial query never supplies a grade.
    """
    cert_heats = sorted({norm(v) for v in certificate.get('heat_lots', []) if heat_valid(v)})
    grade, diameter, query_locations = certificate_conflict_query(record, certificate, page)
    if not cert_heats:
        return []
    certificate_numbers = {certificate_number(v) for v in certificate.get('certificate_numbers', [])}
    certificate_numbers.discard('')
    certificate_diameters = {str(v) for v in certificate.get('diameters_mm', []) if v}
    certificate_grades = {norm(v) for v in certificate.get('grades', []) if v}
    if (len(certificate_numbers) != 1 or len(certificate_diameters) > 1 or len(certificate_grades) > 1 or
            (record.get('diameter_mm') and certificate_diameters and
             any(not same_number(record['diameter_mm'], value) for value in certificate_diameters)) or
            (record.get('grade') and certificate_grades and
             any(norm(record['grade']) != value for value in certificate_grades))):
        return []
    # An explicit grade label that cannot be bound to a unique heat row is
    # ambiguous evidence, rather than a missing grade we may ignore.  A plain
    # page with no usable grade label can still yield a diameter+heat-typo
    # locator, without borrowing the certificate's global grade.
    unbound_grade_label = not query_locations and any(
        norm(block.get('text')).startswith(('牌号:', '牌号：'))
        for region in page.get('regions', []) for block in region.get('blocks', []))
    cert_heat_source = dict(certificate, field_observations=[o for o in certificate.get('field_observations', [])
                                                          if o.get('field') == 'heat_lot_no'])
    cert_locations = source_locations(cert_heat_source, 'certificate') + query_locations
    candidates = []
    for plate in peers:
        pi = plate_info[plate['candidate_id']]
        boundary_codes = {issue.get('code') if isinstance(issue, dict) else str(issue)
                          for issue in plate.get('issues', [])}
        # Missing keywords may be useful for a review-only candidate; known
        # contradictions and unreliable physical-card boundaries are not.
        incomplete_warning = '铭牌牌号或直径未完整提取，请核对关联送货单规格或原图。'
        specification = pi.get('specification', {})
        if (plate.get('spatial_unresolved') or
                'physical_card_boundary_not_verified' in boundary_codes or
                plate.get('scope') in ('full', 'model_plain_text', 'model_logical_table') or
                any(w != incomplete_warning for w in specification.get('warnings', [])) or
                len(specification.get('candidates', [])) > 1 or
                any(pi.get(key, {}).get('warnings') or len(pi.get(key, {}).get('candidates', [])) > 1
                    for key in ('grade', 'diameter_mm')) or
                bool({'conflicting_grade_columns', 'model_or_grade_unreadable'} & set(record.get('issues', [])))):
            continue
        observed_grade = norm(pi.get('grade', {}).get('value'))
        observed_diameter = pi.get('diameter_mm', {}).get('value')
        entry_grade, entry_diameter = norm(record.get('grade')), record.get('diameter_mm')
        if ((entry_grade and observed_grade and entry_grade != observed_grade) or
                (entry_diameter and observed_diameter and not same_number(entry_diameter, observed_diameter)) or
                (grade and observed_grade and grade != observed_grade) or
                (diameter and observed_diameter and not same_number(diameter, observed_diameter))):
            continue
        plate_heats = sorted({norm(v) for v in plate.get('heat_lots', []) if heat_valid(v)})
        if not plate_heats or set(cert_heats) & set(plate_heats):
            continue
        heat_differences = [difference
            for certificate_heat in cert_heats
            for nameplate_heat in plate_heats
            if (difference := single_heat_difference(certificate_heat, nameplate_heat))]
        complete_keywords = bool(grade and diameter and observed_grade == grade and observed_diameter and
                                 same_number(diameter, observed_diameter))
        equal_known_diameter = bool(entry_diameter and observed_diameter and
                                    same_number(entry_diameter, observed_diameter))
        if not complete_keywords and not (
                equal_known_diameter and heat_differences and len(certificate_numbers) == 1 and
                not unbound_grade_label):
            continue
        if ((certificate_diameters and observed_diameter and
             any(not same_number(value, observed_diameter) for value in certificate_diameters)) or
                (certificate_grades and observed_grade and
                 any(value != observed_grade for value in certificate_grades))):
            continue
        plate_locations = evs(pi['heat_lot_no']) or source_locations(plate, 'nameplate')
        for ev in plate_locations:
            ev.setdefault('page_no', plate['page_no'])
            ev.update(source_role='nameplate', candidate_id=plate['candidate_id'])
        keyword_description = '同规格牌号' if complete_keywords else '已知规格一致、牌号未完整确认的'
        reason = (f'质量证明书第{certificate["page_no"]}页炉批号 {"、".join(cert_heats)} 与{keyword_description}'
                  f'铭牌第{plate["page_no"]}页炉批号 {"、".join(plate_heats)} 不一致；'
                  '保留异常候选，未自动匹配或改写字符')
        if query_locations:
            reason += f'；查询牌号 {grade} 取自该证书单炉批的明确牌号标签，未补写进场记录'
        # A one-letter OCR substitution (for example CE2603337 vs
        # CB2603337) can identify a unique association, but must not rewrite
        # either source value. Keep the exact differing positions alongside
        # the page/ROI evidence below.
        if heat_differences:
            descriptions = '；'.join(item['description'] for item in heat_differences)
            reason += ('；检测到炉批号仅有单个字母差异（' + descriptions + '），'
                       '可能是OCR字符误识别；仅保留候选，需人工核验')
        candidates.append(dict(kind='certificate_nameplate_heat_conflict', nameplate=plate,
            certificate=certificate, certificate_heat_lots=cert_heats, nameplate_heat_lots=plate_heats,
            query_grade=grade, query_diameter_mm=diameter or entry_diameter,
            query_source=('certificate_single_heat_label' if query_locations else
                          ('inspection' if complete_keywords else 'inspection_partial_keywords')),
            reason=reason, locations=deepcopy(cert_locations + plate_locations),
            heat_lot_differences=heat_differences,
            heat_lot_review_locations=deepcopy(cert_locations + plate_locations),
            review_only=True, write_allowed=False,
            association_status='candidate_only'))
    return candidates


def build_automatic_ledger(result, index, catalog, records):
    from .nameplate_resources import bind_nameplate_resources
    records, certificate_dedup_diagnostics, certificate_dedup_summary = \
        deduplicate_certificate_records(records)
    pages = {p['page_no']: p for p in index['pages']}
    profile = result['filling_profile']
    peers, spatial_issues = bind_nameplate_resources(result.get('nameplate_inventory', []), index)
    result['nameplate_inventory'] = peers
    result['nameplate_spatial_diagnostics'] = spatial_issues
    plate_info = {c['candidate_id']: plate_fields(pages[c['page_no']], c,
                    peers=[p for p in peers if p['page_no'] == c['page_no']]) for c in peers}
    profile.setdefault('resources', {})['nameplate'] = deepcopy(plate_info)
    entry_duplicates = entry_duplicate_diagnostics(records)
    certificates = result.get('certificate_page_inventory', [])
    cert_info = {c['candidate_id']: certificate_fields(c, pages[c['page_no']]) for c in certificates}
    deliveries, all_rows = build_delivery_resources(index, catalog, result.get('delivery_inventory', []))
    result['delivery_inventory'] = deliveries
    delivery_info = {c['candidate_id']: delivery_fields(pages[c['page_no']], c) for c in deliveries}
    by_id = {d['candidate_id']: d for d in deliveries}
    by_key = {row_key(d): d for d in deliveries}
    pending = []
    for record in records['records']:
        grade, diameter = norm(record.get('grade')), record.get('diameter_mm')
        number = certificate_number(record.get('certificate_no'))
        route = 'certificate_number' if number else 'specification_grade'
        direct = [c for c in certificates if number and number in
                  {norm(v) for v in c.get('certificate_numbers', [])}]
        options, rejected_plates = [], []
        for plate in peers:
            if plate.get('spatial_unresolved'):
                continue
            pi = plate_info[plate['candidate_id']]
            heats = {norm(v) for v in plate.get('heat_lots', []) if heat_valid(v)}
            linked = [q for q in (direct if number else certificates)
                      if heats & {norm(v) for v in q.get('heat_lots', [])}]
            keyword_issues = nameplate_keyword_issues(record, pi, require_complete=not bool(number))
            if keyword_issues:
                related = bool(linked) if number else (
                    bool(grade and grade == norm(pi['grade']['value'])) or
                    bool(diameter and pi['diameter_mm']['value'] and same_number(diameter, pi['diameter_mm']['value'])))
                if related:
                    rejected_plates.append(dict(kind='nameplate_keyword_mismatch', nameplate=plate,
                        reason='；'.join(keyword_issues) + '；未自动匹配', write_allowed=False,
                        locations=evs(pi['specification']) + evs(pi['grade']) + evs(pi['diameter_mm']) +
                                  evs(pi['heat_lot_no']) or source_locations(plate, 'nameplate')))
                continue
            if number:
                if len(heats) != 1 or not linked:
                    continue
            if len(heats) != 1:
                linked = []
            dr, mode, headers = delivery_choices(plate, pi, all_rows, deliveries, peers)
            options.append(dict(plate=plate, certificates=linked, delivery_rows=dr,
                                delivery_mode=mode, delivery_headers=headers))
        # A single letter-level heat discrepancy can be used as an association
        # only when it identifies exactly one physical nameplate. We still
        # preserve both OCR values and emit a blocking-for-review diagnostic;
        # ambiguous candidates remain unselected.
        # A fuzzy one-letter fallback is safe only when the certificate has
        # one heat and no exact-heat nameplate exists.  If an exact heat card
        # is present but rejected for a keyword/boundary conflict, keep that
        # contradiction for review instead of replacing it with another card.
        exact_heat_plates = [plate for plate in peers
            if set(norm(v) for v in direct[0].get('heat_lots', []) if heat_valid(v)) &
               set(norm(v) for v in plate.get('heat_lots', []) if heat_valid(v))] if direct else []
        heat_conflicts = (certificate_heat_conflicts(record, direct[0], peers, plate_info,
                            pages[direct[0]['page_no']])
            if (not options and number and len(direct) == 1 and
                len({norm(v) for v in direct[0].get('heat_lots', []) if heat_valid(v)}) == 1 and
                not exact_heat_plates) else [])
        if (len(heat_conflicts) == 1 and len(heat_conflicts[0].get('heat_lot_differences', [])) == 1 and
                len(heat_conflicts[0]['certificate_heat_lots']) == 1 and
                len(heat_conflicts[0]['nameplate_heat_lots']) == 1):
            conflict = heat_conflicts[0]
            plate = conflict['nameplate']
            pi = plate_info[plate['candidate_id']]
            heat_info = pi.get('heat_lot_no', {})
            if (not heat_info.get('warnings') and len(heat_info.get('candidates', [])) == 1 and
                    norm(heat_info.get('value')) == conflict['nameplate_heat_lots'][0]):
                dr, mode, headers = delivery_choices(plate, pi, all_rows, deliveries, peers)
                associated = deepcopy(conflict)
                associated.update(review_only=False, write_allowed=True,
                                  association_status='auto_matched_with_heat_conflict',
                                  association_method='unique_single_letter_heat_difference')
                associated['reason'] = associated['reason'].replace(
                    '保留异常候选，未自动匹配或改写字符',
                    '已按唯一单字母差异自动关联，台账采用铭牌炉批号原文；与质量证明书存在矛盾，待人工核验'
                ).replace('仅保留候选，需人工核验', '已关联但未改写双方OCR原文，需人工核验')
                options = [dict(plate=plate, certificates=[direct[0]], delivery_rows=dr,
                                delivery_mode=mode, delivery_headers=headers,
                                heat_conflict=associated)]
        # Certificate ambiguity does not erase an independently identified plate.
        selected = options[0] if len(options) == 1 else None
        pending.append(dict(record=record, route=route, options=options, selected=selected, direct=direct,
                            rejected_plates=rejected_plates, heat_conflicts=heat_conflicts))
    plate_owners, delivery_owners = defaultdict(list), defaultdict(list)
    for p in pending:
        if p['selected']:
            selected = p['selected']
            plate_owners[selected['plate']['candidate_id']].append(p)
            if len(selected['delivery_rows']) == 1:
                delivery_owners[row_key(selected['delivery_rows'][0])].append(p)
    rows = []
    for p in pending:
        r, selected = p['record'], p['selected']
        loc = [deepcopy(r.get('field_evidence', {}).get('specification') or
               dict(page_no=r.get('page_no'), polygon_page=None, source_role='inspection', record_id=r['record_id']))]
        issues = [diagnostic(x, '匹配关键词', 'OCR字段或表格未检验成功：' + x, loc) for x in r.get('issues', [])]
        issues.extend(entry_duplicates.get(r['record_id'], []))
        unresolved_plates = [plate for plate in peers if plate.get('spatial_unresolved') and
            (not p['selected'] or plate['page_no'] == p['selected']['plate']['page_no'])]
        for plate in unresolved_plates:
            issues.extend(deepcopy(plate.get('spatial_diagnostics', [])))
        info, sources, alternatives = {}, {}, []
        alternatives.extend(dict(kind='unassigned_nameplate_observation', nameplate=plate,
            reason='原图位置未能唯一归属物理铭牌，保留异常候选', locations=source_locations(plate, 'nameplate'))
            for plate in unresolved_plates)
        fallback = {}
        contexts = profile.get('approval_contexts', [])
        preceding = [c for c in contexts if c['page_no'] <= r.get('page_no', 0)]
        context = max(preceding, key=lambda c: c['page_no']) if preceding else (contexts[0] if len(contexts) == 1 else None)
        if context:
            info.update(deepcopy(context['fields']))
        def record_field(key):
            value = r.get(key)
            ev = deepcopy(r.get('field_evidence', {}).get(key) or loc[0])
            return field([dict(value=str(value), evidence=[ev])] if value else [])
        for key in ('material_name', 'usage_location'):
            if not info.get(key, {}).get('value'):
                info[key] = record_field(key)
        quantity = inspection_fields(r, r['record_id'])['quantity_t']
        info['quantity_t'] = quantity
        # A direct number lookup is independent from plate selection.  A plate
        # with no usable heat or an incomplete/ambiguous downstream link must
        # not erase a unique, already observed certificate number/page.
        certs = (p['direct'] if p['route'] == 'certificate_number' else
                 (selected['certificates'] if selected else []))
        if len(certs) == 1:
            cert = certs[0]
            info.update(deepcopy(cert_info[cert['candidate_id']]))
            sources['certificate'] = cert
        elif certs:
            for cert in certs:
                alternatives.append(dict(kind='certificate_candidate', certificate=cert,
                    locations=evs(cert_info[cert['candidate_id']].get('certificate_no', field())) or source_locations(cert, 'certificate')))
            issues.append(diagnostic('certificate_not_unique', '质保书编号', '炉批号或编号对应多份质量证明书，保留候选待最终核验',
                                     [ev for a in alternatives for ev in a['locations']]))
        entry_number = entry_certificate_field(r, certs, cert_info)
        if entry_number is not None:
            info['certificate_no'] = entry_number
        delivery_selected = None
        if selected:
            plate = selected['plate']
            pi = deepcopy(plate_info[plate['candidate_id']])
            info.update(pi)
            sources['nameplate'] = plate
            if selected.get('heat_conflict'):
                conflict = deepcopy(selected['heat_conflict'])
                alternatives.append(conflict)
                issue = diagnostic('certificate_nameplate_heat_conflict', '炉批号',
                    conflict['reason'], conflict['locations'],
                    certificate_heat_lots=conflict['certificate_heat_lots'],
                    nameplate_heat_lots=conflict['nameplate_heat_lots'],
                    heat_lot_differences=deepcopy(conflict.get('heat_lot_differences', [])),
                    heat_lot_review_locations=deepcopy(conflict.get('heat_lot_review_locations', [])),
                    association_status='auto_matched_with_heat_conflict',
                    association_method='unique_single_letter_heat_difference',
                    written_heat_lot=next(iter(conflict['nameplate_heat_lots']), ''))
                info.setdefault('heat_lot_no', field())
                info['heat_lot_no'].setdefault('diagnostics', []).append(issue)
                sources['nameplate'] = dict(plate,
                    association_method='unique_single_letter_heat_difference',
                    certificate_heat_lots=deepcopy(conflict['certificate_heat_lots']),
                    nameplate_heat_lots=deepcopy(conflict['nameplate_heat_lots']))
            for key in ('manufacturer', 'heat_lot_no', 'specification'):
                fallback[key] = evs(pi.get(key, field())) or source_locations(plate, 'nameplate')
            if not certs:
                issues.append(diagnostic('certificate_not_found', '质保书编号', '铭牌炉批号未检索到质量证明书',
                                         evs(pi['heat_lot_no']) or source_locations(plate, 'nameplate')))
            dr = selected['delivery_rows']
            if len(dr) == 1:
                delivery_selected = by_id.get(dr[0].get('candidate_id')) or by_key.get(row_key(dr[0]))
            elif selected['delivery_headers']:
                # Share only a known form header, never a guessed goods row.
                delivery_selected = dict(selected['delivery_headers'][0], row_id=None,
                    physical_row_index=None, header_only=True, field_observations=[],
                    grades=[], diameters_mm=[], quantities_t=[])
            if delivery_selected:
                di = deepcopy(delivery_info[delivery_selected['candidate_id']])
                if delivery_selected.get('header_only'):
                    # Header evidence belongs to the form, not the first
                    # arbitrarily ordered row used to read that form's title.
                    bind_source(di, delivery_selected, 'delivery')
                sources['delivery'] = dict(delivery_selected, association_method=selected['delivery_mode'])
                if selected['delivery_mode'] == 'grade_digits_and_exact_diameter':
                    sources['delivery'].update(grade_numeric_key=grade_number(pi['grade']['value']),
                        nameplate_grade_raw=pi['grade']['value'],
                        delivery_grades_raw=deepcopy(dr[0].get('grades', [])),
                        delivery_grade_numeric_details=[grade_numeric_details(value)
                            for value in dr[0].get('grades', [])],
                        diameters_mm_raw=deepcopy(dr[0].get('diameters_mm', [])))
                for key in ('supplier', 'delivery_no'):
                    info[key] = di.get(key, field())
                    fallback[key] = evs(info[key]) or source_locations(delivery_selected, 'delivery')
                if delivery_selected.get('header_only'):
                    issues.append(diagnostic('delivery_row_unresolved', '送货单',
                        '铭牌与同页送货单已默认关联，供应单位和送货单编号保留；货物行牌号或规格缺失、冲突或存在多个候选，待最终核验',
                        evs(di.get('supplier', field())) + evs(di.get('delivery_no', field())) or
                        source_locations(delivery_selected, 'delivery')))
                if not delivery_selected.get('header_only'):
                    info['specification'] = resolve_specification(pi, di)
                    numeric_grade = selected['delivery_mode'] == 'grade_digits_and_exact_diameter'
                    if not all(row_agreement(dr[0], pi, numeric_grade=numeric_grade)):
                        issues.append(diagnostic('delivery_keywords_unverified', '送货单',
                            '同页关系已确认，但送货行规格或牌号未识别或与铭牌冲突', source_locations(delivery_selected, 'delivery')))
                    elif numeric_grade and not all(norm(value) == norm(pi['grade']['value'])
                                                   for value in dr[0].get('grades', [])):
                        # A relaxed alphabetic OCR join can fill supplier/No.,
                        # but its original words remain visible for final review.
                        delivery_grade_locations = []
                        for observation in dr[0].get('evidence', []):
                            if observation.get('field') != 'grade':
                                continue
                            location = deepcopy(observation.get('value_evidence') or {})
                            location.update(page_no=dr[0]['page_no'], source_role='delivery',
                                candidate_id=delivery_selected.get('candidate_id'), row_id=dr[0].get('row_id'))
                            delivery_grade_locations.append(location)
                        ignored = [item for value in dr[0].get('grades', [])
                            for item in grade_numeric_details(value)['ignored_leading_digits']]
                        suffix_reason = ('；按右端三位强度数字比较，忽略多识别的前置数字'
                            + '、'.join(item['ignored_prefix'] for item in ignored)) if ignored else ''
                        review = diagnostic('delivery_grade_numeric_match_review', '送货单',
                            '送货单与铭牌规格直径严格相同、牌号数字部分相同；'
                            '牌号文字OCR存在差异，已关联并提取送货信息，待导出前人工核验' + suffix_reason,
                            evs(pi['grade']) + (delivery_grade_locations or source_locations(delivery_selected, 'delivery')),
                            nameplate_grade=pi['grade']['value'],
                            delivery_grades=deepcopy(dr[0].get('grades', [])),
                            grade_numeric_key=grade_number(pi['grade']['value']),
                            ignored_leading_digits=ignored,
                            association_method='grade_digits_and_exact_diameter')
                        issues.append(review)
                        for key in ('supplier', 'delivery_no'):
                            info[key].setdefault('diagnostics', []).append(dict(deepcopy(review), field=LABELS[key]))
                    dq = di.get('quantity_t', field())
                    if quantity['value'] and dq.get('value') and not same_number(quantity['value'], dq['value']):
                        info['quantity_t']['warnings'].append('进场数量与送货行重量不一致；验收数量保留进场数量。')
                        info['quantity_t']['alternatives'] = deepcopy(dq.get('candidates', []))
            else:
                ambiguous_forms = selected['delivery_mode'] in ('same_page_ambiguous', 'same_page_partial_keywords')
                local_deliveries = [d for d in deliveries if d['page_no'] == plate['page_no']]
                issues.append(diagnostic('delivery_form_not_unique' if ambiguous_forms else
                    ('delivery_not_unique' if dr else 'delivery_not_found'), '送货单',
                    '同页存在多份送货单，未能唯一确定表单；保留候选供最终核验' if ambiguous_forms else
                    ('相同规格与牌号数字部分对应多个送货物理行' if dr else '未找到可唯一关联的送货物理行'),
                    [ev for d in local_deliveries for ev in source_locations(d, 'delivery')]
                    if ambiguous_forms else source_locations(plate, 'nameplate')))
            owners = plate_owners[plate['candidate_id']]
            if len(owners) > 1:
                issues.append(diagnostic('duplicate_resource', '铭牌', '同一铭牌被多条进场记录命中，可能重复或信息冲突',
                    fallback['heat_lot_no'], record_ids=[x['record']['record_id'] for x in owners]))
            if len(dr) == 1 and len(delivery_owners[row_key(dr[0])]) > 1:
                issues.append(diagnostic('duplicate_delivery_row', '送货单', '同一送货物理行被多条记录命中，未合计或拆分数量',
                    source_locations(delivery_selected, 'delivery'),
                    record_ids=[x['record']['record_id'] for x in delivery_owners[row_key(dr[0])]]))
        else:
            if not p['options'] and p['rejected_plates']:
                alternatives.extend(deepcopy(p['rejected_plates']))
                issues.append(diagnostic('nameplate_keywords_unverified', '规格品种',
                    ('质证编号与炉批号路线的已识别规格或牌号存在矛盾、冲突或未检验成功；'
                     if p['route'] == 'certificate_number' else
                     '铭牌必须与验收清单的牌号和规格型号同时完整匹配；') +
                    '；'.join(dict.fromkeys(a['reason'] for a in p['rejected_plates'])),
                    loc + [ev for a in p['rejected_plates'] for ev in a['locations']]))
            heat_conflicts = p.get('heat_conflicts', [])
            if heat_conflicts:
                alternatives.extend(heat_conflicts)
                for candidate in heat_conflicts:
                    issues.append(diagnostic('certificate_nameplate_heat_conflict', '炉批号',
                        candidate['reason'], candidate['locations'],
                        certificate_heat_lots=candidate['certificate_heat_lots'],
                        nameplate_heat_lots=candidate['nameplate_heat_lots'],
                        heat_lot_differences=deepcopy(candidate.get('heat_lot_differences', [])),
                        heat_lot_review_locations=deepcopy(candidate.get('heat_lot_review_locations', [])),
                        association_status=candidate.get('association_status', 'candidate_only')))
            else:
                issues.append(diagnostic('multiple_match_candidates' if p['options'] else 'match_not_found', '规格/牌号/质证编号',
                    '存在多个独立铭牌，未自动选择' if p['options'] else
                    ('质证编号未命中或炉批号链缺失' if p['route'] == 'certificate_number' else '未找到同一铭牌内同时匹配规格与牌号的可靠证据'), loc))
            for option in p['options']:
                plate = option['plate']
                alternatives.append(dict(nameplate=plate, certificates=option['certificates'], delivery_rows=option['delivery_rows'],
                    locations=evs(plate_info[plate['candidate_id']]['specification']) + evs(plate_info[plate['candidate_id']]['heat_lot_no'])))
            if not p['options'] and p['route'] == 'specification_grade':
                if p['rejected_plates']:
                    issues.append(diagnostic('specification_mismatch_candidates', '规格品种', '存在仅部分关键词匹配或关键词未检验成功的铭牌',
                        loc + [ev for a in p['rejected_plates'] for ev in a['locations']]))
        if not delivery_selected:
            query = plate_info[selected['plate']['candidate_id']] if selected else dict(
                grade=field([dict(value=r['grade'])]) if r.get('grade') else field(),
                diameter_mm=field([dict(value=r['diameter_mm'])]) if r.get('diameter_mm') else field())
            ranked = ranked_delivery_candidates(query, selected['plate'] if selected else None, all_rows, deliveries)
            for candidate in ranked:
                di = delivery_info[candidate['delivery']['candidate_id']]
                candidate['preview_fields'] = {key: deepcopy(di.get(key, field()))
                                               for key in ('supplier', 'delivery_no')}
                preview = '；'.join(label + '：' + di[key]['value'] for key, label in
                    [('delivery_no', '候选送货单编号'), ('supplier', '候选供应单位')]
                    if di.get(key, {}).get('value'))
                if preview:
                    candidate['reason'] += '；' + preview
            alternatives.extend(ranked)
            best = [a['delivery'] for a in ranked if (a['same_page'], a['score']) ==
                    (ranked[0]['same_page'], ranked[0]['score'])] if ranked else deliveries
            for key in ('supplier', 'delivery_no'):
                fallback[key] = [ev for d in best for ev in
                    (evs(delivery_info[d['candidate_id']].get(key, field())) or source_locations(d, 'delivery'))]
                info[key] = field(warnings=['送货单关联未检验成功；请在最终核验时核对候选送货单。'])
                candidates = field([c for d in best for c in
                    delivery_info[d['candidate_id']].get(key, {}).get('candidates', [])])
                info[key]['alternatives'] = candidates['candidates']
        if not info.get('specification', {}).get('value') and not p['options'] and r.get('grade') and r.get('diameter_mm'):
            info['specification'] = field([dict(value=f"{r['grade']}/Ф{r['diameter_mm']}", evidence=loc)])
        elif not info.get('specification', {}).get('value') and not p['options']:
            raw_spec = (r.get('fields', {}).get('specification') or {}).get('value') or r.get('raw_cells', {}).get('specification')
            if isinstance(raw_spec, str) and raw_spec.strip():
                evidence = deepcopy(r.get('field_evidence', {}).get('specification') or loc[0])
                evidence.update(page_no=r.get('page_no'), source_role='inspection', record_id=r['record_id'])
                missing = '、'.join(label for key, label in [('grade', '牌号'), ('diameter_mm', '规格型号')]
                                   if not r.get(key))
                info['specification'] = field([dict(value=raw_spec, evidence=[evidence])],
                    warnings=[f'验收清单{missing or "规格或牌号"}未检验成功；保留原规格文字，未自动匹配铭牌'])
        if 'manufacturer' not in fallback:
            cards = [a['nameplate'] for a in alternatives if a.get('nameplate')]
            fallback['manufacturer'] = [ev for c in cards or peers for ev in
                (evs(plate_info[c['candidate_id']].get('manufacturer', field())) or source_locations(c, 'nameplate'))]
        fields = {}
        cert_duplicate_issues = deepcopy(certificate_dedup_diagnostics.get(r['record_id'], []))
        for duplicate_issue in cert_duplicate_issues:
            info.setdefault('certificate_no', field()).setdefault('review_locations', [])
            for location in duplicate_issue.get('locations', []):
                if location not in info['certificate_no']['review_locations']:
                    info['certificate_no']['review_locations'].append(deepcopy(location))
        for key, label in LABELS.items():
            f = deepcopy(info.get(key, field()))
            locations = evs(f) or deepcopy(fallback.get(key)) or deepcopy(loc)
            for ev in locations:
                ev.setdefault('page_no', r.get('page_no'))
            own = deepcopy(f.get('diagnostics', []))
            if not f['value'] and key != 'file_no':
                own.append(diagnostic('missing_keyword', label, '关键词未识别到或存在多个值，需核对原图', locations))
            if len(f.get('candidates', [])) > 1:
                own.append(diagnostic('conflicting_values', label, '信息矛盾或存在多个OCR值', locations))
            for warning in f.get('warnings', []):
                if key == 'certificate_no' and warning.startswith('保留质证书编号原 OCR'):
                    continue
                own.append(diagnostic('unverified_keyword', label, warning, locations))
            if key == 'certificate_no':
                own.extend(deepcopy(cert_duplicate_issues))
            low = [ev for ev in locations if ev.get('score') is not None and ev['score'] < .90]
            if low:
                own.append(diagnostic('unclear_ocr', label, 'OCR置信度不足，识别不清晰', low))
            if key == 'certificate_no' and f.get('source_role') == 'inspection':
                polygon = locations[0]
            elif key == 'certificate_no' and f.get('source_role') == 'certificate':
                polygon = next((ev for ev in locations if ev.get('source_role') == 'certificate'), locations[0])
            else:
                polygon = next((ev for ev in locations if ev.get('polygon_page')), locations[0])
            fields[label] = dict(value=f['value'], page=polygon.get('page_no'), polygon=polygon.get('polygon_page'),
                source_pixel_size=polygon.get('source_pixel_size') or pages.get(polygon.get('page_no'), {}).get('pixel_size'),
                render_dpi=polygon.get('render_dpi') or pages.get(polygon.get('page_no'), {}).get('dpi'),
                evidence=locations, alternatives=f.get('alternatives', []) or f.get('candidates', []),
                diagnostics=own, status='needs_review' if own else 'observed', found_in_ocr=bool(f['value']) and not own)
            issues.extend(own)
        chain_complete = selected and len(sources) == 3 and not sources.get('delivery', {}).get('header_only')
        blocking = {'duplicate_resource', 'duplicate_delivery_row', 'delivery_keywords_unverified', 'delivery_row_unresolved',
                    'certificate_number_conflict', 'certificate_number_not_matched',
                    'duplicate_certificate_record_conflict'}
        matched_heats = sorted(
            {norm(v) for v in selected['plate'].get('heat_lots', []) if heat_valid(v)} &
            {norm(v) for cert in selected['certificates'] for v in cert.get('heat_lots', []) if heat_valid(v)}
        ) if selected else []
        path = (['inspection', 'certificate', 'nameplate', 'delivery'] if p['route'] == 'certificate_number'
                else ['inspection', 'nameplate', 'delivery', 'certificate'])
        associated_heat_conflict = bool(selected and selected.get('heat_conflict') and
            any(issue.get('code') == 'certificate_nameplate_heat_conflict' and
                issue.get('association_status') == 'auto_matched_with_heat_conflict' for issue in issues))
        blocking_issues = any(i['code'] in blocking for i in issues)
        match_status = ('matched_with_conflicts' if chain_complete and associated_heat_conflict and not blocking_issues else
            ('matched' if chain_complete and not blocking_issues
             else 'incomplete_or_ambiguous'))
        rows.append(dict(record_id=r['record_id'], fields=fields, match_route=p['route'],
            match_path=path, matched_heat_lots=matched_heats,
            match_status=match_status,
            diagnostics=issues, match_candidates=alternatives, sources=sources, reviewed=False,
            duplicate_record_ids=deepcopy(r.get('duplicate_record_ids', [])),
            duplicate_source_records=deepcopy(r.get('duplicate_source_records', []))))
    # Every diagnostic/candidate retains its own source pixel axes, including approval crops.
    def annotate(value):
        if isinstance(value,dict):
            if value.get('page_no') in pages:
                page=pages[value['page_no']]
                size=value.get('source_pixel_size') or value.get('page_pixel_size')
                if not size and page.get('pixel_size'):
                    ratio=(value.get('render_dpi') or page.get('dpi') or 180)/(page.get('dpi') or 180)
                    size=[n*ratio for n in page['pixel_size']]
                if size:value['source_pixel_size']=size
            for child in list(value.values()):annotate(child)
        elif isinstance(value,list):
            for child in value:annotate(child)
    annotate(rows)
    result['automatic_ledger']=dict(schema_version='automatic-ledger-v1',pdf_sha256=index['pdf_sha256'],
        pdf_name=index.get('pdf_name'),page_count=len(index['pages']),records=rows,
        diagnostics=deepcopy(records.get('extraction_issues',[])),review_stage='before_excel_export',
        matching_requires_human=False,
        certificate_number_deduplication=deepcopy(certificate_dedup_summary))
    result['metrics']['automatic_ledger_rows']=len(rows)
    result['metrics']['automatic_complete_rows']=sum(r['match_status']=='matched' for r in rows)
    result['metrics']['automatic_matched_with_conflicts_rows']=sum(
        r['match_status']=='matched_with_conflicts' for r in rows)
    return result['automatic_ledger']
