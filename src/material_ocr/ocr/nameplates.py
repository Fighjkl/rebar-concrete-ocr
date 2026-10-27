"""Detect independent pasted cards using image contours AND local label evidence."""
import re
from .layout import box
from material_ocr.storage_artifacts import register_derivative


def card_bounds(image, blocks):
    import cv2
    gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY) if image.ndim==3 else image
    h,w=gray.shape
    mask=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    grid = labelled_card_bounds(mask, blocks)
    if grid:
        return grid
    # Bridge gaps in printed card outlines without joining separate cards.
    mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,cv2.getStructuringElement(cv2.MORPH_RECT,(3,3)))
    contours,_=cv2.findContours(mask,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    candidates=[]
    for contour in contours:
        x,y,bw,bh=cv2.boundingRect(contour)
        if not (.008*w*h < bw*bh < .42*w*h and .6<bw/bh<3.4 and bw>w*.16 and bh>h*.07):continue
        local=[b for b in blocks if box(b) and x<=(box(b)[0]+box(b)[2])/2<=x+bw and y<=(box(b)[1]+box(b)[3])/2<=y+bh]
        text=re.sub(r'\s+','',''.join(b.get('text','') for b in local)).upper()
        # Delivery/quality tables cannot serve as physical card boundaries.
        if any(t in text for t in ('送货单','发货单','质量证明书','进场数量清单')):continue
        if not re.search(r'(?:HRBF|HRB|HPB|RRB)\d{3}',text):continue
        if not any(t in text for t in ('炉批','炉号','批号','BATCH','HEAT')):continue
        candidates.append((x,y,x+bw,y+bh))
    # Keep outer outlines; nested table cells do not create extra cards.
    keep=[]
    for a in sorted(set(candidates),key=lambda a:-(a[2]-a[0])*(a[3]-a[1])):
        if any(max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))>
               .8*(a[2]-a[0])*(a[3]-a[1]) for b in keep):continue
        if any(max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))>0 for b in keep):continue
        keep.append(a)
    return sorted(keep,key=lambda a:(a[1]//max(1,h//30),a[0]))


def labelled_card_bounds(mask, blocks):
    """Infer pasted-card columns from whitespace and one grade anchor per card.

    Repeated company headers delimit rows. A card with no independent header
    uses the gap between adjacent grade anchors, and local OCR validates that
    it contains exactly one grade/heat pair before it can be matched.
    """
    import numpy as np
    h,w=mask.shape
    anchors=[b for b in blocks if box(b) and re.search(r'(?:HRBF|HRB|HPB|RRB)\s*\d{3}[EB]?',b.get('text',''),re.I)]
    if not anchors:return []
    def cx(b): a=box(b);return (a[0]+a[2])/2
    def cy(b): a=box(b);return (a[1]+a[3])/2
    columns=[]
    for b in sorted(anchors,key=cx):
        if columns and cx(b)-np.median([cx(x) for x in columns[-1]])<w*.25:columns[-1].append(b)
        else:columns.append([b])
    if len(columns)>3:return []
    cuts=[0]
    for a,b in zip(columns,columns[1:]):
        lo=int((max(cx(x) for x in a)+min(cx(x) for x in b))/2-w*.08)
        hi=int((max(cx(x) for x in a)+min(cx(x) for x in b))/2+w*.08)
        lo,hi=max(cuts[-1]+1,lo),min(w-1,hi)
        if hi<=lo:return []
        density=(mask[:int(h*.94)]>0).sum(axis=0)
        cuts.append(lo+int(np.argmin(density[lo:hi])))
    cuts.append(w)
    output=[]
    for i,col in enumerate(columns):
        ordered=sorted(col,key=cy);starts=[]
        companies=[b for b in blocks if box(b) and cuts[i]<=cx(b)<cuts[i+1]
                   and re.search(r'公司|GROUP|SPECIALSTEEL',b.get('text',''),re.I)
                   and not re.search(r'生产厂[:：]|生产单位[:：]',b.get('text',''))]
        for j,anchor in enumerate(ordered):
            previous=cy(ordered[j-1]) if j else 0
            heads=[b for b in companies if previous<cy(b)<cy(anchor)]
            start=min(box(b)[1] for b in heads)-h*.012 if heads else ((previous+cy(anchor))/2 if j else max(0,cy(anchor)-h*.15))
            starts.append(max(0,int(start)))
        for j,anchor in enumerate(ordered):
            top=starts[j];bottom=starts[j+1]-2 if j+1<len(starts) else min(h,int(box(anchor)[3]+h*.12))
            local=[b for b in blocks if box(b) and cuts[i]<=cx(b)<cuts[i+1] and top<=cy(b)<bottom]
            text=re.sub(r'\s+','',''.join(b.get('text','') for b in local)).upper()
            if any(t in text for t in ('质量证明书','送货单','发货单','进场数量清单')):return []
            output.append((cuts[i],top,cuts[i+1],bottom))
    full_text=re.sub(r'\s+','',''.join(b.get('text','') for b in blocks)).upper()
    if not any(t in full_text for t in ('批号','炉批','炉号','BATCH','HEAT')):
        return []
    return sorted(output,key=lambda b:(b[1]//max(1,h//30),b[0]))


def physical_regions(engine,image,blocks,page_no,image_dir,original_shape,rotation,rules):
    from .attachments import _ocr_blocks,_map_blocks_to_original,build_regions,_map_point_to_original
    import cv2
    regions=[]
    for bounds in card_bounds(image,blocks):
        x0,y0,x1,y1=bounds
        entity=f'card-{x0}-{y0}-{x1}-{y1}'
        path=image_dir/f'p{page_no:03d}_{entity}.png'
        crop=image[y0:y1,x0:x1];cv2.imwrite(str(path),crop)
        register_derivative(path,image_dir/f'page_{page_no:03d}_upright.png',
                            [{'crop':[x0,y0,x1,y1]}],kind='physical_nameplate_crop')
        local=_ocr_blocks(engine,crop,path)
        for b in local:
            b['polygon_page']=[[p[0]+x0,p[1]+y0] for p in b.get('polygon_page') or []]
            b['polygon_ocr']=b['polygon_page']
        local=_map_blocks_to_original(local,original_shape,rotation)
        selected=build_regions(local,page_no=page_no,rotation_ccw=rotation,rules=rules,
            region_id=f'p{page_no:03d}_{entity}',document_type='nameplate',entity_key=entity,_layout=False)
        polygon=[_map_point_to_original(p,original_shape,rotation) for p in [[x0,y0],[x1,y0],[x1,y1],[x0,y1]]]
        for region in selected:
            region.update(physical_plate_id=f'p{page_no:03d}:{entity}',layout_role='physical_nameplate',
                          polygon_page=polygon,boundary_method='contour_and_local_labels',crop_path=str(path))
        regions.extend(selected)
    return regions
