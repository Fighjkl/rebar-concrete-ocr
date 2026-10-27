"""Whole-page field anchors and conservative global pairing. Standard library only.

Coordinates are OCR quadrilaterals, never fixed plate rectangles. All matches are
review drafts. No character correction or batch-number propagation is performed.
"""
import math
import re
import unicodedata
from statistics import median

VERSION = '0.3.0'
ANCHORS = {
    'spec': re.compile(r'规格型号|规格|直径|[S$]IZE(?=[:：ΦØ∅\d]|$)'),
    'batch': re.compile(r'炉[/（(]?批[）)]?号|炉罐号|轧制号|炉号|批次号|批号|HEAT(?:/LOT)?(?:NO\.?|NUMBER)|BATCH(?:NO\.?|NUMBER)|LOTNO\.?'),
}
STOP = re.compile(r'长度|重量|支数|数量|生产日期|日期|牌号|规格|批号|炉号|炉批号|轧制号|检验员|LENGTH|WEIGHT|PIECES')
COMMON_DIAMETERS = {6, 6.5, 8, 10, 12, 14, 16, 18, 20, 22, 25, 28, 32, 36, 40, 50}


def clean(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(text))).upper()


def parse_value(kind, raw):
    """Only parse prefixes of anchored values; never search arbitrary numbers."""
    text = clean(raw).lstrip(':：=')
    text = STOP.split(text, maxsplit=1)[0]
    if kind == 'batch':
        m = re.match(r'([A-Z0-9][A-Z0-9/\-]{3,31})(?![A-Z0-9])', text)
        if not m:
            return None
        val = m.group(1)
        if len(re.findall(r'\d', val)) < 4 or val.startswith(('HRB', 'HPB', 'GB')):
            return None
        if re.fullmatch(r'20\d{2}[-/]\d{1,2}[-/]\d{1,2}', val):
            return None
        if re.fullmatch(r'\d+(?:KG|MM|CM|TON|M|T)', val):
            return None
        if text[m.end():] and not text[m.end():].startswith(('支', '件', '捆')):
            return None
        return {'number': val}
    m = re.match(r'(?:Φ|Ф|Ø|∅|⌀|D)?(\d{1,2}(?:\.\d+)?)(MM|毫米)?(?=$|[X×*乘/:：])', text)
    if not m:
        return None
    diameter = float(m.group(1))
    if not 4 <= diameter <= 80:
        return None
    result = {'diameter_mm': diameter, 'length_mm': None}
    rest = text[m.end():]
    length = re.match(r'[X×*乘](\d+(?:\.\d+)?)(MM|毫米|M|米)?$', rest)
    if rest and not length:
        return None
    if length:
        result['length_raw'] = length.group(0)[1:]
        if length.group(2):
            result['length_mm'] = float(length.group(1)) * (1000 if length.group(2) in ('M', '米') else 1)
    return result


def geometry(poly):
    if len(poly) != 4 or any(len(p) != 2 for p in poly):
        raise ValueError('Expected ordered quadrilateral with four points')
    p = [[float(v) for v in q] for q in poly]
    if any(not math.isfinite(v) for q in p for v in q):
        raise ValueError('Non-finite OCR coordinates')
    dx, dy = p[1][0] - p[0][0], p[1][1] - p[0][1]
    width = math.hypot(dx, dy)
    height = (math.dist(p[0], p[3]) + math.dist(p[1], p[2])) / 2
    if width < .1 or height < .1:
        raise ValueError('Degenerate OCR polygon')
    u = (dx / width, dy / width)
    return {'center': [sum(q[d] for q in p) / 4 for d in (0, 1)],
            'u': u, 'v': (-u[1], u[0]), 'height': height, 'width': width}


def local(point, base):
    d = [point[i] - base['center'][i] for i in (0, 1)]
    return sum(d[i] * base['u'][i] for i in (0, 1)), sum(d[i] * base['v'][i] for i in (0, 1))


def angle_gap(a, b):
    # Directed reading orientation, important when merging rotated OCR passes.
    return math.degrees(math.acos(max(-1, min(1, sum(a['u'][i] * b['u'][i] for i in (0, 1))))))


def prepare_blocks(blocks):
    prepared = []
    seen = set()
    for i, b in enumerate(blocks):
        b = dict(b)
        b['id'] = str(b.get('id', f'b{i:04d}'))
        if b['id'] in seen:
            raise ValueError('Duplicate OCR block ID')
        seen.add(b['id'])
        b['text'] = str(b['text'])
        b['normalized'] = clean(b['text'])
        b['g'] = geometry(b['polygon'])
        score = b.get('score')
        if score is not None and (not math.isfinite(float(score)) or not 0 <= float(score) <= 1):
            raise ValueError('OCR score must be null or in [0,1]')
        prepared.append(b)
    return prepared


def union_polygon(members, base):
    points=[local(p,base) for b in members for p in b['polygon']]
    xs,ys=zip(*points);lo,hi=min(xs),max(xs);top,bottom=min(ys),max(ys)
    return [[base['center'][i]+x*base['u'][i]+y*base['v'][i] for i in (0,1)]
            for x,y in [(lo,top),(hi,top),(hi,bottom),(lo,bottom)]]


def reconstruct_labels(blocks):
    """Join adjacent OCR fragments of a field name, not plate regions or arbitrary text."""
    additions=[]
    suffix={'规':re.compile(r'^格'), '炉':re.compile(r'^(?:号|批号|罐号)'),
            '批':re.compile(r'^号'), '轧制':re.compile(r'^号')}
    for a in blocks:
        pattern=suffix.get(a['normalized'])
        if pattern is None:continue
        candidates=[]
        for b in blocks:
            if a['id']==b['id'] or not pattern.search(b['normalized']) or angle_gap(a['g'],b['g'])>18:continue
            h=max(a['g']['height'],b['g']['height']);x,y=local(b['g']['center'],a['g'])
            gap=x-(a['g']['width']+b['g']['width'])/2
            if x>0 and -.2*h<=gap<=2*h and abs(y)<.65*h:candidates.append((gap,b))
        if not candidates:continue
        b=min(candidates,key=lambda c:c[0])[1]
        poly=union_polygon([a,b],a['g']);scores=[q.get('score') for q in (a,b)]
        additions.append({'id':f'joined_{a["id"]}_{b["id"]}', 'text':a['text']+b['text'],
                          'normalized':a['normalized']+b['normalized'],'polygon':poly,'g':geometry(poly),
                          'score':min(scores) if all(s is not None for s in scores) else None,
                          'rotation':a.get('rotation',0),'source_block_ids':[a['id'],b['id']],
                          'reconstructed_label':True})
    return blocks+additions


def source_ids(block):
    return block.get('source_block_ids',[block['id']])


def spec_barrier(anchor, candidate, blocks):
    """Do not jump over a length label or multiplication separator to a bare length."""
    g=anchor['g'];cx,cy=local(candidate['g']['center'],g)
    for b in blocks:
        if b['id'] in source_ids(anchor)+source_ids(candidate)+[anchor['id'],candidate['id']]:continue
        if angle_gap(g,b['g'])>18:continue
        x,y=local(b['g']['center'],g);h=max(g['height'],candidate['g']['height'],b['g']['height'])
        if 0<x<cx and abs(y-cy)<.7*h and (re.search(r'长度|重量|支数|数量|日期',b['normalized']) or
                                            re.search(r'[X×*]$',b['normalized'])):
            return True
    return False


def spec_continuations(anchor, initial, blocks, raw):
    """Grow only adjacent pieces of one size expression; preserve each source token."""
    line=list(initial);current=line[-1] if line else anchor
    excluded=set(source_ids(anchor)+[anchor['id']]+[b['id'] for b in line])
    options=[]
    for _ in range(5):
        choices=[];h=anchor['g']['height']
        for b in blocks:
            if b['id'] in excluded or b.get('reconstructed_label') or angle_gap(anchor['g'],b['g'])>18:continue
            x,y=local(b['g']['center'],current['g']);gap=x-(current['g']['width']+b['g']['width'])/2
            if x>0 and -.2*h<=gap<=2.2*h and abs(y)<.6*max(h,b['g']['height']):choices.append((x,b))
        if not choices:break
        current=min(choices,key=lambda c:c[0])[1]
        if STOP.search(current['normalized']) or any(p.search(current['normalized']) for p in ANCHORS.values()):break
        if not re.fullmatch(r'[0-9.ΦФØ∅⌀X×*MM毫米米]+',current['normalized']):break
        line.append(current);excluded.add(current['id']);raw+=current['normalized']
        value=parse_value('spec',raw)
        if value:options.append((raw,line[:]))
    return options


def minimum_score(blocks):
    """Unknown scores stay unknown, even when other contributing blocks scored well."""
    scores = [b.get('score') for b in blocks]
    return min(scores) if scores and all(s is not None for s in scores) else None


def _evidence(anchor, members, kind, raw, cost, value_in_anchor=False):
    value = parse_value(kind, raw)
    if value is None:
        return None
    value_members = ([anchor] if value_in_anchor or not members else []) + members
    return {'value': value, 'raw': raw, 'cost': round(cost, 4),
            'block_ids': list(dict.fromkeys(i for b in [anchor] + members for i in source_ids(b))),
            'value_polygon': members[0]['polygon'] if members else anchor['polygon'],
            'ocr_min_score': minimum_score([anchor] + members),
            'value_ocr_score': minimum_score(value_members),
            'value_source_block_ids': list(dict.fromkeys(i for b in value_members for i in source_ids(b))),
            'value_score_scope': 'ocr_blocks_not_individual_characters'}


def length_candidates(anchor, blocks):
    """Explicit same-row length with explicit unit, including separate OCR tokens."""
    result=[];g=anchor['g']
    for label in blocks:
        hit=re.search(r'长度[:：]?',label['normalized'])
        if not hit:continue
        x,y=local(label['g']['center'],g)
        if label['id']!=anchor['id'] and not (angle_gap(g,label['g'])<18 and abs(y)<.7*g['height'] and 0<x<18*g['height']):
            continue
        tail=label['normalized'][hit.end():]
        options=[(tail,[label])]
        for b in blocks:
            if b['id']==label['id'] or angle_gap(label['g'],b['g'])>18:continue
            xx,yy=local(b['g']['center'],label['g']);h=max(label['g']['height'],b['g']['height'])
            gap=xx-(label['g']['width']+b['g']['width'])/2
            if xx<=0 or not -.5*h<=gap<=4*h or abs(yy)>.6*h:continue
            options.append((b['normalized'],[label,b]))
            if re.fullmatch(r'\d+(?:\.\d+)?',b['normalized']):
                for unit in blocks:
                    if unit['normalized'] not in ('M','MM','米','毫米'):continue
                    ux,uy=local(unit['g']['center'],b['g'])
                    ugap=ux-(b['g']['width']+unit['g']['width'])/2
                    if ux>0 and -.2*h<=ugap<=2*h and abs(uy)<.6*h:
                        options.append((b['normalized']+unit['normalized'],[label,b,unit]))
        for text,members in options:
            lm=re.fullmatch(r'(\d+(?:\.\d+)?)(MM|毫米|M|米)',text)
            if lm:
                value=float(lm.group(1))*(1000 if lm.group(2) in ('M','米') else 1)
                if 0<value<=30000:result.append((value,members))
    return result


def extract_fields(blocks, min_score=.80):
    blocks = reconstruct_labels(prepare_blocks(blocks))
    fields = []
    for anchor in blocks:
        for kind, pattern in ANCHORS.items():
            hit = pattern.search(anchor['normalized'])
            if not hit:
                continue
            g = anchor['g']
            tail = anchor['normalized'][hit.end():].lstrip(':：')
            candidates = []
            inline = _evidence(anchor, [], kind, tail, 0)
            if inline:
                candidates.append(inline)
                if kind=='spec':
                    for raw,members in spec_continuations(anchor,[],blocks,tail):
                        c=_evidence(anchor,members,kind,raw,.05,value_in_anchor=True)
                        if c:candidates.append(c)
            else:
                for b in blocks:
                    if b['id'] == anchor['id'] or angle_gap(g, b['g']) > 18:
                        continue
                    # Don't consume another field label as a value block.
                    if any(p.search(b['normalized']) for p in ANCHORS.values()) or STOP.match(b['normalized']):
                        continue
                    h = max(g['height'], b['g']['height'])
                    x, y = local(b['g']['center'], g)
                    gap = x - (g['width'] + b['g']['width']) / 2
                    same_row = -.6*h <= gap <= 10*h and abs(y) <= .65*h and x > 0
                    below = .65*h < y <= 2.7*h and abs(x + (g['width']-b['g']['width'])/2) <= 3*h
                    if not (same_row or below):
                        continue
                    if kind=='spec' and spec_barrier(anchor,b,blocks):
                        continue
                    cost = max(0, gap)/h*.18 + abs(y)/h if same_row else 1.1+y/h+abs(x)/h*.15
                    # A phi token can be attached to the anchor while digits are separate.
                    prefix = tail if tail in ('Φ', 'Ф', 'Ø', '∅', '⌀') else ''
                    raw = prefix + b['normalized']
                    c = _evidence(anchor, [b], kind, raw, cost)
                    if c:
                        candidates.append(c)
                    if kind=='spec':
                        for joined,members in spec_continuations(anchor,[b],blocks,raw):
                            c=_evidence(anchor,members,kind,joined,cost+.05)
                            if c:candidates.append(c)
            # Prefer a complete size from the same OCR row over its bare diameter prefix.
            if kind == 'spec':
                complete = [c for c in candidates if c['value'].get('length_raw')]
                candidates = [c for c in candidates if not any(
                    d['value']['diameter_mm'] == c['value']['diameter_mm'] and
                    set(c['block_ids']).issubset(d['block_ids']) and
                    (not c['value'].get('length_raw') or
                     (c['value'].get('length_mm') is None and d['value'].get('length_mm') is not None))
                    for d in complete)]
            candidates.sort(key=lambda c:c['cost'])
            unique = []
            for c in candidates:
                if not any(c['value'] == d['value'] for d in unique):
                    unique.append(c)
            field = {'id': f'{kind}_{anchor["id"]}', 'kind': kind,
                     'label': hit.group(0), 'anchor_text': anchor['text'],
                     'anchor_polygon': anchor['polygon'], 'g': g,
                     'rotation': anchor.get('rotation', 0), 'candidates': unique,
                     'value': unique[0]['value'] if unique else None, 'issues': []}
            field['anchor_source_block_ids']=source_ids(anchor)
            field['anchor_score']=anchor.get('score')
            field['reconstructed_label']=anchor.get('reconstructed_label',False)
            if hit.group(0).startswith('$'):
                field['issues'].append('anchor_ocr_variant')
            if len(list(pattern.finditer(anchor['normalized'])))>1:
                field['issues'].append('multiple_field_labels_in_one_ocr_block')
            if not unique:
                field['issues'].append('anchor_value_missing')
            else:
                field.update({k:unique[0][k] for k in ('raw','block_ids','value_polygon','ocr_min_score',
                                                     'value_ocr_score','value_source_block_ids','value_score_scope')})
                if len(unique) > 1 and unique[1]['cost'] - unique[0]['cost'] < .65:
                    field['issues'].append('anchor_value_ambiguous')
                if field['ocr_min_score'] is None or field['ocr_min_score'] < min_score:
                    field['issues'].append('low_or_missing_ocr_score')
                if kind == 'spec' and field['value']['diameter_mm'] not in COMMON_DIAMETERS:
                    field['issues'].append('unusual_diameter')
                if kind == 'spec' and field['value'].get('length_mm') is None:
                    lengths = length_candidates(anchor,blocks)
                    if lengths:
                        if len({v for v,b in lengths})==1:
                            field['value'] = dict(field['value'],length_mm=lengths[0][0])
                            members={b['id']:b for v,group in lengths for b in group}
                            field['length_evidence'] = [{'text':b['text'],'block_id':b['id'],'score':b.get('score')} for b in members.values()]
                            field['block_ids'] = list(dict.fromkeys(field['block_ids']+list(members)))
                            if any(b.get('score') is None or b['score']<min_score for b in members.values()):
                                field['issues'].append('low_or_missing_length_ocr_score')
                        else:
                            field['issues'].append('length_ambiguous')
                if kind=='spec' and field['value'].get('length_mm') is not None and not 100<=field['value']['length_mm']<=30000:
                    field['value']=dict(field['value'],observed_length_mm=field['value']['length_mm'],length_mm=None)
                    field['issues'].append('length_out_of_range')
            fields.append(field)
    # English labels can be a few pixels below their Chinese equivalents. When
    # a reconstructed Chinese SIZE label has an inline value, do not let its
    # English caption form an independent candidate from the trailing length.
    redundant=set()
    for english in fields:
        if english['kind']!='spec' or english['label'] not in ('SIZE','$IZE'):continue
        for chinese in fields:
            if chinese['kind']!='spec' or chinese['label']!='规格' or chinese['value'] is None:continue
            if not chinese['reconstructed_label']:continue
            h=max(chinese['g']['height'],english['g']['height'])
            x,y=local(english['g']['center'],chinese['g'])
            left=x+(chinese['g']['width']-english['g']['width'])/2
            if abs(left)<h and -.2*h<=y<=1.1*h:
                redundant.add(english['id']);chinese.setdefault('alternate_labels',[]).append(english['label'])
    fields=[f for f in fields if f['id'] not in redundant]
    # Bilingual labels may point at the same physical value. Never deduplicate by value alone.
    result = []
    for f in sorted(fields, key=lambda f:(bool(f['issues']), -(f.get('ocr_min_score') or 0))):
        duplicate = next((d for d in result if d['kind'] == f['kind'] and f['value'] is not None
                          and d['value'] == f['value'] and set(d.get('block_ids', [])) & set(f.get('block_ids', []))), None)
        if duplicate:
            duplicate.setdefault('alternate_labels', []).append(f['label'])
        else:
            result.append(f)
    return result


def inverse_point(point, rotation, width, height):
    """Inverse PIL expand=True counterclockwise rotation; continuous pixel-edge coordinates."""
    x,y = point
    return {0:(x,y), 90:(width-y,x), 180:(width-x,height-y), 270:(y,height-x)}[rotation]


def restore_field(field, rotation, width, height):
    f = dict(field)
    for key in ('anchor_polygon','value_polygon'):
        if key in f:
            f[key] = [list(inverse_point(p,rotation,width,height)) for p in f[key]]
    f['g'] = geometry(f['anchor_polygon'])
    f['candidates'] = [dict(c,value_polygon=[list(inverse_point(p,rotation,width,height)) for p in c['value_polygon']])
                       for c in f['candidates']]
    f['rotation'] = rotation
    return f


def fuse_fields(fields):
    """Combine repeated views at the same location; disagreement stays explicit."""
    result = []
    for f in sorted(fields, key=lambda f:(f['value'] is None, bool(f['issues']), -(f.get('ocr_min_score') or 0))):
        same = next((d for d in result if d['kind']==f['kind'] and
                     math.dist(d['g']['center'],f['g']['center']) < .85*min(d['g']['height'],f['g']['height'])), None)
        if same is None:
            result.append(f)
        else:
            same.setdefault('other_views', []).append({'rotation':f['rotation'],'value':f['value'],'issues':f['issues']})
            if f['value'] is not None and f['value'] != same['value']:
                if 'rotation_disagreement' not in same['issues']:
                    same['issues'].append('rotation_disagreement')
    return result


def assignment(costs):
    """Rectangular Hungarian algorithm, n rows <= m columns, including dummy columns."""
    if not costs:
        return [], 0.0
    n,m = len(costs),len(costs[0])
    if n > m or any(len(r)!=m for r in costs):
        raise ValueError('Invalid assignment matrix')
    u,v,p,way = [0.]*(n+1),[0.]*(m+1),[0]*(m+1),[0]*(m+1)
    for i in range(1,n+1):
        p[0]=i; j0=0; mins=[float('inf')]*(m+1);used=[False]*(m+1)
        while True:
            used[j0]=True;i0=p[j0];delta=float('inf');j1=0
            for j in range(1,m+1):
                if not used[j]:
                    cur=costs[i0-1][j-1]-u[i0]-v[j]
                    if cur<mins[j]:mins[j]=cur;way[j]=j0
                    if mins[j]<delta:delta=mins[j];j1=j
            for j in range(m+1):
                if used[j]:u[p[j]]+=delta;v[j]-=delta
                else:mins[j]-=delta
            j0=j1
            if p[j0]==0:break
        while True:
            j1=way[j0];p[j0]=p[j1];j0=j1
            if j0==0:break
    out=[-1]*n
    for j in range(1,m+1):
        if p[j]:out[p[j]-1]=j-1
    return out,sum(costs[i][j] for i,j in enumerate(out))


DEFAULTS = {'max_vertical_h': 6.0, 'max_horizontal_h': 5.0, 'max_angle_deg': 18.0,
            'max_cost': 6.0, 'ambiguity_margin': .8, 'min_ocr_score': .8}


def pair_cost(spec, batch, fields, cfg):
    a,b=spec['g'],batch['g'];h=median([a['height'],b['height']])
    if angle_gap(a,b)>cfg['max_angle_deg']:
        return None,'orientation_mismatch'
    x,y=local(b['center'],a);x/=h;y/=h
    # Key anchors are left-aligned even if value lengths differ markedly.
    left = x+(a['width']-b['width'])/(2*h)
    if abs(y)>cfg['max_vertical_h'] or abs(left)>cfg['max_horizontal_h']:
        return None,'distance_gate'
    # Another occurrence of either key between endpoints is a boundary signal.
    for f in fields:
        if f['id'] in (spec['id'],batch['id']) or angle_gap(a,f['g'])>cfg['max_angle_deg']:
            continue
        fx,fy=local(f['g']['center'],a);fx/=h;fy/=h
        if min(0,y)+.45<fy<max(0,y)-.45 and abs(fx-x*(fy/y))<2.0:
            return None,'intervening_field_anchor'
    cost=.55*abs(y)+.9*abs(left)+angle_gap(a,b)/30
    return cost,'geometry'


def assess_record(record, by_id, min_score):
    """Separate pairing, value recognition, field-name hints and optional length.

    Legacy issues/status remain conservative for existing JSON consumers. These
    checks explain the cause; they do not certify OCR correctness or alter values.
    """
    checks = {}
    for key, field_id in [('diameter', record['spec_id']), ('number', record['batch_id'])]:
        field = by_id.get(field_id)
        problems = []
        score = field.get('value_ocr_score') if field else None
        if not field or field['value'] is None:
            problems.append('anchor_value_missing')
        else:
            problems.extend(k for k in field['issues'] if k in {
                'anchor_value_ambiguous', 'multiple_field_labels_in_one_ocr_block',
                'rotation_disagreement', 'unusual_diameter'})
            if score is None or score < min_score:
                problems.append('low_or_missing_value_ocr_score')
        checks[key] = {'status': 'needs_review' if problems else 'score_pass',
                       'score': score, 'issues': problems,
                       'score_scope': 'ocr_blocks_not_individual_characters',
                       'source_block_ids': field.get('value_source_block_ids', []) if field else []}
        label_issues = []
        label_score = field.get('anchor_score') if field else None
        if field:
            if label_score is None or label_score < min_score:
                label_issues.append('low_or_missing_anchor_ocr_score')
            if 'anchor_ocr_variant' in field['issues']:
                label_issues.append('anchor_ocr_variant')
        checks['spec_label' if key == 'diameter' else 'batch_label'] = {
            'status': 'notice' if label_issues else ('score_pass' if field else 'missing'),
            'score': label_score, 'issues': label_issues}
    pairing_issues = [k for k in record['issues'] if k in {
        'pairing_ambiguous', 'batch_unmatched', 'spec_unmatched'}]
    checks['pairing'] = {'status': 'needs_review' if pairing_issues else 'matched',
                         'issues': pairing_issues}
    spec = record['specification']
    length_issues = [k for k in record['issues'] if k.startswith('length_') or k == 'low_or_missing_length_ocr_score']
    if spec and spec.get('length_mm') is None and not length_issues:
        length_issues.append('length_not_extracted')
    checks['length'] = {'status': 'notice' if length_issues else ('extracted' if spec else 'missing'),
                        'issues': length_issues}
    if pairing_issues:
        status = 'pairing_review'
    elif any(checks[k]['status'] == 'needs_review' for k in ('diameter', 'number')):
        status = 'value_review'
    elif any(checks[k]['issues'] for k in ('spec_label', 'batch_label', 'length')):
        status = 'matched_with_notes'
    else:
        status = 'matched'
    record['checks'] = checks
    record['review_status'] = status


def match_fields(fields, config=None):
    cfg={**DEFAULTS,**(config or {})}
    for k,v in cfg.items():
        if k not in DEFAULTS or not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) or v<=0:
            raise ValueError(f'Invalid matcher option: {k}')
    if cfg['min_ocr_score']>1:
        raise ValueError('min_ocr_score must not exceed 1')
    specs=[f for f in fields if f['kind']=='spec'];batches=[f for f in fields if f['kind']=='batch']
    edges=[];costs=[];big=1e6
    for s in specs:
        row=[]
        for b in batches:
            cost,reason=pair_cost(s,b,fields,cfg)
            if s['value'] is None or b['value'] is None:
                cost=None;reason='missing_value'
            edges.append({'spec_id':s['id'],'batch_id':b['id'],'cost':cost,'reason':reason})
            row.append(cost if cost is not None and cost<cfg['max_cost'] else big)
        costs.append(row+[cfg['max_cost']]*len(specs))
    selected,total=assignment(costs)
    records=[];used=set()
    for i,j in enumerate(selected):
        s=specs[i];issues=list(s['issues']);b=None;margin=None
        if j<len(batches) and costs[i][j]<cfg['max_cost']:
            b=batches[j];used.add(j);issues+=b['issues']
            alternate=[r[:] for r in costs];alternate[i][j]=big
            _,alt_total=assignment(alternate);margin=alt_total-total
            if margin<cfg['ambiguity_margin']:
                issues.append('pairing_ambiguous')
        else:
            issues.append('batch_unmatched')
        if (s['value'] and s['value'].get('length_raw') and s['value'].get('length_mm') is None
                and 'length_out_of_range' not in issues and 'length_ambiguous' not in issues):
            issues.append('length_unit_unspecified')
        records.append({'id':f'pair_{len(records)+1:03d}','spec_id':s['id'],
                        'batch_id':b['id'] if b else None,'specification':s['value'],
                        'specification_raw':s.get('raw'), 'batch_label':b['label'] if b else None,
                        'batch_number':b['value']['number'] if b and b['value'] else None,
                        'cost':costs[i][j] if b else None,'alternative_margin':margin,
                        'issues':sorted(set(issues)),
                        'status':'needs_review' if issues else 'rule_pass_review_required',
                        'confirmed':False})
    for j,b in enumerate(batches):
        if j not in used:
            records.append({'id':f'pair_{len(records)+1:03d}','spec_id':None,'batch_id':b['id'],
                            'specification':None,'specification_raw':None,'batch_label':b['label'],
                            'batch_number':b['value']['number'] if b['value'] else None,
                            'cost':None,'alternative_margin':None,
                            'issues':sorted(set(b['issues']+['spec_unmatched'])),
                            'status':'needs_review','confirmed':False})
    by_id = {f['id']: f for f in fields}
    for record in records:
        assess_record(record, by_id, cfg['min_ocr_score'])
    return {'version':VERSION,'config':cfg,'fields':fields,'candidate_edges':edges,'records':records,
            'summary':{'spec_anchor_count':len(specs),'batch_anchor_count':len(batches),
                       'pair_count':sum(r['spec_id'] is not None and r['batch_id'] is not None for r in records),
                       'rule_pass_count':sum(r['status']=='rule_pass_review_required' for r in records),
                       'needs_review_count':sum(r['status']=='needs_review' for r in records),
                       'matched_count':sum(r['review_status']=='matched' for r in records),
                       'matched_with_notes_count':sum(r['review_status']=='matched_with_notes' for r in records),
                       'pairing_review_count':sum(r['review_status']=='pairing_review' for r in records),
                       'value_review_count':sum(r['review_status']=='value_review' for r in records),
                       'status':'no_anchors_detected' if not fields else 'draft_review_required',
                       'plate_detection_performed':False,'all_plates_found':None}}
