"""只重试异常序号格；不使用行位置、相邻序号或字符替换猜数字。"""
import re
import cv2
import numpy as np
from material_ocr.upstream_legacy.materials.rebar.parsing import compact


def number(text):
    value = compact(text)
    return int(value) if re.fullmatch(r'[0-9]{1,6}',value) and int(value)>0 else None


def needs_retry(text, blocks, threshold, expected=None):
    value = number(text)
    return (value is None or not blocks or any(b['score'] < threshold for b in blocks)
            or (expected is not None and value != expected))


def choose(original, attempts, threshold):
    values = [number(a.get('text','')) for a in attempts]
    reliable = [bool(a.get('blocks')) and all(b['score']>=threshold for b in a['blocks']) for a in attempts]
    # 两种处理都明确读出同一个数才采纳；这是重复证据，不是独立准确率保证。
    if len(values)!=2 or None in values or values[0]!=values[1] or not all(reliable):
        return None,'retry_inconclusive'
    if number(original) is not None and number(original)!=values[0]:
        return None,'retry_conflicts_with_original'
    return str(values[0]),'retry_consensus_pending_review'


def number_region(crop):
    gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)
    _,mask=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
    h,w=mask.shape
    _,_,stats,_=cv2.connectedComponentsWithStats(mask,8)
    # 只用于已知序号列；过滤接触边缘的框线，保留所有内部有效连通域，避免把12裁成1。
    valid=[s for s in stats[1:] if s[4]>=5 and s[0]>2 and s[1]>2
           and s[0]+s[2]<w-2 and s[1]+s[3]<h-2]
    if not valid:
        raise ValueError('No interior number region; do not guess from row position')
    x=max(0,min(s[0] for s in valid)-6)
    y=max(0,min(s[1] for s in valid)-6)
    right=min(w,max(s[0]+s[2] for s in valid)+6)
    bottom=min(h,max(s[1]+s[3] for s in valid)+6)
    return crop[y:bottom,x:right].copy(),[int(x),int(y),int(right),int(bottom)]


def retry_row(engine, crop, original, directory, cell_id, threshold, ocr, direct=None):
    directory.mkdir(exist_ok=True)
    attempts=[]
    # 不去线、不侵蚀文字、不旋转；只增加留白并扩大，降低单字符漏检概率。
    for scale in (2,3):
        enlarged=cv2.resize(crop,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC)
        pad=24*scale
        processed=cv2.copyMakeBorder(enlarged,pad,pad,pad,pad,cv2.BORDER_CONSTANT,value=(255,255,255))
        path=directory/f'{cell_id}_scale{scale}.png'
        attempt={'crop_path':f'{directory.name}/{path.name}',
                 'scale':scale,'padding_px':pad,'coordinate_system':'retry_image_pixels'}
        try:
            text,blocks=ocr(engine,processed,path)
            attempt.update(text=text,blocks=blocks)
        except Exception as exc:
            attempt.update(text='',blocks=[],error=str(exc))
        attempts.append(attempt)
    selected,status=choose(original,attempts,threshold)
    method='retry_consensus'
    if selected is None and direct is not None:
        direct_attempts=[]
        try:
            region,box=number_region(crop)
            for scale in (1,2):
                processed=cv2.resize(region,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC)
                path=directory/f'{cell_id}_direct{scale}.png'
                event={'crop_path':f'{directory.name}/{path.name}','scale':scale,
                       'region_in_original_cell':box,'coordinate_system':'direct_recognition_image_pixels',
                       'method':'recognition_only_no_detection_no_rotation'}
                try:
                    text,blocks=direct(processed,path)
                    event.update(text=text,blocks=blocks)
                except Exception as exc:
                    event.update(text='',blocks=[],error=str(exc))
                direct_attempts.append(event)
            candidate,direct_status=choose(original,direct_attempts,threshold)
            # 和之前高分的数字候选冲突时不采纳。
            reliable_previous=[number(a['text']) for a in attempts if number(a.get('text','')) is not None
                               and a.get('blocks') and all(b['score']>=threshold for b in a['blocks'])]
            if candidate is not None and any(v!=int(candidate) for v in reliable_previous):
                status='retry_conflicting_candidates'
            elif candidate is not None:
                selected=candidate
                status='direct_consensus_pending_review'
                method='direct_recognition_consensus'
            else:
                status=direct_status
        except Exception as exc:
            direct_attempts.append({'text':'','blocks':[],'error':str(exc),'method':'direct_region_preparation'})
        attempts.extend(direct_attempts)
    return {'original_text':original,'selected_text':selected,'status':status,
            'method':method,'attempts':attempts,'human_verified':False}
