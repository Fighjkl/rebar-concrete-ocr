"""固定有线进场表自动定位：外框透视校正 + 等间距明细行 + 列结构验证。"""
import cv2
import numpy as np


def adaptive_columns(xs, width, config):
    """Validate a complete grid against a template, retaining measured boundaries.

    A small boundary drift is allowed, but missing/extra lines, narrow or wide
    replacement columns and substantial layout changes remain unresolved.
    """
    expected = np.asarray(config['column_boundaries'], dtype=float)
    internal = np.asarray([x for x in xs if .02*width < x < .98*width], dtype=float)
    if len(internal) != len(expected)-2:
        return None
    measured = np.r_[0., internal/(width-1), 1.]
    if np.any(np.diff(measured) <= 0):
        return None
    drift = config['adaptive_columns']['max_boundary_drift']
    width_drift = config['adaptive_columns']['max_column_width_relative_drift']
    if (np.any(np.abs(measured-expected) > drift)
            or np.any(np.abs(np.diff(measured)/np.diff(expected)-1) > width_drift)):
        return None
    return measured.tolist()


def groups(values, threshold):
    indices = np.flatnonzero(values >= threshold)
    if not len(indices):
        return []
    return [int(round(g.mean())) for g in np.split(indices, np.where(np.diff(indices) > 1)[0]+1)]


def locate_body(image, config):
    # 降采样只用于定位；最终OCR仍使用原始300dpi页面。
    scale = min(1., 1800/image.shape[0])
    small = cv2.resize(image, None, fx=scale, fy=scale)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    bw = cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,cv2.THRESH_BINARY_INV,35,15)
    # 连通的表格外轮廓；不以页面边缘或文字包围框代替。
    contours, _ = cv2.findContours(bw,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for contour in contours:
        if cv2.contourArea(contour) < small.shape[0]*small.shape[1]*.25:
            continue
        polygon = cv2.approxPolyDP(contour,.015*cv2.arcLength(contour,True),True)
        if len(polygon) != 4 or not cv2.isContourConvex(polygon):
            continue
        p = polygon[:,0,:].astype(np.float32)
        sums, diffs = p.sum(axis=1), np.diff(p,axis=1).ravel()
        quad = np.float32([p[sums.argmin()],p[diffs.argmin()],p[sums.argmax()],p[diffs.argmax()]])
        if len(np.unique(quad,axis=0)) != 4:
            continue
        w,h = 1200,1800
        matrix = cv2.getPerspectiveTransform(quad,np.float32([[0,0],[w-1,0],[w-1,h-1],[0,h-1]]))
        rect = cv2.warpPerspective(bw,matrix,(w,h))
        hor = cv2.morphologyEx(rect,cv2.MORPH_OPEN,np.ones((1,100),np.uint8))
        ys = groups((hor>0).mean(axis=1),.70)
        n = config['expected_rows']
        for start in range(len(ys)-n):
            seq = ys[start:start+n+1]
            delta = np.diff(seq)
            cv = float(np.std(delta)/np.mean(delta))
            if cv > .10 or delta.min() < 25 or delta.max()/delta.min() > 1.4:
                continue
            # 同一固定版式中的明细区应占外框中部；拒绝签字框等误选。
            if not (.10 < seq[0]/h < .40 and .45 < seq[-1]/h < .85):
                continue
            body = rect[seq[0]+4:seq[-1]-4]
            ver = cv2.morphologyEx(body,cv2.MORPH_OPEN,np.ones((max(30,len(body)//5),1),np.uint8))
            xs = groups((ver>0).mean(axis=0),.65)
            expected = config['column_boundaries'][1:-1]
            if config.get('adaptive_columns'):
                boundaries = adaptive_columns(xs, w, config)
                if boundaries is None:
                    continue
            else:
                matches = [[x for x in xs if abs(x-f*(w-1)) < .015*w] for f in expected]
                if any(len(m)!=1 for m in matches):
                    continue
                boundaries = [0.] + [m[0]/(w-1) for m in matches] + [1.]
            corners = np.float32([[[0,seq[0]],[w-1,seq[0]],[w-1,seq[-1]],[0,seq[-1]]]])
            original = cv2.perspectiveTransform(corners,np.linalg.inv(matrix))[0]/scale
            candidates.append((original,{'method':'outer_frame_and_repeated_grid',
                'row_spacing_cv':cv,'detected_rows':n,'matched_internal_columns':len(expected),
                'detected_column_boundaries':boundaries,
                'outer_quad_page':(quad/scale).tolist(),'rectified_row_lines':seq}))
    if len(candidates) != 1:
        raise ValueError(f'auto_alignment_unresolved:candidates={len(candidates)}; 不回退固定坐标，请人工检查表框')
    return candidates[0]
