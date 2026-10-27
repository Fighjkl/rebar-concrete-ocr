"""Exact legacy certificate label locator, isolated from legacy matching."""
import re
from .attachments import normalize_identifier as norm

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
    return max(page.get('regions', []), key=lambda r: (len(r.get('blocks', [])), len(r.get('text', ''))))

def bbox(block):
    polygon = block.get('polygon_ocr') or block.get('polygon_page')
    xs, ys = zip(*polygon)
    return min(xs), min(ys), max(xs), max(ys)

def certificate_identifiers(index, allowed_pages):
    """Recall identifier values only beside explicit certificate number labels."""
    # Manufacturer forms use both Chinese labels and ``No.``/``No:``.
    # Chinese explicit labels remain preferred. Generic No labels require a
    # certificate-number format, excluding licence/template numbers.
    labels = re.compile(r'(?:质量证明书编号|质保书编号|质编号|证明书号|(?<![A-Z])NO)\s*[.:：]?\s*', re.I)
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
