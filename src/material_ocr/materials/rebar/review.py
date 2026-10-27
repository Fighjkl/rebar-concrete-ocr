"""Build a date-free same-page review from a new PDF's saved OCR evidence."""
import argparse
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from material_ocr.review.render import build
from material_ocr.matching.catalog import build_keyword_catalog
from material_ocr.materials.rebar.matching import heat_valid, norm, observations
from material_ocr.materials.rebar.matching import rematch


def recall_plate_fields(index, catalog):
    """Retain labelled heat crops from physical plate regions as review evidence.

    Focus crops are observations, never independent document or delivery-row
    candidates. A heat value must have its own saved polygon and accepted label.
    """
    for pg in index['pages']:
        if pg.get('document_type') != 'delivery_note':
            continue
        for region in pg.get('regions', []):
            if region.get('priority_field') != 'heat_lot_no':
                continue
            plate_region = region.get('document_type') == 'nameplate' or region.get('entity_role') == 'nameplate'
            # Some vendors print the plate fields inline inside a delivery page.
            inline_plate = '轧制批号' in region.get('text', '') and '牌号' in region.get('text', '') and '规格' in region.get('text', '')
            if not (plate_region or inline_plate):
                continue
            for value in region.get('extracted_fields', {}).get('heat_lot_no', []):
                evidence = value.get('evidence') or {}
                key = norm(value.get('normalized_value') or value.get('value'))
                if value.get('validation_status') != 'accepted' or not evidence.get('polygon_page') or not heat_valid(key):
                    continue
                catalog['items'].append(dict(role='nameplate', field='heat_lot_no', page_no=pg['page_no'],
                    region_id=region['region_id'], entity_key=region.get('entity_key'), row_id=None,
                    accepted=True, status='labelled_focus_observation', normalized_value=key,
                    raw_value=value.get('value'), ocr_raw_value=value.get('raw_value'),
                    value_evidence=deepcopy(evidence), value_evidence_refs=[deepcopy(evidence)],
                    recall_method='saved_labelled_physical_plate_heat_crop'))
    catalog['item_count'] = len(catalog['items'])


def main_region(page):
    """Return the saved OCR region representing the page as a whole.

    OCR layout splitting can suffix the full-page region with entity names;
    those regions still contain the page-level label evidence needed here.
    """
    page_no = page['page_no']
    exact = [r for r in page.get('regions', []) if r.get('region_id') == f'p{page_no:03d}_full']
    if exact:
        return exact[0]
    pref = [r for r in page.get('regions', []) if r.get('region_id', '').startswith(f'p{page_no:03d}_full')]
    if pref:
        return max(pref, key=lambda r: (len(r.get('blocks', [])), len(r.get('text', ''))))
    return max(page.get('regions', []), key=lambda r: (len(r.get('blocks', [])), len(r.get('text', ''))),
               default=dict(region_id=f'p{page_no:03d}_full', blocks=[], text='', entity_key='full'))


def bbox(block):
    polygon = block.get('polygon_ocr') or block.get('polygon_page')
    xs, ys = zip(*polygon)
    return min(xs), min(ys), max(xs), max(ys)


def recall_joined_certificate_heats(index, catalog):
    """Split exact heat+grade OCR only below its own table headers.

    Keep the merged polygon; never repair characters, invent a sub-box,
    consult another region or accept an unlabelled identifier.
    """
    joined = re.compile(r'([A-Z0-9][A-Z0-9./-]{4,}?)((?:HRBF?|HPB|RRB|CRB)\d{3,4}(?:E|B)?)', re.I)
    heat_label = re.compile(r'^(?:炉批号|批号|炉号|HEATNO|BATCHNO)[:：.]?$', re.I)
    grade_label = re.compile(r'^(?:牌号|STEELGRADE|GRADE)[:：.]?$', re.I)
    for page in index.get('pages', []):
        if page.get('document_type') != 'quality_certificate':
            continue
        for region in page.get('regions', []):
            if region.get('document_type', page['document_type']) != 'quality_certificate':
                continue
            blocks = [b for b in region.get('blocks', []) if b.get('polygon_page')]
            heats = [b for b in blocks if heat_label.fullmatch(norm(b.get('text')))]
            grades = [b for b in blocks if grade_label.fullmatch(norm(b.get('text')))]
            for label in heats:
                x0, y0, x1, y1 = bbox(label)
                h = max(1, y1-y0); hx=(x0+x1)/2; hy=(y0+y1)/2
                peers = [b for b in grades if bbox(b)[0] > x1 and
                         abs((bbox(b)[1]+bbox(b)[3])/2-hy) <= h*.75]
                if len(peers) != 1:
                    continue
                peer = peers[0]; gx0, gy0, gx1, gy1 = bbox(peer); gx=(gx0+gx1)/2
                for block in blocks:
                    match = joined.fullmatch(norm(block.get('text')))
                    if (not match or not heat_valid(match.group(1)) or
                            re.search(r'(?:HRBF?|HPB|RRB|CRB)\d',match.group(1),re.I) or
                            re.match(r'XK\d',match.group(1),re.I)):
                        continue
                    bx0, by0, bx1, by1 = bbox(block)
                    if not (y1 < by0 <= y1+h*8 and bx0 <= hx <= bx1 and
                            bx0 <= gx <= bx1 and bx1 <= gx1+h*.75 and bx0 >= x0-h*2):
                        continue
                    value = match.group(1)
                    ev = dict(page_no=page['page_no'], region_id=region['region_id'],
                        polygon_page=deepcopy(block['polygon_page']), raw_text=block.get('text'),
                        source_image=page.get('source_image'), rotation_ccw=page.get('rotation_ccw',0),
                        label_raw_text=label.get('text'), label_polygon_page=deepcopy(label['polygon_page']),
                        grade_label_raw_text=peer.get('text'), grade_label_polygon_page=deepcopy(peer['polygon_page']),
                        method='same_table_joined_heat_grade', matched_heat_text=value,
                        matched_grade_text=match.group(2), polygon_scope='original_merged_heat_and_grade_block')
                    catalog['items'].append(dict(role='quality_certificate',field='heat_lot_no',
                        page_no=page['page_no'],region_id=region['region_id'],entity_key=region.get('entity_key','full'),
                        row_id=None,accepted=True,status='labelled_joined_table_observation',
                        normalized_value=value,raw_value=value,ocr_raw_value=block.get('text'),
                        value_evidence=ev,value_evidence_refs=[deepcopy(ev)]))
    catalog['item_count'] = len(catalog['items'])


def certificate_identifiers(index, allowed_pages):
    """Recall identifier values only beside explicit certificate number labels."""
    # Manufacturer forms use both Chinese labels and ``No.``/``No:``.
    # Chinese explicit labels remain preferred. Generic No labels require a
    # certificate-number format, excluding licence/template numbers.
    labels = re.compile(r'(?:质量证明书编号|质保书编号|证书编号|质编号|证明书号|(?<![A-Z])NO)\s*[.:：]?\s*', re.I)
    valid = lambda s: bool(re.fullmatch(r'[A-Z0-9][A-Z0-9._/-]{2,}', norm(s)) and re.search(r'\d', norm(s)))
    found = []
    for pg in index['pages']:
        if pg['page_no'] not in allowed_pages:
            continue
        full = main_region(pg)
        blocks = list(full['blocks'])
        for region in pg.get('regions', []):
            if region.get('region_id') == f"p{pg['page_no']:03d}_full_form":
                blocks.extend(b for b in region.get('blocks', [])
                              if re.match(r'^NO\s*[.:：]?\s*[A-Z]{1,4}\d{6,}',norm(b.get('text',''))))
        for label in blocks:
            match = labels.search(norm(label['text']))
            if not match:
                continue
            inline = norm(label['text'])[match.end():]
            generic = match.group().upper().startswith('NO')
            valid_value = lambda s: valid(s) and (not generic or bool(re.fullmatch(r'[A-Z]{1,4}\d{6,}(?:[./-]\d+)*|\d{6,}', norm(s))))
            value_block = label if valid_value(inline) else None
            method = 'explicit_no_label_inline' if match.group().upper().startswith('NO') else 'explicit_chinese_label_inline'
            if not value_block:
                x0, y0, x1, y1 = bbox(label)
                yc, h = (y0+y1)/2, max(1, y1-y0)
                neighbours = []
                for b in blocks:
                    if b is label or not valid_value(b['text']):
                        continue
                    a, c, d, e = bbox(b)
                    if a >= x1-h*.25 and abs((c+e)/2-yc) <= h*.75 and a-x1 < h*12:
                        neighbours.append((max(0, a-x1), b))
                if neighbours:
                    value_block = min(neighbours, key=lambda x: x[0])[1]
                    inline = norm(value_block['text'])
                    method = 'explicit_no_label_same_line_right' if match.group().upper().startswith('NO') else 'explicit_chinese_label_same_line_right'
            if value_block:
                found.append(dict(value=inline, page_no=pg['page_no'], method=method,
                                  evidence=dict(pdf_sha256=index['pdf_sha256'], page_no=pg['page_no'],
                                                region_id=full['region_id'], raw_text=value_block['text'],
                                                score=value_block.get('score'),
                                                polygon_page=value_block.get('polygon_page'),
                                                polygon_ocr=value_block.get('polygon_ocr'),
                                                label_raw_text=label['text'], label_polygon_page=label.get('polygon_page'),
                                                source_image=pg.get('source_image'), rotation_ccw=pg.get('rotation_ccw'))))
    return list({(c['page_no'], c['value']): c for c in found}.values())


def classify(index, catalog):
    categories = {role: [] for role in ['inspection_record', 'approval_form', 'delivery_note',
                                      'nameplate', 'quality_certificate', 'test_report', 'unknown']}
    pages = []
    for pg in index['pages']:
        full = main_region(pg)
        text = norm('\n'.join(r['text'] for r in pg.get('regions', [])
                              if r['region_id'].startswith(f"p{pg['page_no']:03d}_full")))
        # A scanned delivery note can share a page with a plate and may also
        # contain OCR fragments that look like a certificate. Explicit
        # delivery anchors take precedence over a page-level certificate hint.
        mixed_delivery = ('送货单' in text and
                          ('规格/材质' in text or '签收数量' in text or 'NO:XSD' in text))
        if mixed_delivery:
            roles = ['delivery_note']
            has_plate = ('轧制批号' in text or '炉批号' in text or
                         ('牌号' in text and '规格' in text and '生产厂' in text) or
                         any(r.get('document_type') == 'nameplate' or r.get('entity_role') == 'nameplate'
                             for r in pg.get('regions', [])))
            if has_plate:
                roles.append('nameplate')
        elif '进场检验记录' in text:
            roles = ['inspection_record']
        elif '钢筋原材检测报告' in text:
            roles = ['test_report']
        else:
            role = pg['document_type']
            roles = [role if role in categories else 'unknown']
            physical_plate = any(r.get('entity_role') == 'nameplate' or r.get('document_type') == 'nameplate'
                                 for r in pg.get('regions', []))
            physical_delivery = any(r.get('document_type') == 'delivery_note' for r in pg.get('regions', []))
            if role == 'nameplate' and physical_delivery:
                roles.append('delivery_note')
            if role == 'delivery_note' and (physical_plate or any(c['role'] == 'nameplate' and c['field'] == 'heat_lot_no'
                                             and c['page_no'] == pg['page_no'] for c in catalog)):
                roles.append('nameplate')
        for role in roles:
            categories[role].append(pg['page_no'])
        pages.append(dict(page_no=pg['page_no'], classification=roles,
                          original_page_document_type=pg['document_type'],
                          classification_status='pending_user_review',
                          classification_basis='explicit_page_title_precedence_and_saved_physical_nameplate_region',
                          raw_text=full['text'], ocr_blocks=full['blocks']))
    return dict(pdf_sha256=index['pdf_sha256'], pdf_name=index['pdf_name'],
                status='pending_user_review', metrics=dict(category_pages=categories, page_count=len(pages)), pages=pages)


def refresh_cached_quantity_lists(index, saved_records):
    """Apply today's list parser to cached OCR without overwriting its inputs.

    Inspection/template backends remain authoritative on their own pages.
    Missing rows and failed parses preserve the prior observation with a
    diagnostic instead of silently losing an arrival-quantity record.
    """
    from .quantity_list import extract_quantity_lists
    backend = 'quantity_list_spatial_v1'
    records = deepcopy(saved_records)
    original = records.get('records', [])
    other = [r for r in original if r.get('extraction_backend') != backend]
    excluded = {r.get('page_no') for r in other}
    old_pages = {}
    for row in original:
        if row.get('extraction_backend') == backend:
            old_pages.setdefault(row.get('page_no'), []).append(row)
    issues = list(records.get('extraction_issues', []))
    refreshed = {};reparsed = [];retained = []
    def issue(code,page_no,reason,**extra):
        value = dict(code=code,page_no=page_no,reason=reason,
                     source_role='inspection',locations=[dict(page_no=page_no,
                     source_role='inspection',polygon_page=None)],**extra)
        if value not in issues:issues.append(value)
    for page in index.get('pages', []):
        number = page['page_no']
        old = old_pages.get(number, [])
        if number in excluded:
            if old:
                refreshed[number] = old
                retained.append(number)
                issue('quantity_list_refresh_page_excluded',number,
                      '该页已有其他检验提取后端记录，未重解析量表；旧量表行保留供核验。')
            continue
        try:
            parsed = extract_quantity_lists(dict(index,pages=[page]))
        except Exception as exc:
            parsed = []
            issue('quantity_list_cache_refresh_failed',number,
                  '缓存进场数量清单重解析失败，保留旧记录：'+type(exc).__name__,
                  error_type=type(exc).__name__)
        ids = [r.get('record_id') for r in parsed]
        if any(not rid for rid in ids) or len(ids) != len(set(ids)):
            parsed = []
            issue('quantity_list_cache_refresh_invalid_rows',number,
                  '缓存清单重解析产生缺失或重复记录ID，保留旧记录。')
        if not parsed:
            if old:
                refreshed[number] = old
                retained.append(number)
                issue('quantity_list_cache_refresh_unreadable',number,
                      '已有进场数量清单无法从缓存OCR重新定位，保留旧行及原字段供核验。')
            continue
        by_id = {r['record_id']:r for r in parsed}
        missing = [r for r in old if r.get('record_id') not in by_id]
        for row in missing:
            if row.get('record_id'):
                by_id[row['record_id']] = row
        if missing:
            retained.append(number)
            issue('quantity_list_cache_refresh_incomplete',number,
                  '缓存清单只重读到部分旧行；未重读行仍保留，请核验原图。',
                  record_ids=[r.get('record_id') for r in missing])
        refreshed[number] = list(by_id.values())
        reparsed.append(number)
    # Cached records can refer to a page absent from a damaged OCR index.
    for number,old in old_pages.items():
        if number not in refreshed and not any(p['page_no']==number for p in index.get('pages',[])):
            refreshed[number]=old;retained.append(number)
            issue('quantity_list_cache_page_missing',number,'缓存OCR缺少旧量表页面，保留旧记录供核验。')
    updated=[];seen=set()
    for row in other+[r for rows in refreshed.values() for r in rows]:
        rid=row.get('record_id')
        if rid and rid in seen and row.get('extraction_backend') == backend:
            issue('quantity_list_cache_duplicate_record_id',row.get('page_no'),
                  '缓存记录存在重复记录ID，保留首条并标记重复供核验。',record_id=rid)
            continue
        if rid:seen.add(rid)
        updated.append(row)
    records['records']=sorted(updated,key=lambda r:(r.get('page_no',0),r.get('row_no',0),str(r.get('record_id',''))))
    records['extraction_issues']=issues
    records['quantity_list_cache_refresh']=dict(backend=backend,reparsed_pages=sorted(set(reparsed)),
        retained_pages=sorted(set(retained)),source_records_unchanged=True,
        cached_ocr_only=True,record_count=len(updated))
    from .entry_duplicates import deduplicate_certificate_records
    records, _, _ = deduplicate_certificate_records(records)
    records['quantity_list_cache_refresh']['record_count'] = len(records['records'])
    return records


def generate_review(pdf, index_path, records_path, output, *, rules=None, matching_options=None):
    """Steel-specific keys; reusable rendering and explicit input paths."""
    from types import SimpleNamespace
    args = SimpleNamespace(pdf=Path(pdf), index=Path(index_path), records=Path(records_path), output=Path(output))
    index = json.loads(args.index.read_text())
    records = json.loads(args.records.read_text())
    assert hashlib.sha256(args.pdf.read_bytes()).hexdigest() == index['pdf_sha256'] == records['pdf_sha256']
    records = refresh_cached_quantity_lists(index, records)
    from material_ocr.ocr.attachments import build_regions, classify_page_text
    from material_ocr.ocr.layout import split_entities
    for page in index['pages']:
        full = main_region(page)
        corrected = classify_page_text(full['text'], rules=rules)
        if (page['document_type'] == 'quality_certificate' and corrected == 'delivery_note'
                and full.get('entity_key', 'full') == 'full' and split_entities(full['blocks'])):
            replacement = build_regions(full['blocks'], page_no=page['page_no'],
                source_image=page.get('source_image'), rotation_ccw=page.get('rotation_ccw',0),
                rules=rules, document_type=corrected)
            page['regions'] = [r for r in page['regions'] if r is not full] + replacement
            page['document_type'] = corrected
    catalog = build_keyword_catalog(index, rules)
    recall_plate_fields(index, catalog)
    recall_joined_certificate_heats(index, catalog)
    classification = classify(index, catalog['items'])
    for record in records['records']:
        if record.get('extraction_backend') == 'quantity_list_spatial_v1':
            page_no = record['page_no']
            category = classification['metrics']['category_pages']['inspection_record']
            if page_no not in category:
                category.append(page_no)
    roles = classification['metrics']['category_pages']
    # The raw index stays read-only. Exclude false certificate/plate sources by page class.
    catalog['items'] = [c for c in catalog['items'] if c['page_no'] in roles.get(c['role'], [])]
    catalog['item_count'] = len(catalog['items'])
    certs = certificate_identifiers(index, set(roles['quality_certificate']))
    plates = observations(catalog['items'], 'nameplate', 'heat_lot_no', set(roles['nameplate']))
    cert_heats = observations(catalog['items'], 'quality_certificate', 'heat_lot_no', set(roles['quality_certificate']))
    heat_pairs = [dict(nameplate_page=p['page_no'], certificate_page=h['page_no'], heat_lot_no=p['value'],
                       evidence=dict(nameplate=deepcopy(p), certificate=deepcopy(h)))
                  for p in plates for h in cert_heats if p['value'] == h['value']]
    deliveries = [c for c in catalog['items'] if c['role'] == 'delivery_note' and c['field'] == 'quantity_t'
                  and c.get('accepted') and c.get('value_t') is not None]
    entries = []
    for record in records['records']:
        direct = [c for c in certs if norm(c['value']) == norm(record.get('certificate_no'))]
        qpages = {c['page_no'] for c in direct}
        quantity = [d for d in deliveries if record.get('weight_t') is not None
                    and Decimal(d['value_t']) == Decimal(record['weight_t'])]
        entries.append(dict(record_id=record['record_id'], record=deepcopy(record),
                            quality_certificate_candidates=direct, delivery_note_candidates=quantity,
                            nameplate_candidates=[p for p in heat_pairs if p['certificate_page'] in qpages],
                            existing_pipeline_cross_checks=[]))
    source = args.output.with_name(args.output.name + '-inputs')
    source.mkdir(parents=True, exist_ok=False)
    def save(name, data):
        path = source / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return path
    inputs = dict(baseline=save('explicit_matches.json', dict(pdf_sha256=index['pdf_sha256'], entries=entries,
                  metrics=dict(certificate_identifiers=certs))),
                  classification=save('recognition_results.json', classification),
                  catalog=save('keyword_catalog.json', dict(keyword_catalog=catalog)),
                  records=save('records_for_matching.json', records), index=args.index)
    from material_ocr.materials.rebar.filling import build_filling_profile
    # Older merged records reference their original cell OCR instead of
    # carrying coordinates. Read only results inside this inspection folder.
    inspection_evidence, source_cache = {}, {}
    for record in records['records']:
        if record.get('field_evidence', {}).get('weight'):
            inspection_evidence[record['record_id']] = record['field_evidence']['weight']
            continue
        reference = record.get('source_result')
        if not reference:
            continue
        path = (args.records.parent / reference).resolve()
        if not path.is_relative_to(args.records.parent.resolve()) or not path.is_file():
            continue
        if path not in source_cache:
            source_cache[path] = json.loads(path.read_text())
        cell_id = record.get('source_cells', {}).get('weight')
        cell = source_cache[path].get('cells', {}).get(cell_id)
        if cell:
            inspection_evidence[record['record_id']] = dict(cell_id=cell_id,
                polygon_page=cell.get('polygon_page'), raw_text=cell.get('raw_text'),
                source_pixel_size=source_cache[path].get('page', {}).get('pixel_size'),
                coordinate_system=source_cache[path].get('page', {}).get('coordinate_system'),
                blocks=cell.get('blocks'), crop_path=cell.get('crop_path'))
    def enrich(result):
        # Keep both evidence streams: submission OCR supplies approval
        # contexts, while the saved inspection cell supplies authoritative
        # arrival quantity and its original coordinates.
        result['submission_duplicate_candidates'] = deepcopy(
            records.get('submission_duplicate_candidates', []))
        result['filling_profile'] = build_filling_profile(result, index, classification,
            inspection_evidence=inspection_evidence,
            submission_forms=records.get('submission_forms', []),
            submission_duplicates=records.get('submission_duplicate_candidates', []))
        from .automatic import build_automatic_ledger
        build_automatic_ledger(result, index, catalog['items'], records)
    return build(args.output, inputs=inputs, pdf=args.pdf, base=args.index.parent,
                 matcher=rematch, matching_options=matching_options, result_enricher=enrich)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--index', type=Path, required=True)
    parser.add_argument('--records', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    generate_review(args.pdf, args.index, args.records, args.output)


if __name__ == '__main__':
    main()
