"""Bounded whole-page retries for missing attachment labels/table headers."""
from copy import deepcopy
import re

POLICY = dict(version="whole-page-detail-v3", text_det_thresh=.15,
              text_det_box_thresh=.35, close_kernel=3, line_kernel=150,
              min_header_score=.60)


def retry_kind(view, size):
    from .paddle_spatial_resources import role_of, spatial_resources
    from .paddle_attachment_resources import anchor_page
    role = role_of(view["blocks"])
    if role == "excluded":
        return None
    cp = dict(page_no=1, original_pixel_size=size, dpi=300,
              rotation_ccw=view["rotation_ccw"], ocr_passes=[view],
              raw_text="\n".join(b["text"] for b in view["blocks"]))
    from .paddle_attachment_orientation import quality
    if role in ('unknown','nameplate') and len(view['blocks'])>=180 and quality(view['blocks'])['weak']:
        # Dense watermark fragments can hide the title entirely. Cleanup is
        # permitted without guessing the document class; classify its new text.
        return 'certificate_text'
    if role=='quality_certificate':
        if len(view['blocks'])<80:return None
        from .paddle_attachment_orientation import quality
        resources=spatial_resources([cp])
        if quality(view['blocks'])['weak'] or any(not c['heat_lots'] or not c['certificate_numbers'] for c in resources['certificates']):
            return 'certificate_text'
        return None
    if role == "delivery_note":
        resources = spatial_resources([cp])
        if any(not d["rows"] or any("specification_raw" not in r for r in d["rows"])
               for d in resources["deliveries"]):
            return "delivery_table_headers"
        return None
    text = re.sub(r"\s+", "", cp["raw_text"]).upper()
    if not re.search(r"(?:HRB|HPB|RRB)\d{3}[E]?", text):
        return None
    report, _ = anchor_page(cp)
    if any(not r.get("batch_number") or not r.get("specification") for r in report["records"]):
        return "nameplate_labels"
    return None


def prepare_image(image, kind):
    if kind not in ("delivery_table_headers","certificate_text"):
        return image.copy()
    import cv2
    import numpy as np
    from PIL import Image
    gray = np.asarray(image.convert("L"))
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if kind=='certificate_text':return Image.fromarray(255-ink).convert('RGB')
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE,
                          np.ones((POLICY["close_kernel"],) * 2, np.uint8))
    length = max(30, round(POLICY["line_kernel"] * max(image.size) / 3507))
    lines = cv2.bitwise_or(
        cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, length), np.uint8)),
        cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((length, 1), np.uint8)))
    cleaned = 255 - cv2.bitwise_and(ink, cv2.bitwise_not(
        cv2.dilate(lines, np.ones((3, 3), np.uint8))))
    return Image.fromarray(cleaned).convert("RGB")


def delivery_header_blocks(checkpoint, blocks):
    """Supplement missing headers only; preserve primary values and identifiers."""
    from .paddle_spatial_resources import converted_blocks
    from .paddle_anchor_recovery import overlap
    from .multiform_resources import compact, delivery_weight_header
    labels = {"规格", "规格型号", "型号规格", "规格/材质", "规格材质", "直径", "重量", "发货数", "发货数量", "配送数量",
              "吨", "（吨）", "(吨)", "T", "（T）", "(T)"}
    def category(text):
        value=compact(text)
        if value in {"规格", "规格型号", "型号规格", "规格/材质", "规格材质", "直径"}:
            return "specification"
        if re.fullmatch(r"[（(]?(?:吨|T)[)）]?",value):
            return "unit"
        return "weight"
    result = list(blocks)
    for view in checkpoint["ocr_passes"]:
        if view.get("detail_retry") != "delivery_table_headers":
            continue
        for b in converted_blocks(checkpoint, view):
            if (compact(b["text"]) not in labels and not delivery_weight_header(b["text"])) or (b.get("score") or 0) < POLICY["min_header_score"]:
                continue
            overlapping=[old for old in result
                if (compact(old["text"]) in labels or delivery_weight_header(old["text"]))
                and category(old["text"])==category(b["text"])
                and overlap(old["polygon_page"], b["polygon_page"]) > .5]
            if any((old.get("score") or 0)>=POLICY["min_header_score"] for old in overlapping):
                continue
            result=[old for old in result if old not in overlapping]
            result.append(deepcopy(b))
    return result
