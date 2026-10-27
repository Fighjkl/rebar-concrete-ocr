"""Independent delivery forms/rows reconstructed from immutable saved OCR.

The index receives deterministic synthetic *container regions*, not new OCR.
Every row keeps the form header via form_region_id so repeated row numbers on
one PDF page never share a delivery identity. No damaged grade is corrected.
"""
from copy import deepcopy
from decimal import Decimal
import re

from .filling import (DELIVERY_TITLE, GRADE, bbox, center, compact, decimal_text,
    delivery_form_scopes, evidence, page_blocks, same_line, specification_parts, delivery_grade_candidates,
    _upright_evidence_center)
from .matching import delivery_rows


SPEC_HEADER=re.compile(r'^(?:规格(?:型号)?(?:[/／]材质)?|型号规格|直径(?:mm)?|格?[/／]材质)$',re.I)
GRADE_HEADER=re.compile(r'^(?:材质|牌号|型号|钢号|星级)$')
WEIGHT_HEADER=re.compile(r'^(?:重量|数量|净重)(?:[（(](?:吨|t|kg|千克)[）)])?$',re.I)
FOOTER=re.compile(r'^(?:合计|总计|金额|数量大写|送货地址|说明|购货单位[（(]盖章|供货单位[（(]盖章)')
NUMBER=re.compile(r'^\d+(?:[.,]\d+)?$')


def _within(point,bounds):
    return bool(point and bounds[0]<=point[0]<bounds[2] and bounds[1]<=point[1]<bounds[3])


def _observation(page,block,name,value,row_id,entity,*,unit=None):
    ev=evidence(page,block,'physical_delivery_table_cell')
    ev.update(row_id=row_id,column_unit=unit)
    return dict(role='delivery_note',page_no=page['page_no'],field=name,
                normalized_value=value,value_t=value if name=='quantity_t' else None,
                raw_value=block.get('text'),ocr_raw_value=block.get('text'),accepted=True,
                status='physical_table_observation',entity_key=entity,row_id=row_id,
                region_id=block.get('region_id'),value_evidence=ev)


def _physical_lines(page,scope):
    """Find goods rows from table geometry, retaining incomplete specifications."""
    blocks=scope['blocks'];ty=center(scope['title'])[1]
    headers=[b for b in blocks if center(b) and center(b)[1]>ty
             and SPEC_HEADER.fullmatch(compact(b.get('text')))]
    if not headers:return []
    header=min(headers,key=lambda b:center(b)[1]);hy=center(header)[1]
    columns=same_line(blocks,header)
    # Only table-column headings define the specification cell horizontally.
    known=[b for b in columns if re.fullmatch(r'品名|材料名称|生产企业|厂家|产地|件重|件数|支数|签收件数|签收数量|单价|金额|备注',compact(b.get('text')))
           or SPEC_HEADER.fullmatch(compact(b.get('text'))) or GRADE_HEADER.fullmatch(compact(b.get('text')))
           or WEIGHT_HEADER.fullmatch(compact(b.get('text')))]
    if len(known)<2:return []
    known.sort(key=lambda b:center(b)[0])
    def bounds_for(head):
        x=center(head)[0]
        before=[center(b)[0] for b in known if center(b)[0]<x]
        after=[center(b)[0] for b in known if center(b)[0]>x]
        return ((max(before)+x)/2 if before else x-200,
                (min(after)+x)/2 if after else x+200)
    sx0,sx1=bounds_for(header)
    gh=next((b for b in known if GRADE_HEADER.fullmatch(compact(b.get('text')))),None)
    wh=next((b for b in known if WEIGHT_HEADER.fullmatch(compact(b.get('text')))),None)
    gx= bounds_for(gh) if gh else None
    wx= bounds_for(wh) if wh else None
    footers=[center(b)[1] for b in blocks if center(b) and center(b)[1]>hy+10 and FOOTER.match(compact(b.get('text')))]
    bottom=min(footers) if footers else scope['bounds'][3]
    goods=[b for b in blocks if center(b) and hy+max(8,(bbox(header)[3]-bbox(header)[1])*.35)<center(b)[1]<bottom]
    cells=[b for b in goods if sx0<=center(b)[0]<sx1 or (gx and gx[0]<=center(b)[0]<gx[1])]
    # A positive goods-weight cell independently preserves an unreadable spec
    # row. Unlike received quantity, plain weight has no implied tonne unit.
    if wx:
        cells += [b for b in goods if wx[0]<=center(b)[0]<wx[1] and NUMBER.fullmatch(compact(b.get('text')))
                  and Decimal(compact(b['text']).replace(',','.'))>0
                  and any(other is not b and abs(center(other)[1]-center(b)[1])<=max(12,(bbox(b)[3]-bbox(b)[1])*.6)
                          and ((sx0<=center(other)[0]<sx1) or
                               (sx1<=center(other)[0]<wx[0] and NUMBER.fullmatch(compact(other.get('text')))))
                          for other in goods)]
    # Exclude prose and empty/line artifacts, while retaining damaged compact
    # grade strings as an explicit physical row anchor.
    cells=[b for b in cells if len(compact(b.get('text')))<=40 and not FOOTER.match(compact(b.get('text')))]
    groups=[]
    for b in sorted(cells,key=lambda b:center(b)[1]):
        tolerance=max(12,(bbox(b)[3]-bbox(b)[1])*.6)
        if groups and abs(center(b)[1]-sum(center(x)[1] for x in groups[-1])/len(groups[-1]))<=tolerance:
            groups[-1].append(b)
        else:groups.append([b])
    rows=[]
    for group in groups:
        y=sum(center(b)[1] for b in group)/len(group)
        line=[b for b in goods if abs(center(b)[1]-y)<=max(12,(bbox(b)[3]-bbox(b)[1])*.6)]
        spec_cells=[b for b in line if sx0<=center(b)[0]<sx1]
        grade_cells=[b for b in line if gx and gx[0]<=center(b)[0]<gx[1]]
        grade,diameter=specification_parts(page,spec_cells,'physical_delivery_specification',specification_column=True)
        grades,_=specification_parts(page,grade_cells,'physical_delivery_grade')
        # Preserve damaged handwritten grade words from their own cells. The
        # cross-page join can use numeric strength digits, while the original
        # text and its exact column/row evidence remain available for review.
        grade['candidates'].extend(delivery_grade_candidates(page,spec_cells,'physical_delivery_specification'))
        grades['candidates'].extend(delivery_grade_candidates(page,grade_cells,'physical_delivery_grade',grade_column=True))
        # A handwritten diameter can place its explicit phi after the number.
        for b in spec_cells:
            suffix=re.fullmatch(r'(\d{1,2})[ΦφФØ⌀]',compact(b.get('text')))
            if suffix and 5<=int(suffix[1])<=50:
                diameter['candidates'].append(dict(value=suffix[1],evidence=[evidence(page,b,'physical_delivery_suffix_phi')]))
        observations=[]
        for name,values in [('grade',grade['candidates']+grades['candidates']),('diameter_mm',diameter['candidates'])]:
            for val in values:
                ev=val['evidence'][0]
                observations.append(dict(role='delivery_note',page_no=page['page_no'],field=name,
                    normalized_value=val['value'],value_t=None,raw_value=ev.get('raw_text'),ocr_raw_value=ev.get('raw_text'),
                    accepted=True,status='physical_table_observation',value_evidence=deepcopy(ev)))
        if wh and wx and re.search(r'[（(](?:吨|t)[）)]',compact(wh['text']),re.I):
            for b in line:
                text=compact(b.get('text'))
                if wx[0]<=center(b)[0]<wx[1] and NUMBER.fullmatch(text) and Decimal(text.replace(',','.'))>0:
                    observations.append(_observation(page,b,'quantity_t',decimal_text(text.replace(',','.')),None,None,unit='t'))
        if not spec_cells and not grade_cells and not observations:continue
        rows.append(dict(y=y,blocks=line,observations=observations,header=header))
    return rows


def build_delivery_resources(index,catalog,inventory):
    """Return (row candidates, row keyword records), appending scoped regions.

    Rows follow matching.delivery_rows and add candidate_id/form_id. Old test
    mocks or pages without table structure retain the original resources.
    """
    deliveries=[];rows=[];replaced_pages=set()
    for page in index.get('pages',[]):
        scopes=delivery_form_scopes(page)
        parsed=[(scope,_physical_lines(page,scope)) for scope in scopes]
        if not parsed:continue
        # If no table on this page can be rebuilt, preserve its legacy row
        # observations as well as each independent physical header below.
        # With at least one readable table, unreadable sibling forms remain
        # explicit header-only resources rather than disappearing entirely.
        if any(lines for _,lines in parsed):replaced_pages.add(page['page_no'])
        page['regions']=[r for r in page.get('regions',[]) if not r.get('physical_delivery_resource')]
        for scope,lines in parsed:
            form=scope['form_id'];number=int(form.rsplit('delivery',1)[1])
            entity=f'delivery-form-{number}';container=f'{form}_container'
            page['regions'].append(dict(region_id=container,entity_key=entity,document_type='delivery_note',
                layout_role='physical_delivery_form',physical_delivery_resource=True,
                form_id=form,blocks=deepcopy(scope['blocks'])))
            if not lines:
                deliveries.append(dict(candidate_id=f'delivery_note:{page["page_no"]}:{entity}:header-only',
                    page_no=page['page_no'],entity_key=entity,row_id=None,form_id=form,
                    form_region_id=container,physical_row_index=None,header_only=True,
                    certificate_numbers=[],heat_lots=[],quantities_t=[],diameters_mm=[],grades=[],
                    field_observations=[],associated_record_ids=[],status='header_only',write_allowed=False,
                    diagnostics=[dict(code='delivery_table_rows_unreadable',page_no=page['page_no'],
                        reason='送货单表头或货物行未可靠定位，仅保留供应公司及No.来源供最终核验。',
                        locations=[evidence(page,scope['title'],'physical_delivery_header_only')])]))
                continue
            for pos,line in enumerate(lines,1):
                row_id=f'{form}_row{pos}';cid=f'delivery_note:{page["page_no"]}:{entity}:row{pos}'
                obs=deepcopy(line['observations'])
                # Recovered column OCR may be better than full-page OCR. Its
                # coordinates must fall in this physical form AND this row.
                neighbour_gap=min([abs(line['y']-other['y']) for other in lines if other is not line] or [80])
                tolerance=max(12,min(neighbour_gap*.45,45))
                for item in catalog:
                    if item.get('role')!='delivery_note' or item.get('page_no')!=page['page_no'] or not item.get('accepted'):
                        continue
                    if item.get('field') not in ('grade','diameter_mm','quantity_t'):continue
                    point=_upright_evidence_center(page,item.get('value_evidence') or {})
                    if _within(point,scope['bounds']) and abs(point[1]-line['y'])<=tolerance:
                        obs.append(deepcopy(item))
                for o in obs:
                    o.update(page_no=page['page_no'],entity_key=entity,row_id=row_id,region_id=row_id)
                    o.setdefault('value_evidence',{}).update(form_id=form,row_id=row_id,physical_row_index=pos)
                def values(name):
                    return sorted({str(o.get('value_t') if name=='quantity_t' and o.get('value_t') is not None else o.get('normalized_value'))
                                   for o in obs if o.get('field')==name and (o.get('value_t') if name=='quantity_t' else o.get('normalized_value')) is not None})
                row=dict(page_no=page['page_no'],entity_key=entity,row_id=row_id,candidate_id=cid,form_id=form,
                    physical_row_index=pos,quantities_t=values('quantity_t'),diameters_mm=values('diameter_mm'),
                    grades=values('grade'),evidence=deepcopy(obs),quantity_scores=[
                    float((o.get('value_evidence') or {})['score']) for o in obs if o.get('field')=='quantity_t'
                    and (o.get('value_evidence') or {}).get('score') is not None])
                candidate=dict(candidate_id=cid,page_no=page['page_no'],entity_key=entity,row_id=row_id,
                    form_id=form,form_region_id=container,physical_row_index=pos,
                    certificate_numbers=[],heat_lots=[],quantities_t=row['quantities_t'],diameters_mm=row['diameters_mm'],
                    grades=row['grades'],field_observations=obs,associated_record_ids=[],
                    status='automatic_candidate',write_allowed=False)
                page['regions'].append(dict(region_id=row_id,entity_key=entity,row_id=row_id,
                    document_type='delivery_note',layout_role='physical_delivery_row',physical_delivery_resource=True,
                    form_id=form,blocks=deepcopy(line['blocks'])))
                deliveries.append(candidate);rows.append(row)
    # Mixed indexes may contain OCR pages and older catalog-only pages.
    remaining=[deepcopy(c) for c in inventory if c.get('page_no') not in replaced_pages]
    deliveries.extend(remaining)
    fallback_pages=sorted({x.get('page_no') for x in catalog if x.get('role')=='delivery_note'}-replaced_pages)
    for page_no in fallback_pages:
        for row in delivery_rows(catalog,page_no):
            matches=[c for c in remaining if (c.get('page_no'),c.get('entity_key'),c.get('row_id'))==
                     (page_no,row['entity_key'],row['row_id'])]
            if len(matches)==1:row['candidate_id']=matches[0]['candidate_id']
            rows.append(row)
    return deliveries,rows
