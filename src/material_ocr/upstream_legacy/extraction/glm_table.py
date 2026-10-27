"""GLM HTML -> legacy records, with logical cells (never invented pixel boxes)."""
from collections import Counter
from copy import deepcopy
from decimal import Decimal
from html.parser import HTMLParser
import re

from material_ocr.upstream_legacy.materials.rebar.entry_schema import FIELD_ALIASES, OPTIONAL_ALIASES, compact, header_matches, parse_specification, quantity_header
from material_ocr.upstream_legacy.materials.rebar.coordinate_match import field_value, split_shared_cell


class TableParser(HTMLParser):
    """Bounded, strict structural parsing. Unclosed/overlapping tables fail closed."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.table = self.row = self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            if self.table is not None:
                raise ValueError('nested_table_not_supported')
            self.table = []
        elif tag == 'tr' and self.table is not None:
            if self.row is not None:
                raise ValueError('unclosed_table_row')
            self.row = []
        elif tag in ('td', 'th') and self.table is not None:
            if self.row is None or self.cell is not None:
                raise ValueError('invalid_cell_nesting')
            attrs = dict(attrs)
            spans = {}
            for key in ('rowspan', 'colspan'):
                value = attrs.get(key, '1')
                if not value or not re.fullmatch(r'[0-9]{1,4}', value) or not 1 <= int(value) <= 500:
                    raise ValueError('invalid_' + key)
                spans[key] = int(value)
            self.cell = {**spans, 'parts': []}
        elif tag == 'br' and self.cell is not None:
            self.cell['parts'].append('\n')
        elif tag in ('script', 'style', 'iframe') and self.table is not None:
            raise ValueError('unexpected_active_content_in_table')

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.table is not None:
            if self.cell is None:
                raise ValueError('unexpected_cell_end')
            text = ''.join(self.cell.pop('parts')).strip()
            self.row.append({**self.cell, 'raw_text': text})
            self.cell = None
        elif tag == 'tr' and self.table is not None:
            if self.row is None or self.cell is not None:
                raise ValueError('unclosed_cell')
            self.table.append(self.row)
            self.row = None
            if len(self.table) > 1000:
                raise ValueError('table_too_large')
        elif tag == 'table':
            if self.table is None or self.row is not None or self.cell is not None:
                raise ValueError('unclosed_row_or_cell')
            self.tables.append(self.table)
            self.table = None
            if len(self.tables) > 30:
                raise ValueError('too_many_tables')
        elif tag in ('p', 'div') and self.cell is not None:
            self.cell['parts'].append('\n')

    def handle_data(self, data):
        if self.cell is not None:
            self.cell['parts'].append(data)


def parse_tables(raw):
    if len(raw) > 2_000_000:
        raise ValueError('model_output_too_large')
    parser = TableParser()
    parser.feed(raw)
    parser.close()
    if parser.table is not None or not parser.tables:
        raise ValueError('missing_or_unclosed_html_table')
    tables = []
    for ti, rows in enumerate(parser.tables):
        cells, occupied = [], set()
        for ri, row in enumerate(rows):
            ci = 0
            for source in row:
                while (ri, ci) in occupied:
                    ci += 1
                rs, cs = source['rowspan'], source['colspan']
                if ci + cs > 200 or ri + rs > len(rows):
                    raise ValueError('span_outside_table')
                slots = {(r, c) for r in range(ri, ri+rs) for c in range(ci, ci+cs)}
                if slots & occupied:
                    raise ValueError('overlapping_html_cells')
                occupied.update(slots)
                cells.append({**source, 'cell_id': f't{ti}_r{ri}_c{ci}',
                    'grid': dict(row_start=ri, row_end=ri+rs, col_start=ci, col_end=ci+cs, rowspan=rs, colspan=cs),
                    'lines': source['raw_text'].splitlines(), 'normalized_text': re.sub(r'[\s:：]', '', source['raw_text']),
                    'bbox': None, 'tokens': [], 'issues': []})
                ci += cs
        width = max((c['grid']['col_end'] for c in cells), default=0)
        if not width or len(occupied) != len(rows) * width:
            raise ValueError('ragged_or_missing_html_cells')
        tables.append(cells)
    return tables


def columns(cell):
    g = cell['grid']
    return g['col_start'], g['col_end']


def logical_evidence(field):
    for name in ('evidence', 'unit_header_evidence', 'header_evidence'):
        ev = field.get(name)
        if ev:
            ev.pop('cell_bbox', None)
            ev.pop('ocr_tokens', None)
            ev.update(coordinate_system='html_logical_grid', pixel_coordinates_available=False)
    field['ocr_confidence'] = None
    field['confidence_source'] = 'not_provided_by_glm_ocr'
    return field


def table_records(cells, form_type):
    issues, records, blanks = [], [], []
    serials = [c for c in cells if compact(c['raw_text']) == '序号']
    if len(serials) != 1:
        return records, blanks, ['serial_header_missing_or_ambiguous'], {}
    serial = serials[0]
    start, end = serial['grid']['row_start'], serial['grid']['row_end']
    band = [c for c in cells if start <= c['grid']['row_start'] < end]
    headers = {}
    for name, aliases in {**FIELD_ALIASES, **OPTIONAL_ALIASES}.items():
        found = [c for c in band if header_matches(name, c['raw_text'], aliases)]
        if len(found) == 1:
            headers[name] = found[0]
        elif len(found) > 1:
            issues.append('ambiguous_header:' + name)
    # Some forms put both labels in one header cell, and both values in one body cell.
    combined = [c for c in band if any(a in compact(c['raw_text']) for a in FIELD_ALIASES['manufacturer'])
                and any(a in compact(c['raw_text']) for a in FIELD_ALIASES['certificate_no'])]
    if len(combined) == 1 and not {'manufacturer', 'certificate_no'} & headers.keys():
        headers.update(manufacturer=combined[0], certificate_no=combined[0])
    optional_cert = form_type == 'entry_quantity_list'
    required = set(FIELD_ALIASES) - ({'certificate_no'} if optional_cert else set())
    issues += ['missing_header:' + name for name in sorted(required - headers.keys())]
    if issues:
        return records, blanks, issues, headers
    # An explicit unit-only subheading can supply the quantity column's unit.
    unit_components = None
    qh = headers['quantity']
    if quantity_header(qh['raw_text'])[1] is None:
        units = [c for c in band if columns(c) == columns(qh) and c['cell_id'] != qh['cell_id']
                 and re.fullmatch(r'[（(]?\s*(t|kg|吨|千克)\s*[)）]?', c['raw_text'].strip(), re.I)]
        if len(units) == 1:
            unit_components = [dict(cell_id=c['cell_id'], raw_text=c['raw_text'], grid=c['grid']) for c in (qh,units[0])]
            qh = deepcopy(qh)
            unit = re.sub(r'[()（）\s]', '', units[0]['raw_text'])
            qh['raw_text'] += '(' + unit + ')'
            headers['quantity'] = qh
    known = {h['cell_id'] for h in headers.values()}
    if unit_components:
        known.update(c['cell_id'] for c in unit_components)
    unknown = [c['cell_id'] for c in band if c['cell_id'] not in known and compact(c['raw_text']) not in ('备注', '检验项目', '检验结果')]
    width = max(c['grid']['col_end'] for c in cells)
    footers = [c for c in cells if c['grid']['row_start'] >= end and columns(c) == (0, width)
               and compact(c['raw_text']).startswith(('检验结论', '检查意见', '验收意见', '合计', '备注'))]
    stop = min((c['grid']['row_start'] for c in footers), default=max(c['grid']['row_end'] for c in cells))
    body = [c for c in cells if end <= c['grid']['row_start'] < stop]
    anchors = [c for c in body if columns(c) == columns(serial)]
    if not anchors:
        issues.append('no_product_rows')
    for c in body:
        if not any(a['grid']['row_start'] <= c['grid']['row_start'] < a['grid']['row_end'] for a in anchors):
            issues.append('unanchored_body_cell:' + c['cell_id'])
    for anchor in anchors:
        a = anchor['grid']
        related = [c for c in body if c['grid']['row_start'] < a['row_end'] and c['grid']['row_end'] > a['row_start']]
        serial_only = (bool(re.fullmatch(r"\d+",anchor["raw_text"].strip()))
                       and not any(c["raw_text"].strip() for c in related
                                   if c["cell_id"]!=anchor["cell_id"]))
        if not any(c['raw_text'].strip() for c in related) or serial_only:
            blanks.append(dict(anchor_cell_id=anchor['cell_id'], grid_rows=[a['row_start'], a['row_end']],
                               status='blank_candidate', requires_review=True,
                               row_no=anchor["raw_text"] if serial_only else None,
                               note='Only serial number observed; source image not confirmed blank.' if serial_only else
                                    'Model output empty; source image not confirmed blank.'))
            continue
        fields = {}
        for name, h in headers.items():
            peers = [v for v in headers.values() if columns(v) == columns(h)]
            candidates = [c for c in related if columns(c) == columns(h)]
            if len(peers) > 1 and len({v['cell_id'] for v in peers}) > 1:
                hg = h['grid']
                candidates = [c for c in candidates if
                    (c['grid']['row_start']-a['row_start'])/a['rowspan'] >= (hg['row_start']-start)/(end-start)
                    and (c['grid']['row_end']-a['row_start'])/a['rowspan'] <= (hg['row_end']-start)/(end-start)]
            chosen = candidates[0] if len(candidates) == 1 else None
            fields[name] = field_value(name, chosen, h)
            fields[name]['header_evidence'] = field_value('header', h)['evidence']
            if name == 'quantity' and unit_components:
                fields[name]['header_components'] = unit_components
                for evidence_name in ('header_evidence', 'unit_header_evidence'):
                    if fields[name].get(evidence_name):
                        fields[name][evidence_name]['header_components'] = unit_components
            if chosen is None:
                fields[name]['issues'].append('cell_missing_or_ambiguous')
                fields[name]['candidate_cell_ids'] = [c['cell_id'] for c in candidates]
        if 'certificate_no' in headers and columns(headers['manufacturer']) == columns(headers['certificate_no']):
            shared = [c for c in related if columns(c) == columns(headers['manufacturer'])
                      and c['grid']['row_start'] <= a['row_start'] and c['grid']['row_end'] >= a['row_end']]
            if len(shared) == 1:
                fields.update(split_shared_cell(shared[0]))
        if 'certificate_no' not in fields:
            fields['certificate_no'] = dict(value=None, raw_text='', lines=[], evidence=None,
                source_status='header_not_detected' if unknown else 'not_in_source',
                issues=['unrecognized_header_cannot_confirm_absence'] if unknown else [])
        fields = {k: logical_evidence(v) for k, v in fields.items()}
        problems = [f'{k}:{v}' for k, f in fields.items() for v in f['issues']]
        records.append(dict(record_id=anchor['cell_id'] + ':product', fields=fields,
            row_no=fields['row_no']['value'], entry_form_type=form_type,
            grid_rows=[a['row_start'], a['row_end']], issues=problems,
            status='needs_review' if problems else 'rule_pass_review_required', ready_for_search=False))
    counts = Counter(r['row_no'] for r in records if r['row_no'] is not None)
    for r in records:
        if counts[r['row_no']] > 1:
            r['issues'].append('duplicate_row_no')
            r['status'] = 'needs_review'
    return records, blanks, sorted(set(issues)), headers


def analyze_html(raw, *, form_type='inspection_record', page_no, pdf_sha, input_size):
    tables = parse_tables(raw)
    candidates = []
    for i, cells in enumerate(tables):
        # Ignore metadata tables only when they have neither serial nor several business headings.
        hits = sum(any(header_matches(name, c['raw_text'], aliases) for c in cells) for name, aliases in FIELD_ALIASES.items())
        if any(compact(c['raw_text']) == '序号' for c in cells) or hits >= 3:
            candidates.append((i, table_records(cells, form_type)))
    if not candidates:
        raise ValueError('no_entry_table_with_supported_headers')
    if len(candidates) != 1:
        raise ValueError('multiple_entry_tables_require_review')
    ti, (records, blanks, issues, headers) = candidates[0]
    for record in records:
        record['record_id'] = f'p{page_no:03d}:' + record['record_id']
        record.update(page_no=page_no, pdf_sha256=pdf_sha, extraction_backend='glm_ocr',
                      geometry_review_status='pixel_coordinates_unavailable',
                      evidence_warnings=['GLM-OCR has no measured cell confidence or pixel coordinates; review source page.'])
        fields = record['fields']
        for key in ('material_name', 'manufacturer', 'certificate_no', *OPTIONAL_ALIASES):
            record[key] = fields.get(key, {}).get('value')
        spec = parse_specification(fields['specification']['value'] or '')
        record['diameter_mm'] = spec['diameter_mm'] if spec else None
        record['grade'] = spec['grade'] if spec else None
        record['certificate_no_source_status'] = fields['certificate_no'].get('source_status', 'extracted' if record['certificate_no'] else 'not_detected')
        q = fields['quantity']['value']
        record['weight_t'] = format(Decimal(q['value']) / (1000 if q['unit'] == 'kg' else 1), 'f') if isinstance(q, dict) else None
        aliases = {k: k for k in fields}
        aliases['weight'] = aliases.pop('quantity')
        record['raw_cells'] = {k: fields[v]['raw_text'] for k, v in aliases.items()}
        record['effective_cells'] = dict(record['raw_cells'])
        # Existing ledger parser expects a quantity with unit. Preserve raw text separately;
        # append only a unit evidenced by the recognized column heading, never a guessed unit.
        if isinstance(q, dict) and fields['quantity'].get('unit_source') == 'column_header':
            record['effective_cells']['weight'] = q['value'] + q['unit']
        record['source_cells'], record['field_evidence'] = {}, {}
        for name, key in aliases.items():
            ev = fields[key].get('evidence')
            if not ev:
                continue
            record['source_cells'][name] = ev['cell_id']
            record['field_evidence'][name] = dict(deepcopy(ev), raw_text=fields[key]['raw_text'],
                polygon_page=None, source_pixel_size=list(input_size), render_dpi=300,
                model_raw_text=fields[key]['raw_text'], unit_source=fields[key].get('unit_source'),
                evidence_scope='page_and_html_cell', ocr_confidence=None, blocks=[],
                source_image=f'pages/page_{page_no:03d}/glm/input.png',
                raw_model_output=f'pages/page_{page_no:03d}/glm/raw.txt',
                unit_header_evidence=fields[key].get('unit_header_evidence'), issues=fields[key]['issues'])
        record['source'] = dict(page_no=page_no, pdf_sha256=pdf_sha,
            inference_result=f'pages/page_{page_no:03d}/glm/inference.json', table_index=ti)
        record['search_fields_status'] = record['status']
    for blank in blanks:
        blank.update(page_no=page_no, pdf_sha256=pdf_sha)
    needs = sum(r['status'] == 'needs_review' for r in records)
    return dict(schema_version='glm_entry_v1', production_ready=False, records=records,
        blank_candidates=blanks, table_issues=issues, logical_cells=tables[ti],
        table_selection={'table_index':ti, 'headers':{k:v['cell_id'] for k,v in headers.items()}},
        summary=dict(product_count=len(records), blank_candidate_count=len(blanks), needs_review_count=needs,
            business_issue_record_count=needs, table_issue_count=len(issues),
            status='needs_review' if issues or needs or not records else 'rule_pass_review_required',
            pixel_coordinates_available=False, measured_ocr_confidence_available=False))
