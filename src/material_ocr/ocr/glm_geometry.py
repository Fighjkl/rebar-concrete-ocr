"""Measure independent printed forms for GLM without OCR or image files.

The returned polygons describe the pixels of the supplied PIL image. Callers
that rotate the input must map these coordinates back to the original page.
Form kinds are deliberately generic: a physical rectangle alone cannot prove
that it is a nameplate or a delivery note.
"""
from __future__ import annotations


def _area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def _intersection(left, right):
    return max(0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0, min(left[3], right[3]) - max(left[1], right[1]))


def _outer_boxes(boxes):
    """A table outline wins over its cells; independent forms never overlap."""
    kept = []
    for box in sorted(set(boxes), key=lambda value: (-_area(value), value)):
        # Closely neighbouring raw/grid contours describe the same form.
        if any(_intersection(box, other) >= .72 * min(_area(box), _area(other))
               for other in kept):
            continue
        if any(_intersection(box, other) > .08 * min(_area(box), _area(other))
               for other in kept):
            continue
        kept.append(box)
    return kept


def _heading_top(mask, box, other_boxes):
    """Include nearby header ink above a grid, stopping at another form.

    No text is read here. Horizontal ink bands merely extend a measured form
    to include its printed heading when the heading sits outside its border.
    """
    import cv2
    import numpy as np

    height, width = mask.shape
    left, top, right, bottom = box
    limit = max(0, top - round(min(height * .12, (bottom - top) * .42)))
    for other in other_boxes:
        if other == box or other[3] > top:
            continue
        overlap = max(0, min(right, other[2]) - max(left, other[0]))
        if overlap > .15 * min(right - left, other[2] - other[0]):
            limit = max(limit, other[3] + 2)
    if limit >= top:
        return top
    ink = np.count_nonzero(mask[limit:top, left:right], axis=1)
    active = (ink >= max(3, round((right - left) * .003))).astype('uint8')
    active = cv2.morphologyEx(active.reshape(-1, 1), cv2.MORPH_CLOSE,
                             np.ones((max(1, round(height * .002)), 1), dtype='uint8')).reshape(-1)
    positions = np.flatnonzero(active)
    if not positions.size:
        return top
    groups = []
    for position in positions:
        if not groups or position > groups[-1][1] + 1:
            groups.append([int(position), int(position)])
        else:
            groups[-1][1] = int(position)
    cursor = top
    maximum_gap = max(round(height * .025), round((bottom - top) * .14))
    for start, end in reversed(groups):
        start, end = start + limit, end + limit
        if cursor - end > maximum_gap:
            break
        cursor = start
    return cursor


def _rule_network_boxes(binary):
    """Join almost horizontal/vertical printed rules even on skewed scans.

    Faint nameplate outlines are often open. Repeated long rules can still
    locate the card without relying on text or completing a contour by guess.
    """
    import cv2

    height, width = binary.shape
    found = cv2.HoughLinesP(binary, 1, 3.141592653589793 / 720,
        threshold=max(45, width // 18), minLineLength=max(70, width // 6),
        maxLineGap=max(4, width // 100))
    lines = []
    # OpenCV commonly returns N×1×4, but some builds return N×4.
    # Flatten the trailing coordinate dimensions rather than assuming either.
    for raw in [] if found is None else found.reshape(-1, 4):
        x1, y1, x2, y2 = map(int, raw)
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        horizontal = dy <= dx * .065
        vertical = dx <= dy * .065
        if not (horizontal or vertical):
            continue
        if (horizontal and dx > width * .91) or (vertical and dy > height * .40):
            continue
        lines.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2), horizontal))
    if len(lines) > 400:
        # Dense photographic texture is not dependable form geometry.
        return []
    parent = list(range(len(lines)))
    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    for index, left in enumerate(lines):
        for other_index in range(index + 1, len(lines)):
            right = lines[other_index]
            if left[4] and right[4]:
                overlap = max(0, min(left[2], right[2]) - max(left[0], right[0]))
                lengths = (left[2] - left[0], right[2] - right[0])
                connect = (min(lengths) >= .65 * max(lengths)
                           and overlap >= .62 * min(lengths)
                           and abs((left[1] + left[3] - right[1] - right[3]) / 2) < height * .04)
            elif not left[4] and not right[4]:
                overlap = max(0, min(left[3], right[3]) - max(left[1], right[1]))
                connect = (overlap >= .62 * min(left[3] - left[1], right[3] - right[1])
                           and abs((left[0] + left[2] - right[0] - right[2]) / 2) < width * .025)
            else:
                horizontal, vertical = (left, right) if left[4] else (right, left)
                vx, hy = (vertical[0] + vertical[2]) / 2, (horizontal[1] + horizontal[3]) / 2
                connect = (horizontal[0] - width * .06 <= vx <= horizontal[2] + width * .06
                           and vertical[1] - height * .012 <= hy <= vertical[3] + height * .012)
            if connect:
                parent[root(other_index)] = root(index)
    groups = {}
    for index, line in enumerate(lines): groups.setdefault(root(index), []).append(line)
    boxes = []
    for group in groups.values():
        horizontal = [line for line in group if line[4]]
        if len(horizontal) < 3:
            continue
        box = (min(line[0] for line in group), min(line[1] for line in group),
               max(line[2] for line in group) + 1, max(line[3] for line in group) + 1)
        if box[2] - box[0] > width * .16 and box[3] - box[1] > height * .065:
            boxes.append(box)
    return boxes


def detect_form_regions(image, *, page_no=1):
    """Return measured, nonoverlapping form candidates in input pixel axes.

    This function performs no OCR, file I/O, or orientation inference. It only
    considers substantial closed outlines and connected table-rule networks;
    unbounded text and the paper edge remain part of the full-page OCR pass.
    """
    import cv2
    import numpy as np

    rgb = np.asarray(image.convert('RGB'))
    if not rgb.size:
        return []
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    if min(height, width) < 80:
        return []
    mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    # Local contrast also recovers faded carbon-copy rules without treating
    # the scanned paper's broad gray shadow as a filled rectangular form.
    adaptive = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                     cv2.THRESH_BINARY_INV, 35, 12)
    boxes = _rule_network_boxes(adaptive)
    for binary in (mask, adaptive):
        horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (max(18, width // 45), 1)))
        vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(18, height // 65))))
        rules = cv2.morphologyEx(cv2.bitwise_or(horizontal, vertical), cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (max(2, width // 500), max(2, height // 600))))
        outlines = cv2.morphologyEx(binary, cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
        # RETR_LIST is necessary when a page-border contains several pasted
        # cards. The explicit outer-box filter below removes their cells.
        for layer in (rules, outlines):
            contours, _ = cv2.findContours(layer, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                left, top, bw, bh = cv2.boundingRect(contour)
                area = bw * bh
                if not (bw >= max(80, width * .16) and bh >= max(45, height * .055)
                        and .008 * width * height <= area <= .80 * width * height
                        and .4 <= bw / bh <= 5.5):
                    continue
                if bw > width * .9 and bh > height * .82:
                    continue
                contour_area = cv2.contourArea(contour)
                # A bounding rectangle around open text/table fragments is
                # not a measured closed form boundary.
                if contour_area / area < .46:
                    continue
                boxes.append((left, top, left + bw, top + bh))
    boxes = _outer_boxes(boxes)
    output = []
    for box in sorted(boxes, key=lambda value: (value[1] // max(1, height // 100), value[0])):
        left, top, right, bottom = box
        top = _heading_top(adaptive, box, boxes)
        x_margin, y_margin = max(3, round(width * .008)), max(3, round(height * .006))
        crop = [max(0, left - x_margin), max(0, top - y_margin),
                min(width, right + x_margin), min(height, bottom + y_margin)]
        # Padding may approach a neighbour but cannot borrow its text.
        for other in boxes:
            if other == box:
                continue
            if _intersection(crop, other) == 0:
                continue
            if other[3] <= box[1]: crop[1] = max(crop[1], other[3] + 1)
            elif other[1] >= box[3]: crop[3] = min(crop[3], other[1] - 1)
            elif other[2] <= box[0]: crop[0] = max(crop[0], other[2] + 1)
            elif other[0] >= box[2]: crop[2] = min(crop[2], other[0] - 1)
        left, top, right, bottom = map(int, crop)
        output.append(dict(region_id=f'p{int(page_no):03d}-geometry-{len(output) + 1:02d}',
            kind='form_candidate', bbox_upright=[left, top, right, bottom],
            polygon_page=[[left, top], [right, top], [right, bottom], [left, bottom]],
            boundary_method='physical_outline_and_table_rules',
            coordinate_note='Measured form boundary in input-image pixel axes; not word or cell coordinates'))
    # Header padding cannot make two otherwise independent regions overlap.
    for index, left in enumerate(output):
        for right in output[index + 1:]:
            a, b = left['bbox_upright'], right['bbox_upright']
            if not _intersection(a, b):
                continue
            if abs((a[1] + a[3]) - (b[1] + b[3])) > abs((a[0] + a[2]) - (b[0] + b[2])):
                upper, lower = (a, b) if a[1] < b[1] else (b, a)
                cut = (upper[3] + lower[1]) // 2
                upper[3], lower[1] = cut, cut
            else:
                first, last = (a, b) if a[0] < b[0] else (b, a)
                cut = (first[2] + last[0]) // 2
                first[2], last[0] = cut, cut
    for region in output:
        left, top, right, bottom = region['bbox_upright']
        region['polygon_page'] = [[left, top], [right, top], [right, bottom], [left, bottom]]
    return output


def detect_panel_viewports(image, *, page_no=1):
    """Measure repeated shaded-header panels, without claiming card borders.

    Touching pasted cards defeat connected-line form detection. Repeated wide
    shaded headers plus separate large ink components provide independent
    column/row evidence. White headings at the bottom can be located by those
    components. The padded results are OCR viewports, not physical boundaries.
    A single table or a page with no repeated header pattern returns nothing.
    """
    import cv2
    import numpy as np
    gray = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    binary = (gray < 180).astype('uint8') * 255
    # Black-background cards can touch through scan noise or a narrow pasted
    # seam. Opening removes those bridges before considering the large dark
    # silhouettes. Unlike column grouping this also supports staggered cards.
    opened = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
        np.ones((max(3, round(h*.004)), max(3, round(w*.004))), dtype='uint8'))
    silhouettes = []
    for contour in cv2.findContours(opened, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        x, y, bw, bh = cv2.boundingRect(contour)
        if (.22*w < bw < .68*w and .07*h < bh < .40*h and 1.15 < bw/bh < 2.8
                and cv2.contourArea(contour)/(bw*bh) > .25
                and (binary[y:y+bh, x:x+bw] > 0).mean() > .55):
            silhouettes.append((x,y,x+bw,y+bh))
    if len(silhouettes) >= 2:
        output = []
        for x1,y1,x2,y2 in sorted(silhouettes, key=lambda b: (b[1]//max(1,round(h*.03)),b[0])):
            # Keep disjoint measured silhouettes; symmetric padding could
            # otherwise reintroduce the neighbouring card's characters.
            px, py = max(2,round(w*.003)), max(2,round(h*.003))
            box = [max(0,x1-px),max(0,y1-py),min(w,x2+px),min(h,y2+py)]
            for other in silhouettes:
                if other == (x1,y1,x2,y2):continue
                if max(y1,other[1]) < min(y2,other[3]):
                    if other[2] <= x1:box[0]=max(box[0],(other[2]+x1)//2)
                    if other[0] >= x2:box[2]=min(box[2],(x2+other[0])//2)
            a,b,c,d=box
            output.append(dict(region_id=f'p{page_no:03d}-panel-view-{len(output)}',
                kind='independent_panel_view', bbox_upright=box,
                polygon_page=[[a,b],[c,b],[c,d],[a,d]],
                boundary_method='separate_dark_card_silhouettes', physical_card_boundary_verified=False))
        return output
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE,
        np.ones((max(3, round(h*.004)), max(3, round(w*.006))), dtype='uint8'))
    boxes = []
    for contour in cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        x, y, bw, bh = cv2.boundingRect(contour)
        if .25*w < bw < .60*w and .09*h < bh < .85*h:
            boxes.append((x, y, x+bw, y+bh))
    columns = []
    for box in sorted(boxes, key=lambda b: (b[0]+b[2])/2):
        center = (box[0]+box[2])/2
        if not columns or center - np.median([(b[0]+b[2])/2 for b in columns[-1]]) > .25*w:
            columns.append([box])
        else:
            columns[-1].append(box)
    if len(columns) < 2 or len(columns) > 3:
        return []
    output = []
    for column in columns:
        left, right = min(b[0] for b in column), max(b[2] for b in column)
        # Recompute within each measured column. A touching bottom pair can
        # be one page-wide component and must not disappear from both sides.
        column_boxes = []
        local = closed[:, left:right]
        for contour in cv2.findContours(local, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
            x,y,bw,bh = cv2.boundingRect(contour)
            if bw > (right-left)*.65 and bh > h*.09:
                column_boxes.append((left+x,y,left+x+bw,y+bh))
        if len(column_boxes) < 2:
            return []
        inset = round((right-left)*.07)
        density = (gray[:, left+inset:right-inset] < 225).mean(axis=1)
        window = max(3, round(h*.011))
        smooth = np.convolve(density, np.ones(window)/window, mode='same')
        groups = []
        for y in np.flatnonzero(smooth > .45):
            if not groups or y > groups[-1][-1]+1:
                groups.append([int(y)])
            else:
                groups[-1].append(int(y))
        headers = [g[0] for g in groups if len(g) > h*.03]
        if len(headers) < 3:
            return []
        starts = list(headers)
        for box in column_boxes:
            if all(abs(box[1]-y) > h*.07 for y in starts):
                starts.append(box[1])
        starts.sort()
        bottom = max(b[3] for b in column_boxes)
        if bottom-starts[-1] < h*.09:
            return []
        for top, end in zip(starts, starts[1:]+[bottom]):
            if not h*.09 < end-top < h*.40:
                return []
            pad = max(4, round(h*.008))
            x1, x2 = max(0,left-round(w*.01)), min(w,right+round(w*.01))
            y1, y2 = max(0,top-pad), min(h,end+pad)
            output.append(dict(region_id=f'p{page_no:03d}-panel-view-{len(output)}',
                kind='independent_panel_view', bbox_upright=[x1,y1,x2,y2],
                polygon_page=[[x1,y1],[x2,y1],[x2,y2],[x1,y2]],
                boundary_method='repeated_shaded_headers_and_ink_components',
                physical_card_boundary_verified=False))
    return output
