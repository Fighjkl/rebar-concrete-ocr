"""Recover missing delivery cells from located, coherent whole-page OCR views."""
from copy import deepcopy
from .multiform_resources import delivery_rows, split_attachment_forms, center, height, compact
from .paddle_anchor_recovery import overlap


def resolve_supplier_variants(deliveries):
    """Complete one missing leading character only with a shared printed phone."""
    import re
    original=deepcopy(deliveries)
    def contacts(d):
        lines=(d.get('evidence') or {}).get('raw_text','').splitlines()
        return {re.sub(r'\D','',m[0]) for line in lines if re.search(r'电话|TEL',line,re.I)
                for m in re.finditer(r'(?<!\d)0\d{2,3}[-— ]\d{8}|(?<!\d)(?:0\d{2,3}[-— ]?)?\d{7,8}(?!\d)',line)}
    for d in deliveries:
        value=d.get('supplier','');phones=contacts(d)
        if not value or not phones:continue
        hits=[o for o in original if o['candidate_id']!=d['candidate_id'] and o.get('supplier')
              and o['supplier'].endswith(value) and len(o['supplier'])==len(value)+1
              and contacts(o)&phones and not (o.get('supplier_heading') or {}).get('issues')]
        if len({o['supplier'] for o in hits})!=1:continue
        heading=hits[0]['supplier_heading'];ev=heading.get('evidence') or []
        ev=ev if isinstance(ev,list) else [ev]
        if not any(e.get('polygon_page') and (e.get('ocr_score') or 0)>=.9 for e in ev):continue
        d['supplier_view_history']=[deepcopy(d.get('supplier_heading'))]
        d['supplier']=hits[0]['supplier'];d['supplier_heading']=deepcopy(heading)
        d['supplier_contact_corroboration']=dict(original=value,completed=d['supplier'],
            matching_printed_contacts=sorted(phones&contacts(hits[0])),source_page=hits[0]['page_no'],
            note='同一印刷联系电话、唯一完整公司名及仅缺首字的名称共同支持；原读数保留')


def recover_delivery(item,cp,blocks):
    from .paddle_spatial_resources import converted_blocks,delivery,role_of
    from material_ocr.attachment_legacy.layout import box
    def bounds(items):
        bs=[box(b) for b in items]
        return [[min(b[0] for b in bs),min(b[1] for b in bs)],
                [max(b[2] for b in bs),min(b[1] for b in bs)],
                [max(b[2] for b in bs),max(b[3] for b in bs)],
                [min(b[0] for b in bs),max(b[3] for b in bs)]]
    primary_band=bounds(blocks)
    for view in cp['ocr_passes']:
        if view.get('detail_retry')!='delivery_table_headers' or view['rotation_ccw']!=cp.get('rotation_ccw',view['rotation_ccw']):continue
        extra=converted_blocks(cp,view)
        forms=split_attachment_forms(extra) or [(role_of(extra),extra)]
        for role,eb in forms:
            if role!='delivery_note' or overlap(primary_band,bounds(eb))<.65:continue
            alt=delivery(cp,eb,[],item['candidate_id'])
            heading=alt.get('supplier_heading') or {};ev=heading.get('evidence') or {}
            if (alt.get('supplier') and not heading.get('issues') and isinstance(ev,dict)
                    and ev.get('polygon_page') and (ev.get('ocr_score') or 0)>=.85):
                old=item.get('supplier','');new=alt['supplier']
                if not old or (new.endswith(old) and len(new)-len(old)==1):
                    item.setdefault('supplier_view_history',[]).append(deepcopy(item.get('supplier_heading')))
                    item.update(supplier=new,supplier_heading=heading)
                    item['issues']=[i for i in item['issues'] if not i.startswith('supplier_')]
            # Supplement only missing/invalid row specifications. Existing
            # complete primary rows (including quantities) remain authoritative.
            for row in delivery_rows(cp,eb):
                if not row.get('diameter_mm'):continue
                rb=row['evidence'].get('blocks',[])
                if not rb or min(b.get('score') or 0 for b in rb if any(c.isdigit() for c in b['text']))<.60:continue
                nearby=[]
                for old in item['rows']:
                    ob=old.get('evidence',{}).get('blocks',[])
                    if ob and overlap(bounds(rb),bounds(ob))>.5:
                        nearby.append(old)
                if any(r.get('diameter_mm') for r in nearby):continue
                if len(nearby)>1:continue
                row=deepcopy(row)
                row['issues'].append('supplemental_whole_page_delivery_row_requires_review')
                if nearby:
                    old=nearby[0]
                    row['primary_observation']=deepcopy(old)
                    for key in ['quantity_t','quantity_raw','grade']:
                        if old.get(key):row[key]=old[key]
                    item['rows'].remove(old)
                item['rows'].append(row)
    return item
