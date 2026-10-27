"""Conservative attachment layout recovery in upright OCR coordinates.

No field values are inferred from neighbouring pages or unrelated table rows.
"""
import math
import re
from statistics import median


def box(block):
    poly = block.get('polygon_ocr', block.get('polygon_page'))
    if not poly:
        return None
    try:
        xs, ys = zip(*poly)
        return min(xs), min(ys), max(xs), max(ys)
    except (TypeError, ValueError):
        return None


def orientation_score(blocks):
    weights = []
    for b in blocks:
        bounds = box(b)
        text = str(b.get('text', '')).strip()
        if not bounds or len(text) < 2:
            continue
        w, h = bounds[2]-bounds[0], bounds[3]-bounds[1]
        confidence = float(b.get('score') or 0)
        weights.append(confidence * min(len(text), 20) * (1 if w >= h else .1))
    return sum(weights)


def assess_image(image, blocks):
    import cv2
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    # Fixed thumbnail size makes the sharpness diagnostic comparable across DPI.
    scale = min(1., 1200 / max(gray.shape))
    gray = cv2.resize(gray, None, fx=scale, fy=scale)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    slopes = []
    for b in blocks:
        poly = b.get('polygon_ocr', b.get('polygon_page'))
        bounds = box(b)
        if poly and len(poly) >= 4 and bounds and bounds[2]-bounds[0] > 2*(bounds[3]-bounds[1]):
            dx, dy = poly[1][0]-poly[0][0], poly[1][1]-poly[0][1]
            if dx:
                angle = math.degrees(math.atan2(dy, dx))
                if abs(angle) < 15:
                    slopes.append(angle)
    skew = median(slopes) if slopes else None
    reasons = []
    if sharpness < 20:
        reasons.append('low_sharpness')
    if skew is not None and abs(skew) > 3:
        reasons.append('skew_requires_review')
    return {'sharpness_laplacian_1200': sharpness, 'skew_degrees': skew,
            'review_reasons': reasons, 'deskew_applied': False}


def split_entities(blocks):
    """Split vertically stacked forms only with explicit, different titles."""
    titles = {
        'delivery_note': ('送货单', '发货单', '供货单'),
        'nameplate': ('材料铭牌', '材料名牌', '铭牌', 'PIECES/WT'),
        'quality_certificate': ('质量证明书', 'QUALITYCERTIFICATE'),
    }
    anchors = []
    for b in blocks:
        bounds = box(b)
        text = re.sub(r'\s+', '', str(b.get('text', ''))).upper()
        if not bounds:
            continue
        for role, tokens in titles.items():
            if any(t in text for t in tokens):
                anchors.append((bounds[1], role)); break
    anchors.sort()
    delivery_y = min((y for y, role in anchors if role == 'delivery_note'), default=None)
    if delivery_y is not None and not any(role == 'nameplate' for _, role in anchors):
        above = [b for b in blocks if box(b) and box(b)[3] < delivery_y]
        upper_text = ''.join(str(b.get('text','')) for b in above)
        if (('轧制批号' in upper_text or '炉批号' in upper_text)
                and '牌号' in upper_text and '规格' in upper_text):
            anchors.append((min(box(b)[1] for b in above), 'nameplate'))
            anchors.sort()
    if any(role == 'delivery_note' for y, role in anchors) and not any(role == 'nameplate' for y, role in anchors):
        # Pasted steel tags often have no Chinese title. Require the
        # characteristic batch + grade + size label cluster, below delivery.
        # A common mixed scan has the delivery note in the upper part and a
        # pasted nameplate in the lower part without a Chinese title. Use the
        # page geometry as a fallback, then require the characteristic label
        # cluster so ordinary delivery rows are never reclassified.
        all_boxes = [box(b) for b in blocks if box(b)]
        page_top = min((b[1] for b in all_boxes), default=0)
        page_bottom = max((b[3] for b in all_boxes), default=0)
        lower_threshold = page_top + (page_bottom - page_top) * 0.42
        for b in blocks:
            bounds = box(b)
            token = re.sub(r'[^A-Z0-9]', '', str(b.get('text','')).upper())
            cn_token = re.sub(r'\s+', '', str(b.get('text','')))
            is_batch = token in {'BATCHNO','HEATNO'} or cn_token in {'批号','炉批号','炉号','批次号'}
            if bounds and bounds[1] >= lower_threshold and is_batch:
                h = max(1, bounds[3]-bounds[1])
                nearby = ' '.join(str(x.get('text','')).upper() for x in blocks
                                 if box(x) and box(x)[1] >= lower_threshold and
                                 abs(box(x)[1]-bounds[1]) < h*14)
                nearby_cn = ''.join(str(x.get('text','')) for x in blocks
                                     if box(x) and box(x)[1] >= lower_threshold and
                                     abs(box(x)[1]-bounds[1]) < h*14)
                if (('GRADE' in nearby and 'SIZE' in nearby) or
                    ('牌号' in nearby_cn and '规格' in nearby_cn)):
                    anchors.append((bounds[1]-h*7,'nameplate'))
                    break
        anchors.sort()
    distinct = []
    for y, role in anchors:
        if not distinct or role != distinct[-1][1]:
            distinct.append((y, role))
    if len({r for _, r in distinct}) < 2:
        return []
    output = []
    for i, (y, role) in enumerate(distinct):
        lower = -float('inf') if i == 0 else y
        upper = distinct[i+1][0] if i+1 < len(distinct) else float('inf')
        selected = [b for b in blocks if box(b) and lower <= (box(b)[1]+box(b)[3])/2 < upper]
        if selected:
            output.append((role, selected))
    return output


def table_rows(blocks, aliases):
    """Bind data to exclusive header columns; unit comes from its own header.

    Supports single-line column headings and OCR cells. Merged cells, multi-line
    headers and multiple tables remain review evidence rather than guessed rows.
    """
    def compact(s):
        return re.sub(r'\s+', '', s).upper()
    items = [(b, box(b)) for b in blocks if box(b)]
    header_candidates = []
    for b, bounds in items:
        text = compact(str(b.get('text', '')))
        if len(text) > 30:
            continue
        matches = [(f, a) for f, aa in aliases.items() for a in aa if compact(a) in text]
        if re.search(r'件数|支数|捆数|PIECES', text):
            matches.append(('piece_count', text))
        if re.search(r'长度|定尺长度', text):
            matches.append(('length_m', text))
        if re.search(r'单件|单重|单支|UNITWEIGHT', text):
            matches.append(('unit_weight', text))
        if not matches:
            continue
        field, alias = max(matches, key=lambda x: len(compact(x[1])))
        if re.search(r'单件|单重|件重|单支|UNITWEIGHT', text):
            field = 'unit_weight'
        elif re.search(r'件数|支数|捆数|PIECES', text) and not re.search(r'重量|WT', text):
            field = 'piece_count'
        header_candidates.append((b, bounds, field))
    best = []
    for _, bounds, _ in header_candidates:
        height = max(1, bounds[3]-bounds[1]); yc = (bounds[1]+bounds[3])/2
        group = [x for x in header_candidates if abs((x[1][1]+x[1][3])/2-yc) <= height]
        if len({x[2] for x in group}) > len({x[2] for x in best}):
            best = group
    if len({x[2] for x in best}) < 2 or not any(x[2] in {'diameter_mm','heat_lot_no'} for x in best):
        return []
    best.sort(key=lambda x: (x[1][0]+x[1][2])/2)
    centers = [(x[1][0]+x[1][2])/2 for x in best]
    top = max(x[1][3] for x in best)
    height = median(x[1][3]-x[1][1] for x in best)
    rows = []
    for b, bounds in sorted(items, key=lambda x: (x[1][1]+x[1][3])/2):
        if (bounds[1]+bounds[3])/2 < top or bounds[1]-top > height*35:
            continue
        text = compact(str(b.get('text','')))
        if any(t in text for t in ('合计','总计','TOTAL','备注','签收')):
            break
        if not re.search(r'\d', text):
            continue
        xc, yc = (bounds[0]+bounds[2])/2, (bounds[1]+bounds[3])/2
        column = min(range(len(centers)), key=lambda i: abs(centers[i]-xc))
        # A block spanning neighbouring column centers is unsafe to assign.
        if sum(bounds[0] < c < bounds[2] for c in centers) > 1:
            continue
        if abs(xc-centers[column]) > max(height*4, (best[column][1][2]-best[column][1][0])*1.5):
            continue
        if not rows or abs(rows[-1]['yc']-yc) > max(2, height*.7):
            rows.append({'yc':yc,'cells':[]})
        rows[-1]['cells'].append((best[column], b))
    return [r for r in rows if len({h[2] for h,b in r['cells']}) >= 2]


def priority_field_templates(blocks, role, aliases, width, height):
    """Plan label/value crops and columns with explicit upright row windows."""
    def compact(text):
        return re.sub(r"[^A-Z0-9\u4e00-\u9fff]", "", str(text).upper())
    def field_for(text):
        token=compact(text)
        matches=[]
        for field in ('certificate_no','heat_lot_no','quantity_t','weight_t','diameter_mm'):
            for alias in aliases.get(field, ()):
                head=compact(alias)
                if not head or head not in token:
                    continue
                remainder=token.replace(head, '', 1)
                # Prose, totals-in-words, phone numbers and addresses are not
                # column labels. Inline field:value blocks remain eligible.
                pure = remainder in {'','KG','T','吨','MM','千克','公斤','毫米'}
                inline = bool(re.search(re.escape(alias)+r'\s*[:：]\s*[A-Za-z0-9]',text,re.I))
                if pure or inline:
                    matches.append((len(head),field))
        if not matches:
            return None
        field=max(matches)[1]
        return 'quantity_t' if field=='weight_t' and role=='delivery_note' else field
    def unit_label(label):
        text=label['text']
        lb=box(label)
        if re.search(r'吨|千克|公斤|KG|\(T\)',text,re.I):
            return text
        for other in blocks:
            ob=box(other)
            if ob and re.fullmatch(r'[（(]?(?:吨|kg|千克|公斤|t)[）)]?',str(other.get('text','')).strip(),re.I):
                h=max(2,lb[3]-lb[1])
                if abs((ob[0]+ob[2]-lb[0]-lb[2])/2)<h*.8 and abs(ob[1]-lb[1])<h*3:
                    return text+' '+other['text']
        return text
    def template(name, field, bounds, label, windows=None):
        x0,y0,x1,y1 = bounds
        x0,y0,x1,y1 = max(0,x0),max(0,y0),min(width,x1),min(height,y1)
        poly = [[int(x0),int(y0)],[int(x1),int(y0)],[int(x1),int(y1)],[int(x0),int(y1)]]
        return {'name':name, 'document_type':role, 'priority_field':field,
                'x0':x0/width,'y0':y0/height,'x1':x1/width,'y1':y1/height,'upscale':3.0,
                'crop_polygon_ocr':poly, 'crop_polygon_page':poly,
                'label_text':unit_label(label), 'label_polygon_ocr':label.get('polygon_ocr',label.get('polygon_page')),
                'row_windows':windows or []}
    items = [(b,box(b)) for b in blocks if box(b)]
    rows = table_rows(blocks, aliases) if role != 'nameplate' else []
    columns = {}
    for i,row in enumerate(rows):
        for header,value in row['cells']:
            label, lb, field = header
            field = 'quantity_t' if field == 'weight_t' and role == 'delivery_note' else field
            if field not in {'certificate_no','heat_lot_no','quantity_t','weight_t','diameter_mm'}:
                continue
            vb=box(value)
            column_half=max((lb[2]-lb[0])/2+8,(lb[3]-lb[1])*.9)
            if abs((vb[0]+vb[2]-lb[0]-lb[2])/2)>column_half:
                continue
            key = (field, tuple(lb))
            columns.setdefault(key, {'label':label,'lb':lb,'values':[]})['values'].append((i+1,value))
    result = []
    covered_labels = set()
    for (field, _), column in columns.items():
        label, lb = column['label'],column['lb']; h=max(2,lb[3]-lb[1])
        vbs = [box(b) for _,b in column['values']]
        x0 = min([lb[0]]+[b[0] for b in vbs])-h*.4
        x1 = max([lb[2]]+[b[2] for b in vbs])+h*.4
        y0,y1 = lb[1]-h*.3,max(b[3] for b in vbs)+h*.3
        windows=[]
        seen_rows=set()
        for row_index,b in column['values']:
            if row_index in seen_rows:
                continue
            seen_rows.add(row_index)
            vb = box(b)
            windows.append({'physical_row_index':row_index,
                            'polygon_ocr':[[int(x0),int(vb[1]-h*.15)],[int(x1),int(vb[1]-h*.15)],
                                           [int(x1),int(vb[3]+h*.15)],[int(x0),int(vb[3]+h*.15)]]})
        result.append(template(f'column_{field}_{len(result)+1}',field,(x0,y0,x1,y1),label,windows))
        covered_labels.add(id(label))
    for label,lb in items:
        field = field_for(label.get('text',''))
        if not field or id(label) in covered_labels:
            continue
        h=max(2,lb[3]-lb[1]); yc=(lb[1]+lb[3])/2; xc=(lb[0]+lb[2])/2
        # Inline identifiers already have the label and value in one block.
        inline = bool(re.search(r"[:：]\s*[A-Za-z0-9]", label['text']))
        candidates=[]
        for value,vb in items:
            if value is label or field_for(value.get('text','')):
                continue
            vyc=(vb[1]+vb[3])/2; vxc=(vb[0]+vb[2])/2
            if not re.search(r'\d',str(value.get('text',''))):
                continue
            right=abs(vyc-yc)<=h*.8 and -h*.2 <= vb[0]-lb[2] <= h*10
            below=0 <= vb[1]-lb[3] <= h*3 and abs(vxc-xc)<=max(h*2,(lb[2]-lb[0])*.7)
            if right or below:
                distance = max(0,vb[0]-lb[2]) if right else vb[1]-lb[3]+h
                candidates.append((distance,vb))
        if inline:
            vb=lb
        elif candidates:
            vb=min(candidates,key=lambda v:v[0])[1]
        else:
            continue
        bounds=(min(lb[0],vb[0])-h*.5,min(lb[1],vb[1])-h*.4,
                max(lb[2],vb[2])+h*.5,max(lb[3],vb[3])+h*.4)
        result.append(template(f'field_{field}_{len(result)+1}',field,bounds,label))
    return result
