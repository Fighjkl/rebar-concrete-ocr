"""Fast page orientation: reuse trusted classification, otherwise classify once.

OCR retries are bounded and only triggered by weak observed recognition.
All angles are counter-clockwise corrections, matching upstream and v3.
"""
from pathlib import Path
import json

MODEL_NAME = "PP-LCNet_x1_0_doc_ori"
POLICY = dict(version="fast-orientation-v3", min_classification_score=.90,
              min_mean_score=.75, min_blocks=3, min_characters=20,
              max_extra_ocr_passes=3, preview_max_side=1000,
              retry_incomplete_nameplate_anchors=True)


def trusted_hint(page):
    prediction = page.get("orientation_prediction") or {}
    if (page.get("orientation_error") or
            "conflicting_orientation_evidence" in page.get("reasons", [])):
        return None
    if (prediction.get("rotation_ccw") in (0,90,180,270) and
            float(prediction.get("score") or 0) >= POLICY["min_classification_score"]):
        return dict(prediction, source="upstream_classifier")
    return None


def create_classifier(model_root, device):
    from paddleocr import DocImgOrientationClassification
    folder = Path(model_root)/MODEL_NAME
    kwargs = dict(model_name=MODEL_NAME, device=device.replace("cuda:", "gpu:"))
    if (folder/"inference.pdiparams").is_file():
        kwargs["model_dir"] = str(folder)
    return DocImgOrientationClassification(**kwargs)


def classify(classifier, image, folder):
    preview=image.copy()
    try:
        preview.thumbnail((POLICY["preview_max_side"],POLICY["preview_max_side"]))
        path=folder/"orientation_preview.png"
        preview.save(path)
        result=list(classifier.predict(input=str(path),batch_size=1))
        if len(result)!=1:
            raise ValueError("方向分类应返回一个结果")
        saved=folder/"orientation.json"
        result[0].save_to_json(str(saved))
        data=json.loads(saved.read_text(encoding="utf-8"))
        data=data.get("res",data)
        angle,score=int(data["label_names"][0]),float(data["scores"][0])
        if angle not in (0,90,180,270):
            raise ValueError("方向分类角度非法")
        return dict(rotation_ccw=angle,score=score,source="attachment_classifier")
    finally:
        preview.close()


def quality(blocks):
    usable=[b for b in blocks if b["text"].strip()]
    chars=sum(len(b["text"].strip()) for b in usable)
    score=sum(float(b.get("score") or 0)*len(b["text"].strip()) for b in usable)/max(1,chars)
    weak=len(usable)<POLICY["min_blocks"] or chars<POLICY["min_characters"] or score<POLICY["min_mean_score"]
    # A sideways line can receive high recognition confidence while its box
    # is vertical. Prefer coherent horizontal text before mean confidence;
    # watermark fragments must not rotate a readable document by 90 degrees.
    horizontal=0
    for b in usable:
        poly=b.get("polygon") or []
        if not poly:continue
        w=max(x for x,y in poly)-min(x for x,y in poly)
        h=max(y for x,y in poly)-min(y for x,y in poly)
        if len(b["text"].strip())>=4 and (b.get("score") or 0)>=.8 and w>2*h:
            horizontal+=min(len(b["text"].strip()),40)*b["score"]
    return dict(weak=weak,mean_score=score,blocks=len(usable),characters=chars,
                horizontal_text_support=horizontal,rank=horizontal+score)


def retry_reason(passes, size):
    from .paddle_spatial_resources import role_of
    best=max(passes,key=lambda v:v["quality"]["rank"])
    if (role_of(best['blocks'])=='quality_certificate'
            and best['quality'].get('horizontal_text_support',0)>80):
        return None  # coherent upright text; dense noise needs cleanup, not rotation
    if all(p["quality"]["weak"] for p in passes):
        return "weak_whole_page_ocr"
    # Geometry-only v3 check. This does not OCR a crop or alter the matcher.
    # Recognized heat/plate anchors with incomplete pairs can indicate tags
    # pasted in different directions on an otherwise readable delivery page.
    from .paddle_attachment_resources import anchor_page
    best=max(passes,key=lambda v:v["quality"]["rank"])
    if role_of(best["blocks"]) in ("quality_certificate","excluded"):
        return None
    text="".join(b["text"] for p in passes for b in p["blocks"]).upper()
    if not any(t in text for t in ("铭牌","炉号","炉批","批号","轧制","BATCH","HEAT")):
        return None
    result,_=anchor_page(dict(original_pixel_size=size,ocr_passes=passes,
                             rotation_ccw=best["rotation_ccw"]))
    if result["fields"] and any(r["review_status"] not in ("matched","matched_with_notes") for r in result["records"]):
        return "incomplete_nameplate_anchors"
    return None
