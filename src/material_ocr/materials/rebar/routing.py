"""Page routing using title families AND independent column labels."""
import re
import unicodedata
from difflib import SequenceMatcher
from material_ocr.materials.rebar.entry_schema import ENTRY_TITLES, FIELD_ALIASES

TITLE = "材料构配件进场检验记录"
HEADERS = ["序号", "规格", "进场", "数量", "生产厂家", "合格证号", "检验项目", "检验结果"]
OTHER_TITLES = ["质量证明书", "质量证明", "送货单", "发货单", "报审表", "产品合格证"]


def normalize(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text))


def classify_header(blocks, crop_height):
    usable = [b for b in blocks if b["text"].strip()]
    all_text = "".join(normalize(b["text"]) for b in usable)
    # 裁剪为页上方40%；标题要求位于裁剪区前45%，对应整页前18%。
    top = [b for b in usable if sum(p[1] for p in b["poly_crop"])/len(b["poly_crop"]) < crop_height*.45]
    top.sort(key=lambda b: (min(p[1] for p in b["poly_crop"]),min(p[0] for p in b["poly_crop"])))
    candidates = []
    # 允许标题被拆为最多3个相邻文字块；不跨整页任意拼关键词。
    for i in range(len(top)):
        for length in range(1, 4):
            chunk = top[i:i+length]
            if len(chunk) != length:
                continue
            text = "".join(normalize(b["text"]) for b in chunk)
            for kind, titles in ENTRY_TITLES.items():
                for title in titles:
                    similarity = SequenceMatcher(None, title, text).ratio()
                    candidates.append((title in text, similarity, min(b["score"] for b in chunk), text, kind, chunk))
    exact = [x for x in candidates if x[0] and x[2] >= .90]
    # The title's '进场数量' is not evidence of a quantity COLUMN. Exclude
    # the shortest matching title span before collecting column-label votes.
    title_ids = set()
    for kind in {x[4] for x in exact}:
        best = min((x for x in exact if x[4] == kind), key=lambda x: (len(x[5]), len(x[3])))
        title_ids.update(id(b) for b in best[5])
    confident = "".join(normalize(b["text"]) for b in usable
                        if b["score"] >= .85 and id(b) not in title_ids)
    confident = confident.replace("单位工程名称", "").replace("工程名称", "")
    # Independent fields: '进场' and '数量' must not count as two votes.
    aliases = {**FIELD_ALIASES, "quantity": ("进场数量", "进场量", "数量")}
    hits = [name for name, labels in aliases.items() if any(a in confident for a in labels)]
    kinds = {x[4] for x in exact}
    reasons = []
    other_title = any(t in "".join(normalize(b["text"]) for b in top if b["score"] >= .90)
                      for t in OTHER_TITLES)
    possible_title = any(x[0] or x[1] >= .72 for x in candidates)
    columns_ok = {"row_no", "specification", "quantity"}.issubset(hits) and bool(
        {"material_name", "manufacturer"}.intersection(hits))
    kind = next(iter(kinds)) if len(kinds) == 1 else None
    if (other_title and possible_title) or len(kinds) > 1:
        decision = "review"
        reasons = ["conflicting_document_titles"]
    elif exact and columns_ok:
        decision = "target"
        reasons = ["supported_title_family", "independent_entry_column_labels"]
    elif other_title:
        decision = "other"
        reasons = ["another_document_title"]
    elif possible_title or "进场" in all_text or len(hits) >= 4:
        decision = "review"
        reasons = ["possible_entry_form_but_insufficient_evidence"]
    else:
        decision = "review"
        reasons = ["unknown_or_poor_ocr_or_rotated_page"]
    return {"decision": decision, "reasons": reasons, "header_hits": hits,
            "entry_form_type": kind if decision == "target" else None,
            "document_type": kind if decision == "target" else "unknown",
            "ocr_text": "\n".join(b["text"] for b in usable),
            "best_title_candidate": max(candidates,key=lambda x:(x[0],x[1]))[3] if candidates else ""}

def classify_page_header(blocks, crop_height):
    """保留进场表旧接口；新增报审表标题 + 正文锚点联合判定。"""
    result = classify_header(blocks, crop_height)
    top = sorted((b for b in blocks if b['score'] >= .90 and
                  sum(p[1] for p in b['poly_crop']) / len(b['poly_crop']) < crop_height * .45),
                 key=lambda b: (min(p[1] for p in b['poly_crop']), min(p[0] for p in b['poly_crop'])))
    titles = ['工程材料构配件设备报审表', '工程材料构配件或设备报审表', '工程材料设备报审表']
    title_text = ''.join(normalize(b['text']) for b in top)
    has_title = any(t in title_text for t in titles)
    text = ''.join(normalize(b['text']) for b in blocks if b['score'] >= .85)
    anchors = [a for a in ('工程名称', '进场', '拟用于工程', '部位', '经我方检验') if a in text]
    if has_title and (any(t in title_text for ts in ENTRY_TITLES.values() for t in ts)
                      or result['decision'] == 'target'):
        result.update(decision='review', reasons=['conflicting_document_titles'])
    elif has_title:
        # Alternative independent evidence when OCR loses '进场/部位'.
        # This routes the form; it does not certify extracted field values.
        body_evidence = [a for a in ('工程名称', '项目监理机构', '审查', '自检结果') if a in text]
        b06_body = ('表B06' in title_text and len(body_evidence) == 4
                    and re.search(r'20\d{2}年\d{1,2}月\d{1,2}日', text))
        if len(anchors) >= 3 and '进场' in anchors and '部位' in anchors:
            result.update(decision='submission', reasons=['submission_title_and_body_anchors'])
        elif b06_body:
            result.update(decision='submission', reasons=['submission_b06_title_and_independent_body_evidence'])
        else:
            result.update(decision='review', reasons=['submission_title_but_missing_body_anchors'])
    if result['decision'] != 'target':
        result['entry_form_type'] = None
        result['document_type'] = 'approval_form' if result['decision'] == 'submission' else 'unknown'
    result['submission_anchor_hits'] = anchors if has_title else []
    return result
