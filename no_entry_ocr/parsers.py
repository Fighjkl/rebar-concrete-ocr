"""Additional layouts for the isolated experiment; OCR text is never rewritten."""
from copy import deepcopy
from datetime import date
import re
from statistics import median

from material_ocr.materials.rebar.paddle_spatial_resources import converted_blocks, parsing_view, evidence
from material_ocr.materials.rebar.multiform_resources import center, height

GRADE = r'(?:HRBF|HRB|HPB|RRB|CRB)\d{3,4}[EBF]?'


def blocks(cp):
    return converted_blocks(cp, parsing_view(cp))


def ev(cp, bs, role):
    result = evidence(cp, blocks=bs, role=role, source_image=cp.get('source_image'))
    result['boxes'] = [dict(text=b['text'], polygon_page=b['polygon_page'], score=b.get('score')) for b in bs]
    result['method'] = 'no_entry_located_text'
    return result


def compact(value):
    return re.sub(r'\s+', '', str(value or '')).upper()


def companies(bs):
    found = []
    for b in bs:
        # Whitespace often separates a logo from the registered company name.
        for m in re.finditer(r'[\u4e00-\u9fff]{4,}(?:有限责任公司|有限公司)', b['text']):
            value = re.sub(r'^(?:生产厂家|生产单位|供应商|供应单位|供货单位)[:：]?', '', m[0])
            found.append((value, b))
    return found


def submission(cp):
    bs = blocks(cp)
    raw = cp['raw_text']
    result = {}
    def add(key, value, selected):
        result[key] = dict(value=value, evidence=[ev(cp, selected, 'submission')])
    for b in bs:
        t = compact(b['text'])
        m = re.search(r'(?:文件编号|报审编号|编号)[:：]([A-Z0-9][A-Z0-9.\-/]*)', t)
        if m: add('文件编号', m[1], [b])
    # GD-C1-347 is a standard form code, not this submission's identifier.
    m = re.search(r'我方于\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', raw)
    if m:
        try:
            value = date(*map(int, m.groups())).isoformat()
            selected = [b for b in bs if '我方于' in compact(b['text']) or all(x in b['text'] for x in m.groups())]
            add('供货日期与工程材料报审签认日期', value, selected)
        except ValueError: pass
    m = re.search(r'拟用于下述部位[：:]\s*([^\n]+)', raw)
    if m:
        value = m[1].strip().split('请予')[0].strip()
        add('使用部位', value, [b for b in bs if value in b['text'] or '拟用于' in b['text']])
    m = re.search(r'进场的\s*(.{1,30}?)\s*数量', raw)
    if m: add('材料名称', compact(m[1]), [b for b in bs if '进场的' in b['text']])
    return result


def table_columns(bs):
    """Use located column headings and compensate for page skew."""
    headers = {}
    for b in bs:
        t = compact(b['text'])
        if re.fullmatch(r'(?:规格|规格型号)(?:[（(]MM[）)])?', t): headers.setdefault('diameter', b)
        if re.fullmatch(r'(?:炉批号|炉号|批号)', t): headers.setdefault('heat', b)
        if re.fullmatch(r'(?:重量|理计重量|理论重量)(?:[（(](?:T|吨)[）)])?', t):
            
            previous=headers.get('weight')
            # A visible unit separates batch weight from mechanical weight deviation.
            def unit_score(item):
                if re.search(r'[（(](?:T|吨)[）)]',compact(item['text'])):return 2
                return int(any(compact(v['text']) in ('(T)','（T）','T','吨') and abs(center(v)[0]-center(item)[0])<height(item)*2 and 0<center(v)[1]-center(item)[1]<height(item)*3 for v in bs))
            if previous is None or unit_score(b)>unit_score(previous):headers['weight']=b
    return headers


def column_rows(cp, anchors, anchor_header, weight_header, bs):
    """Only accept a unique numeric cell at a located weight column, same row."""
    ax, ay = center(anchor_header); wx, wy = center(weight_header)
    slope = (wy-ay)/(wx-ax) if abs(wx-ax)>50 else 0
    if abs(slope)>.2: return []
    projected = lambda b: center(b)[1]-slope*center(b)[0]
    result=[]
    for a in anchors:
        if projected(a) <= projected(anchor_header)+height(anchor_header)*.6: continue
        values=[b for b in bs if abs(center(b)[0]-wx)<max(55,height(weight_header)*1.5)
                and abs(projected(b)-projected(a))<max(height(a),height(b))*.7
                and re.fullmatch(r'\d+\.\d{1,4}',compact(b['text']))]
        result.append((a,values[0] if len(values)==1 else None))
    return result


def certificate_rows(cp, certificate):
    bs=blocks(cp); h=table_columns(bs)
    if not {'heat','diameter'}<=h.keys(): return []
    heats=set(certificate.get('heat_lots',[]))
    anchors=[b for b in bs if compact(b['text']) in heats]
    hx,hy=center(h['heat']); dx,dy=center(h['diameter'])
    slope=(dy-hy)/(dx-hx) if abs(dx-hx)>50 else 0
    if abs(slope)>.2:return []
    y=lambda b:center(b)[1]-slope*center(b)[0]
    weights={id(a):w for a,w in column_rows(cp,anchors,h['heat'],h['weight'],bs)} if h.get('weight') else {}
    out=[]
    for a in anchors:
        ds=[b for b in bs if abs(center(b)[0]-dx)<max(35,height(h['diameter']))
            and abs(y(b)-y(a))<max(height(b),height(a))*.65
            and re.fullmatch(r'\d{1,2}',compact(b['text'])) and 4<=int(compact(b['text']))<=50]
        w=weights.get(id(a))
        out.append(dict(heat_lot=compact(a['text']),diameter_mm=compact(ds[0]['text']) if len(ds)==1 else '',
            quantity_t=compact(w['text']) if w else '',
            evidence=ev(cp,[a]+ds+([w] if w else []),'certificate')))
    return out


def weigh_note(cp):
    """A weighing slip is a delivery attachment, with explicit quantity basis."""
    bs=blocks(cp); raw='\n'.join(b['text'] for b in bs)
    if not re.search(r'称重单|过磅单|磅码单',raw):return None
    h=table_columns(bs)
    if not {'diameter','weight'}<=h.keys():return None
    dx,dy=center(h['diameter'])
    anchors=[b for b in bs if abs(center(b)[0]-dx)<max(65,height(h['diameter'])*1.5)
             and re.search(r'\d',b['text']) and center(b)[1]>dy+height(h['diameter'])]
    rows=[]
    for a,w in column_rows(cp,anchors,h['diameter'],h['weight'],bs):
        t=compact(a['text']);m=re.fullmatch(r'[ΦφØ]?(\d{1,2})(?:MM|毫米)',t)
        diameter=m[1] if m and 4<=int(m[1])<=50 else ''
        if not w:continue
        rows.append(dict(grade='',diameter_mm=diameter,quantity_t=compact(w['text']),
            specification_raw=a['text'],logical_row=len(rows)+1,
            issues=[] if diameter else ['称重单规格 OCR 不完整，不能直接按规格匹配'],
            evidence=ev(cp,[a,w,h['weight']],'delivery')))
    nums=[]
    for b in bs:
        m=re.search(r'(?:NO[.:：]?|单号[:：])\s*([A-Z0-9][A-Z0-9-]{4,})',compact(b['text']))
        if m:nums.append(dict(value=m[1],evidence=ev(cp,[b],'delivery')))
    names=[(name,b) for name,b in companies(bs) if center(b)[1]<dy]
    names=list({name:(name,b) for name,b in names}.values())
    return dict(candidate_id=f'no-entry-weigh:p{cp["page_no"]:03d}',page_no=cp['page_no'],
        delivery_no=nums[0]['value'] if len({x['value'] for x in nums})==1 else '',
        delivery_number_observations=nums,supplier=names[0][0] if len(names)==1 else '',
        supplier_heading=dict(evidence=[ev(cp,[b],'delivery') for _,b in names]),
        rows=rows,quantity_basis='理计重量（吨）',
        issues=['称重单理计重量用于数量候选；净重与理计重量含义不同，须确认验收口径'],
        evidence=ev(cp,bs,'delivery'))


def adapt(resources, cps):
    result=deepcopy(resources);by_page={c['page_no']:c for c in cps}
    result['excluded_experiment_pages']=[cp['page_no'] for cp in cps if is_lab_report(cp)]
    for c in result.get('certificates',[]):
        if c['page_no'] in by_page:c['batch_rows']=certificate_rows(by_page[c['page_no']],c)
    delivery_pages={d['page_no'] for d in result.get('deliveries',[])}
    for cp in cps:
        if cp['page_no'] not in delivery_pages:
            d=weigh_note(cp) or dispatch_weigh_note(cp)
            if d:result.setdefault('deliveries',[]).append(d)
    plate_pages={p['page_no'] for p in result.get('nameplates',[])}
    for cp in cps:
        if cp['page_no'] not in plate_pages:
            p=isolated_plate(cp)
            if p:result.setdefault('nameplates',[]).append(p)
    return result


def is_lab_report(cp):
    text=compact(cp.get('raw_text',''))
    return ('样品编号' in text and '检测结果' in text and
            ('委托编号' in text or '报告编号' in text) and
            ('力学性能' in text or '检测依据' in text))


def isolated_plate(cp):
    """Recover an incomplete single-card index, even if its diameter is unreadable."""
    bs=blocks(cp)
    labels=[b for b in bs if '合格证粘贴栏' in b['text']]
    if not labels:return None
    y=min(center(b)[1] for b in labels)
    card=[b for b in bs if center(b)[1]>y]
    text='\n'.join(b['text'] for b in card)
    grades=set(re.findall(GRADE,compact(text)))
    heats=set(re.findall(r'(?:炉批号|炉号|批号)[:：]([A-Z0-9][A-Z0-9-]{4,})',compact(text)))
    # The trailing bundle label is Chinese, so cannot be absorbed into this key.
    if len(heats)!=1 or len(grades)>1:return None
    diameters=set()
    for b in card:
        m=re.search(r'规格[:：].{0,6}?(\d{1,2})(?:MM|毫米)',compact(b['text']))
        if m and 4<=int(m[1])<=50:diameters.add(m[1])
    return dict(candidate_id=f'no-entry-plate:p{cp["page_no"]:03d}',page_no=cp['page_no'],
        grade=next(iter(grades)) if grades else '',diameter_mm=next(iter(diameters)) if len(diameters)==1 else '',
        heat_lots=list(heats),manufacturer='',issues=['独立支路单铭牌布局恢复；缺失规格仅允许同炉批号质保书补全'],
        spatial_unresolved=False,evidence=ev(cp,card,'nameplate'))


def dispatch_weigh_note(cp):
    bs=blocks(cp);raw='\n'.join(b['text'] for b in bs)
    if not re.search(r'出库(?:过磅|理计)单',raw):return None
    theoretical='出库理计单' in raw
    headers=[b for b in bs if compact(b['text'])==('吨位' if theoretical else '净重')]
    title=[b for b in bs if compact(b['text'])=='品名']
    if len(headers)!=1 or len(title)!=1:return None
    anchors=[b for b in bs if re.search(GRADE,compact(b['text']))]
    rows=[]
    for a,w in column_rows(cp,anchors,title[0],headers[0],bs):
        t=compact(a['text']);g=re.search(GRADE,t)
        m=re.search(GRADE+r'\*?[Φφ中Ø]?(\d{1,2})\*?',t)
        if not g or not m:continue
        rows.append(dict(grade=g[0],diameter_mm=m[1],quantity_t=compact(w['text']) if w else '',
            specification_raw=a['text'],evidence=ev(cp,[a]+([w] if w else []),'delivery'),
            issues=[] if theoretical else ['过磅单净重栏未标明单位，数量仅作候选']))
    nums=[]
    for b in bs:
        m=re.search(r'单号[:：]([A-Z0-9-]{5,})',compact(b['text']))
        if m:nums.append(dict(value=m[1],evidence=ev(cp,[b],'delivery')))
    return dict(candidate_id=f'no-entry-dispatch:p{cp["page_no"]:03d}',page_no=cp['page_no'],
        delivery_no=nums[0]['value'] if len({x['value'] for x in nums})==1 else '',supplier='',
        delivery_number_observations=nums,rows=rows,evidence=ev(cp,bs,'delivery'),
        issues=['仅识别到品牌或发货章，供应单位全称未明确，不能用生产单位替代'])
