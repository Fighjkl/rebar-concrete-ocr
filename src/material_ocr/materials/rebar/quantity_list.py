"""Read entry quantity lists within their physical header columns and rows.

Consumes saved full-page OCR; never guesses a missing model, unit or identifier.
"""
import re
from decimal import Decimal
from statistics import median
from material_ocr.ocr.layout import box

GRADE = r'(?:HRBF|HRB|HPB|RRB|CRB)\d{3,4}[EB]?'


def parse_specification(text):
    text = re.sub(r'\s+', '', str(text or '')).upper()
    grade = re.search(GRADE, text)
    diameter = re.search(r'[ΦФØ⌀∅](\d{1,2}(?:\.\d+)?)', text)
    if not diameter:
        diameter = re.search(r'(?<!\d)(\d{1,2}(?:\.\d+)?)MM', text)
    return (grade.group() if grade else None, diameter.group(1) if diameter else None)


def _header_token(text):
    return re.sub(r'[\s/／:：()（）\[\]]', '', str(text or '')).lower()


def _column_headers(blocks):
    aliases = dict(
        row_no=('序号',), material_name=('材料名称', '材料品名', '品名'),
        specification=('规格/型号', '规格型号', '规格', '规格品种'),
        grade=('型号', '牌号', '钢筋牌号', '材质', '钢种'),
        weight=('进场数量', '进场数量(t)', '进场数量(吨)', '进场数量(kg)',
                '进场数量(千克)', '进场数量(公斤)'),
        manufacturer=('生产厂家', '生产单位', '生产厂', '厂家'),
        entry_date=('进场日期',), usage_location=('使用部位',),
        certificate_no=('质量证明书编号', '质保证明书编号', '质证书编号', '质保书编号',
                        '质证编号', '质保编号', '质量证明书号', '质保证明书号',
                        '质量证明书', '质保证明书', '质保书'),
    )
    names = {_header_token(alias): key for key, values in aliases.items() for alias in values}

    def cy(block):
        a = box(block)
        return (a[1] + a[3]) / 2

    # Unknown headings still separate physical columns (e.g. 自检结果).
    # Restrict them to the same header line, rather than allowing the fields
    # we wish to extract to determine overly wide neighbouring columns.
    candidates = []
    for anchor in blocks:
        if names.get(_header_token(anchor['text'])) != 'row_no':
            continue
        height = max(1, box(anchor)[3] - box(anchor)[1])
        line = sorted((b for b in blocks if abs(cy(b) - cy(anchor)) <= height * .8),
                      key=lambda b: (box(b)[0] + box(b)[2]) / 2)
        fields = [names.get(_header_token(b['text'])) for b in line]
        if not {'row_no', 'weight', 'specification'} <= set(fields):
            continue
        if any(fields.count(key) > 1 for key in names.values()):
            continue
        candidates.append([(names.get(_header_token(b['text'])), b) for b in line])
    # Repeated table headings are ambiguous; do not merge their data rows.
    return candidates[0] if len(candidates) == 1 else []


def extract_quantity_lists(index, excluded_pages=()):
    output = []
    for page in index['pages']:
        if page['page_no'] in excluded_pages:
            continue
        # Primary full-page observations only: focused crops cannot create rows.
        regions = [r for r in page.get('regions', []) if r['region_id'].startswith(f"p{page['page_no']:03d}_full")
                   and not r.get('row_id') and not r.get('priority_field') and not r.get('physical_plate_id')]
        blocks = list({(b['text'],str(b.get('polygon_page'))):b for r in regions for b in r.get('blocks', []) if box(b)}.values())
        text = ''.join(b['text'] for b in blocks)
        if '进场数量清单' not in re.sub(r'\s+', '', text):
            continue
        columns = _column_headers(blocks)
        if not columns:
            continue
        headers = {key: block for key, block in columns if key}
        def cx(b):
            a=box(b);return (a[0]+a[2])/2
        def cy(b):
            a=box(b);return (a[1]+a[3])/2
        centers=[cx(block) for _, block in columns]
        boundaries=[float('-inf')]+[(a+b)/2 for a,b in zip(centers,centers[1:])]+[float('inf')]
        serial_idx=next(i for i,(key,_) in enumerate(columns) if key=='row_no')
        head_bottom=max(box(b)[3] for _,b in columns)
        rows=sorted([b for b in blocks if re.fullmatch(r'[1-9]\d{0,3}',b['text'].strip()) and box(b)[1]>head_bottom
                     and boundaries[serial_idx]<=cx(b)<boundaries[serial_idx+1]],key=cy)
        spacing=median([cy(b)-cy(a) for a,b in zip(rows,rows[1:])]) if len(rows)>1 else max(1,box(headers['row_no'])[3]-box(headers['row_no'])[1])*2
        for anchor in rows:
            raw,ev={},{}
            for i,(key,_) in enumerate(columns):
                if not key:
                    continue
                hits=sorted([b for b in blocks if abs(cy(b)-cy(anchor))<spacing*.43 and boundaries[i]<=cx(b)<boundaries[i+1]
                             and sum(box(b)[0] < center < box(b)[2] for center in centers) <= 1],key=cx)
                raw[key]=' '.join(b['text'] for b in hits)
                points=[p for b in hits for p in (b.get('polygon_page') or [])]
                polygon=None
                if points:
                    xs,ys=zip(*points);polygon=[[min(xs),min(ys)],[max(xs),min(ys)],[max(xs),max(ys)],[min(xs),max(ys)]]
                ev[key]=dict(page_no=page['page_no'],polygon_page=polygon,raw_text=raw[key],source_pixel_size=page.get('pixel_size'),
                             render_dpi=page.get('dpi',180),blocks=hits,method='quantity_list_same_physical_row')
            grade,diameter=parse_specification(raw.get('specification'))
            issues=[]
            model = re.sub(r'\s+', '', raw.get('grade', '')).upper()
            if model:
                if re.fullmatch(GRADE, model):
                    if grade and grade != model:
                        grade = None
                        issues.append('conflicting_grade_columns')
                    else:
                        grade = model
                else:
                    issues.append('model_or_grade_unreadable')
            # A plain number is a diameter only in its own explicit 规格
            # column. Mixed text never supplies a guessed leading number.
            plain_size = re.fullmatch(r'\d{1,2}(?:\.\d+)?', re.sub(r'\s+', '', raw.get('specification', '')))
            if not diameter and plain_size and Decimal('5') <= Decimal(plain_size.group()) <= Decimal('50'):
                diameter = plain_size.group()
            if raw.get('grade'):
                size_ev, grade_ev = ev['specification'], ev['grade']
                points = [p for item in (size_ev, grade_ev) for p in (item.get('polygon_page') or [])]
                combined = dict(size_ev, raw_text=' '.join((raw['grade'], raw['specification'])),
                                blocks=grade_ev['blocks']+size_ev['blocks'], method='quantity_list_same_physical_row_specification')
                if points:
                    xs,ys=zip(*points)
                    combined['polygon_page']=[[min(xs),min(ys)],[max(xs),min(ys)],[max(xs),max(ys)],[min(xs),max(ys)]]
                ev['specification'] = combined
            quantity=re.sub(r'\s+', '',raw.get('weight',''))
            valid=re.fullmatch(r'\d+(?:\.\d+)?',quantity)
            header_text = _header_token(headers['weight']['text'])
            unit = 'kg' if re.search(r'(?:kg|千克|公斤)$', header_text) else ('t' if re.search(r'(?:t|吨)$', header_text) else None)
            weight = None
            if valid and Decimal(quantity)>0 and unit:
                weight = format(Decimal(quantity) / (1000 if unit == 'kg' else 1), 'f')
            elif valid and Decimal(quantity)>0:
                issues.append('entry_quantity_unit_missing')
            number = raw.get('certificate_no', '').strip()
            certificate = number if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{2,}', number) and re.search(r'\d', number) else None
            if number and number not in ('-', '—', '/', '无', '未填', '不适用') and not certificate:
                issues.append('certificate_number_unreadable')
            if not grade or not diameter: issues.append('specification_or_grade_unreadable')
            if not weight: issues.append('entry_quantity_unreadable')
            for key,item in ev.items():
                if any(b.get('score') is None or b['score']<.90 for b in item['blocks']): issues.append('low_or_missing_ocr:'+key)
            if weight:raw['weight']=weight+'t' # Unit is supplied by this physical quantity header.
            output.append(dict(record_id=f"p{page['page_no']:03d}_list_r{anchor['text'].strip()}",page_no=page['page_no'],
                row_no=int(anchor['text']),pdf_sha256=index['pdf_sha256'],grade=grade,diameter_mm=diameter,weight_t=weight,
                material_name=raw.get('material_name'),manufacturer=raw.get('manufacturer'),certificate_no=certificate,
                entry_date=raw.get('entry_date'),usage_location=raw.get('usage_location'),raw_cells=raw,effective_cells=dict(raw),
                field_evidence=ev,issues=issues,extraction_backend='quantity_list_spatial_v1',ready_for_search=False))
    return output
