"""One whole-page retry for an identifier whose initial reading is digits only."""
import re

POLICY=dict(version='delivery-number-whole-page-v2',close_kernel=2,min_score=.85,
            text_det_thresh=.15,text_det_box_thresh=.35)


def needs_retry(view):
    from .paddle_spatial_resources import role_of
    if role_of(view['blocks'])!='delivery_note':
        return False
    pattern=r'(?:配送单号|送货单号|单据编号|\bNO)[码]?[:：.\s]*([A-Z0-9/-]+)'
    for block in view['blocks']:
        match=re.search(pattern,block['text'],re.I)
        if match and re.fullmatch(r'\d{6,}',match[1]):
            return True
    return False


def prepare_image(image):
    import cv2
    import numpy as np
    from PIL import Image
    gray=np.asarray(image.convert('L'))
    _,ink=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
    ink=cv2.morphologyEx(ink,cv2.MORPH_CLOSE,np.ones((POLICY['close_kernel'],)*2,np.uint8))
    return Image.fromarray(255-ink).convert('RGB')


def needs_literal_check(view):
    from .paddle_spatial_resources import role_of
    return role_of(view['blocks'])=='delivery_note' and any(
        re.search(r'(?:配送单号|送货单号|单据编号|\bNO)[码]?[:：.\s]*[A-Z]{1,8}\d{6,}',b['text'],re.I)
        and (b.get('score') or 0)<.98 for b in view['blocks'])


def recover_number(item, checkpoint):
    from .paddle_spatial_resources import converted_blocks, delivery
    from .paddle_anchor_recovery import overlap
    value=item.get('delivery_no','')
    numeric=bool(re.fullmatch(r'\d{6,}',value))
    if not numeric and not re.fullmatch(r'[A-Z]{1,8}\d{6,}',value):
        return item
    primary=item['delivery_number_observations']
    found=[]
    for view in checkpoint['ocr_passes']:
        if view.get('detail_retry') not in ('delivery_number','delivery_literal'):continue
        alternative=delivery(checkpoint,converted_blocks(checkpoint,view),[],item['candidate_id'])
        for obs in alternative['delivery_number_observations']:
            ev=obs['evidence']
            if (ev.get('ocr_score') or 0)<POLICY['min_score']:continue
            if not any(overlap(o['evidence']['polygon_page'],ev['polygon_page'])>.5 for o in primary):continue
            if numeric and re.fullmatch(r'[A-Z]{1,8}'+re.escape(value),obs['value'],re.I):
                found.append(obs)
            elif (not numeric and view.get('detail_retry')=='delivery_literal' and (ev.get('ocr_score') or 0)>=.95
                  and len(value)==len(obs['value']) and re.sub(r'\D','',value)==re.sub(r'\D','',obs['value'])
                  and sum(a!=b for a,b in zip(value,obs['value']))==1):
                found.append(obs)
    values=list(dict.fromkeys(o['value'] for o in found))
    if not values:return item
    item['delivery_number_candidates']=list(dict.fromkeys(item['delivery_number_candidates']+values))
    item['delivery_number_observations'].extend(found)
    if len(values)==1:
        item['delivery_no']=values[0]
        item['delivery_number_recovery']=dict(method='whole_page_prefix_observation',
            original=value,recovered=values[0],original_preserved=True)
        if not numeric:item['issues'].append('delivery_number_prefix_view_conflict')
    else:
        item['issues'].append('delivery_number_prefix_view_conflict')
    return item
