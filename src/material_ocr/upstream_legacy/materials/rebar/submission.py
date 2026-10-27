"""B.0.6 报审表：表框/下划线定位 -> 四字段OCR -> 来源与校验草稿。

python -m material_ocr.upstream_legacy.materials.rebar.submission --pdf data/input.pdf --page 1 --out runs/cover_test --device gpu:0
加 --preview-only 只验证几何，不加载OCR。页码从1开始，旋转角度为逆时针。
"""
import argparse
from datetime import date
import html
from pathlib import Path
import re
from types import SimpleNamespace
import unicodedata

import cv2
import numpy as np

from material_ocr.upstream_legacy.extraction.table import (create_ocr, digest, load_image, ocr_cell, read_json, render,
                 save_image, write_json)
from material_ocr.upstream_legacy.ocr.orientation import rotate_page
from material_ocr.upstream_legacy.ocr.cell import FixedCellOCR
from material_ocr.upstream_legacy.materials.rebar.routing import classify_page_header
from material_ocr.upstream_legacy.paths import rebar_config

LABELS = {'document_no': '文件编号', 'supply_date': '供货日期',
          'use_location': '使用部位', 'material_name': '材料名称'}


def underline_candidates(binary, config, *, fragmented=False):
    """Find ink-supported underlines; never synthesize missing whole fields.

    The fallback merges only short gaps between collinear line fragments in
    the body band. It changes geometry proposals, NOT the source OCR pixels.
    """
    if not fragmented:
        horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((1, 90), np.uint8))
        contours, _ = cv2.findContours(horizontal, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        return [cv2.boundingRect(c) for c in contours]
    w, h = config['frame_size']
    pad = config['header_pad']
    settings = config.get('fragmented_underlines', {})
    minimum = settings.get('min_segment_px', 30)
    gap = settings.get('max_gap_px', 24)
    tolerance = settings.get('max_center_y_delta_px', 6)
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((1, minimum), np.uint8))
    contours, _ = cv2.findContours(horizontal, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    segments = [cv2.boundingRect(c) for c in contours]
    segments = [s for s in segments if s[2] >= minimum and s[3] < 20
                and config['underline_y_range'][0] <= (s[1]-pad)/h <= config['underline_y_range'][1]]
    groups = []
    for segment in sorted(segments, key=lambda s: (s[0], s[1])):
        x, y, ww, hh = segment
        matches = [group for group in groups
            if 0 <= x-max(s[0]+s[2] for s in group) <= gap
            and max(abs(y+hh/2-(s[1]+s[3]/2)) for s in group) <= tolerance]
        if len(matches) == 1:
            matches[0].append(segment)
        else:
            groups.append([segment])
    result = []
    for group in groups:
        x, y = min(s[0] for s in group), min(s[1] for s in group)
        x2, y2 = max(s[0]+s[2] for s in group), max(s[1]+s[3] for s in group)
        if sum(s[2] for s in group)/(x2-x) >= settings.get('min_ink_coverage', .85):
            result.append((x,y,x2-x,y2-y))
    return result


def select_underlines(segments, endpoints, config):
    w, h = config['frame_size']
    pad = config['header_pad']
    selected = {}
    for field, (starts, ends) in endpoints.items():
        matches = [(x,y,ww,hh) for x,y,ww,hh in segments
            if starts[0] <= x/w <= starts[1] and ends[0] <= (x+ww)/w <= ends[1]
            and config['underline_y_range'][0] <= (y-pad)/h <= config['underline_y_range'][1]
            and hh < 20]
        if len(matches) != 1:
            return None
        selected[field] = matches[0]
    if len(selected) != 3 or np.ptp([s[1] for s in selected.values()]) > 24:
        return None
    return selected


def locate_fields(image, config):
    """用整张报审表外框消除平移/缩放/轻微透视，再检测三条填写下划线。

    不用任意页绝对坐标兜底；不支持的表框或下划线明确报错。
    """
    scale = min(1., 1800 / image.shape[0])
    small = cv2.resize(image, None, fx=scale, fy=scale)
    bw = cv2.adaptiveThreshold(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), 255,
                              cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 35, 15)
    contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    w, h = config['frame_size']
    pad = config['header_pad']
    candidates = []
    for contour in contours:
        if cv2.contourArea(contour) < .30 * bw.size:
            continue
        p = cv2.approxPolyDP(contour, .012 * cv2.arcLength(contour, True), True)
        if len(p) != 4 or not cv2.isContourConvex(p):
            continue
        p = p[:, 0].astype(np.float32)
        sums, diffs = p.sum(1), np.diff(p, axis=1).ravel()
        quad = np.float32([p[sums.argmin()], p[diffs.argmin()], p[sums.argmax()], p[diffs.argmax()]])
        if len(np.unique(quad, axis=0)) != 4:
            continue
        matrix = cv2.getPerspectiveTransform(quad / scale,
                    np.float32([[0, pad], [w-1, pad], [w-1, pad+h-1], [0, pad+h-1]]))
        rect = cv2.warpPerspective(image, matrix, (w, h+pad), borderValue=(255, 255, 255))
        gray = cv2.cvtColor(rect, cv2.COLOR_BGR2GRAY)
        binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY_INV, 35, 15)
        segments = underline_candidates(binary, config)
        # 应有上下两个大签章区的分隔线；进场表/任意矩形不应套用此模板。
        separators = [(x, y, ww, hh) for x, y, ww, hh in segments
                      if ww > .90*w and .45 < (y-pad)/h < .78]
        if len(separators) != 1:
            continue
        selected = select_underlines(segments, config['underline_endpoints'], config)
        method = 'continuous_underlines'
        fallback_height = None
        date_fragments = None
        if selected is None and config.get('fragmented_underlines'):
            repaired = underline_candidates(binary, config, fragmented=True)
            selected = select_underlines(repaired, config['fragmented_underlines']['endpoints'], config)
            method = 'collinear_fragment_underlines'
        if selected is None:
            from .submission_layout_recovery import stacked_body_underlines
            recovered = stacked_body_underlines(
                underline_candidates(binary, config, fragmented=True), config)
            if recovered:
                selected, fallback_height, date_fragments = recovered
                method = 'stacked_body_date_underlines'
        if selected is None:
            continue
        boxes = {}
        for field, (x, y, ww, hh) in selected.items():
            inset = config['field_inset_px']
            boxes[field] = [x+inset, y-(fallback_height or config['field_height_px']), x+ww-inset, y-2]
            if method == 'collinear_fragment_underlines':
                # Some printed dates end on the underline's endpoint. Limited
                # right padding includes the glyph without the next form label.
                boxes[field][2] = min(w, x+ww+config['fragmented_underlines'].get('right_padding_px', 12))
                boxes[field][3] = y+hh+config['fragmented_underlines'].get('bottom_padding_px', 5)
        for field, key in [('document_no', 'document_no_box'), ('body_context', 'body_band')]:
            region = config[key]
            if method in ('collinear_fragment_underlines', 'stacked_body_date_underlines'):
                region = config['fragmented_underlines'].get(key, region)
            x1, y1, x2, y2 = region
            boxes[field] = [round(x1*w), round(pad+y1*h), round(x2*w), round(pad+y2*h)]
        inverse = np.linalg.inv(matrix)
        page_polygons = {}
        valid = True
        for field, (x1, y1, x2, y2) in boxes.items():
            poly = cv2.perspectiveTransform(np.float32([[[x1,y1],[x2,y1],[x2,y2],[x1,y2]]]), inverse)[0]
            if np.any(poly < 0) or np.any(poly > [image.shape[1]-1, image.shape[0]-1]):
                valid = False
            page_polygons[field] = poly.tolist()
        if valid:
            candidates.append((rect, matrix, boxes, {'outer_quad_upright': (quad/scale).tolist(),
                                'alignment_method': method,
                                'date_underline_fragments': date_fragments,
                                'underline_segments': selected, 'polygons_upright': page_polygons}))
    if len(candidates) != 1:
        raise ValueError(f'submission_alignment_unresolved:candidates={len(candidates)}; 请检查方向、外框和正文下划线')
    return candidates[0]


def preview_submission(folder, template, rotation=0):
    folder = Path(folder)
    config = read_json(template)
    image = load_image(folder/'page.png')
    meta = read_json(folder/'page_meta.json')
    if digest(folder/'page.png') != meta['image_sha256']:
        raise ValueError('页面图已变更，请重新render')
    upright, rotation_matrix = rotate_page(image, rotation)
    rect, matrix, boxes, diagnostics = locate_fields(upright, config)
    matrix = matrix @ rotation_matrix
    inverse = np.linalg.inv(matrix)
    overlay = rect.copy()
    page_overlay = image.copy()
    fields = {}
    (folder/'submission_preview').mkdir(exist_ok=True)
    for field, box in boxes.items():
        x1,y1,x2,y2 = box
        poly = cv2.perspectiveTransform(np.float32([[[x1,y1],[x2,y1],[x2,y2],[x1,y2]]]), inverse)[0]
        fields[field] = {'box_form':box, 'polygon_page':poly.tolist()}
        save_image(folder/'submission_preview'/f'{field}.png', rect[y1:y2,x1:x2])
        if field != 'body_context':
            cv2.rectangle(overlay,(x1,y1),(x2,y2),(0,0,255),2)
            cv2.putText(overlay,field,(x1,max(15,y1-6)),cv2.FONT_HERSHEY_SIMPLEX,.65,(0,0,180),2)
            cv2.polylines(page_overlay,[poly.astype(np.int32)],True,(0,0,255),3)
    save_image(folder/'submission_form.png',rect)
    save_image(folder/'submission_preview.png',overlay)
    save_image(folder/'submission_page_preview.png',page_overlay)
    data = {'schema_version':'submission_v1','template':config,'rotation_ccw':rotation,
            'image_sha256':meta['image_sha256'],'matrix_page_to_form':matrix.tolist(),
            'fields':fields,'diagnostics':diagnostics,'geometry_review_status':'pending_review'}
    write_json(folder/'submission_geometry.json',data)
    return data


def compact(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).strip('，,。;；:：_')


def parse_field(field, raw, blocks, minimum_score=.95):
    text = compact(raw)
    scores = [float(b['score']) for b in blocks if b['text'].strip()]
    issues = []
    if not text:
        issues.append('not_detected_requires_review' if field == 'document_no' else 'required_field_not_detected')
        return {'value':None,'issues':issues,'status':'needs_review','min_score':min(scores) if scores else None}
    if not scores or min(scores) < minimum_score:
        issues.append('low_ocr_confidence')
    value = text
    if field == 'document_no':
        value = re.sub(r'^(?:文件)?编号[:：]?', '', text).strip('：:')
        value = value.replace('－','-').replace('—','-').replace('–','-')
        if not value:
            value = None
            issues.append('not_detected_requires_review')
        elif not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9./·•\-]*',value):
            issues.append('document_no_format_uncertain')
        if value and value.endswith(('-', '/', '·', '•')):
            issues.append('document_no_incomplete_or_prefix_only')
        # 当前模型无法保证手写编号准确，也不自动识别“确实未填写”。
        issues.append('document_no_visual_review_required')
    elif field == 'supply_date':
        matches = re.findall(r'(?<!\d)(\d{4})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})(?:日)?(?!\d)',text)
        value = None
        if len(matches) != 1:
            issues.append('date_missing_or_multiple')
        else:
            try:
                value = date(*map(int,matches[0])).isoformat()
            except ValueError:
                issues.append('invalid_calendar_date')
    else:
        if any(a in text for a in ('拟用于工程','部位的','经我方','项目监理','进场','编号')):
            issues.append('field_contains_form_labels_check_crop')
        if field == 'material_name' and '钢筋' not in text:
            issues.append('material_not_confirmed_as_rebar')
    return {'value':value,'issues':issues,'status':'needs_review' if issues else 'rule_pass_review_required',
            'min_score':min(scores) if scores else None}


def extract_submission(folder, device='gpu:0', engine=None):
    folder = Path(folder)
    meta = read_json(folder/'page_meta.json')
    geometry = read_json(folder/'submission_geometry.json')
    if digest(folder/'page.png') != geometry['image_sha256']:
        raise ValueError('页面已变更，请重新预览')
    out = folder/'submission_ocr'
    out.mkdir(exist_ok=False)
    (out/'fields').mkdir()
    engine = engine if engine is not None else create_ocr(device)
    fixed = engine if isinstance(engine,FixedCellOCR) else FixedCellOCR(engine,device)
    config = geometry['template']
    # 使用原始页和已存矩阵重新生成，避免预览PNG被手动修改后误用。
    w,h = config['frame_size']
    rect = cv2.warpPerspective(load_image(folder/'page.png'),np.array(geometry['matrix_page_to_form']),
                               (w,h+config['header_pad']),borderValue=(255,255,255))
    inverse = np.linalg.inv(np.array(geometry['matrix_page_to_form']))
    fields = {}
    page_issues = []
    context_text = ''
    for field, source in geometry['fields'].items():
        x1,y1,x2,y2 = source['box_form']
        path = out/'fields'/f'{field}.png'
        raw,blocks = ocr_cell(fixed,rect[y1:y2,x1:x2],path)
        for block in blocks:
            polygon = np.asarray(block['poly_crop'],np.float32) + [x1,y1]
            block['polygon_page'] = cv2.perspectiveTransform(polygon.astype(np.float32)[None],inverse)[0].tolist()
        evidence = {'raw_text':raw,'lines':raw.splitlines(),'blocks':blocks,
                    'crop_path':str(path.relative_to(out)).replace('\\','/'),**source}
        if field == 'body_context':
            context_text = compact(raw)
            fields[field] = evidence
        else:
            fields[field] = {**evidence,**parse_field(field,raw,blocks,config['minimum_ocr_score'])}
    if not all(a in context_text for a in ('进场','拟用于工程','部位')):
        page_issues.append('body_context_anchors_incomplete')
    data = {'schema_version':'submission_v1','record_id':f'p{meta["page_no"]}_submission',
            'pdf_sha256':meta['pdf_sha256'],'page_no':meta['page_no'],
            'render_dpi':meta['dpi'],'page_pixel_size':meta['pixel_size'],
            'document_type':'submission_form','template_id':config['template_id'],
            'values':{name:fields[name]['value'] for name in LABELS},'fields':fields,
            'issues':page_issues + [f'{name}:{issue}' for name in LABELS for issue in fields[name]['issues']],
            'geometry_review_status':'pending_review','ready_for_search':False,
            'coordinate_system':'original_300dpi_page_pixels_top_left',
            'note':'供货日期来自正文进场日期；编号未检出不等于原件未填写；所有结果为待核对草稿。'}
    data['status'] = 'needs_review' if data['issues'] else 'rule_pass_review_required'
    write_json(out/'result.json',data)
    rows = []
    for name,label in LABELS.items():
        f = fields[name]
        rows.append(f'<tr><td>{label}</td><td>{html.escape(str(f["value"]))}</td>'
                    f'<td><pre>{html.escape(f["raw_text"])}</pre></td>'
                    f'<td><a href="{f["crop_path"]}"><img src="{f["crop_path"]}" style="max-width:420px"></a></td>'
                    f'<td>{html.escape(str(f["issues"]))}</td></tr>')
    (out/'review.html').write_text('<!doctype html><meta charset="utf-8"><title>报审表四字段核对</title>'
        '<style>body{font-family:sans-serif;padding:24px}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:12px}</style>'
        '<h1>报审表四字段核对</h1><p>手写编号需看图确认；未检测到文字不能认定为空白。</p>'
        '<a href="../submission_page_preview.png">原页面定位</a> | <a href="fields/body_context.png">正文上下文</a>'
        f'<p>{html.escape(str(page_issues))}</p><table><tr><th>字段</th><th>解析值</th><th>原文</th><th>裁剪图</th><th>问题</th></tr>'
        + ''.join(rows) + '</table>',encoding='utf-8')
    print(f'报审表 page {meta["page_no"]}: {data["status"]}; 输出 {out/"result.json"}',flush=True)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf',required=True)
    parser.add_argument('--page',type=int,default=1)
    parser.add_argument('--out',required=True)
    parser.add_argument('--template',default=str(rebar_config('submission_b06.json')))
    parser.add_argument('--device',default='gpu:0')
    parser.add_argument('--rotation',type=int,default=0,choices=[0,90,180,270])
    parser.add_argument('--preview-only',action='store_true')
    args = parser.parse_args()
    render(SimpleNamespace(pdf=args.pdf,page=args.page,dpi=300,out=args.out))
    engine = None
    if not args.preview_only:
        engine = create_ocr(args.device)
        upright,_ = rotate_page(load_image(Path(args.out)/'page.png'),args.rotation)
        crop = upright[:round(len(upright)*.4)]
        _,blocks = ocr_cell(engine,crop,Path(args.out)/'header.png')
        route = classify_page_header(blocks,len(crop))
        write_json(Path(args.out)/'route.json',route)
        if route['decision'] != 'submission':
            raise ValueError('未确认是报审表，查看route.json和header.png；核对方向后重跑')
    preview_submission(args.out,args.template,args.rotation)
    if not args.preview_only:
        extract_submission(args.out,args.device,engine)
    print(f'请检查 {Path(args.out)/"submission_page_preview.png"}')


if __name__ == '__main__':
    main()
