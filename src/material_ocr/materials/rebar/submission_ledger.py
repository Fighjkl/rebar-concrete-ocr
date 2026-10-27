"""Field-local acceptance rules for classified submission forms in the ledger."""
import re
from .upstream_adapter import submission_context as raw_submission_context

MINIMUM_SCORE = 0.80
FIELD_COLUMNS = {
    "document_no": "文件编号", "material_name": "材料名称",
    "supply_date": "供货日期与工程材料报审签认日期", "use_location": "使用部位",
}


def submission_context(checkpoint, new_field):
    # Keep the original right-hand number crop and its measured coordinates.
    fields = raw_submission_context(checkpoint, new_field)
    form = checkpoint["submission_form"]
    for name, column in FIELD_COLUMNS.items():
        source = form["fields"].get(name, {})
        issues = list(source.get("issues", []))
        for issue in form.get("issues", []):
            prefix, sep, reason = issue.partition(":")
            if sep and prefix in FIELD_COLUMNS:
                if prefix == name:
                    issues.append(reason)
            elif issue != "body_context_anchors_incomplete" or name != "document_no":
                issues.append(issue)
        score = source.get("min_score")
        soft = {"document_no_visual_review_required"}
        if isinstance(score, (int, float)) and score >= MINIMUM_SCORE:
            soft.add("low_ocr_confidence")
            # A damaged surrounding phrase must not reject a clear, located
            # value independently repeated in the body OCR. Keep unfamiliar
            # errors, malformed dates and conflicting values actionable.
            body=re.sub(r'\s+','',form.get('fields',{}).get('body_context',{}).get('raw_text',''))
            lines=source.get('lines') or str(source.get('raw_text') or '').splitlines()
            parts=[re.sub(r'\s+','',line) for line in lines if line.strip()]
            if (name!='document_no' and source.get('polygon_page') and parts
                    and all(part in body for part in parts)):
                soft.add('body_context_anchors_incomplete')
        field = fields[column]
        dismissed = [issue for issue in issues if issue in soft and field.get("value")]
        remaining = [issue for issue in issues if issue not in dismissed]
        fields[column] = new_field(field["value"], field["page"], field["evidence"], remaining)
        fields[column]["review_notes"] = list(dict.fromkeys(dismissed))
        fields[column]["review_required"] = bool(remaining or not field["value"])
    return fields


def classification_field(checkpoints, new_field):
    classified = [cp for cp in checkpoints if cp.get("upstream_role") in ("entry", "submission")]
    if not classified:
        return None
    submissions = [cp for cp in classified if cp["upstream_role"] == "submission"]
    # "No" is a classification result, not text claimed to appear on an entry form.
    evidence = [dict(page_no=cp["page_no"], precision="page", method="page_classification",
                     source_role="submission" if submissions else "classification",
                     checkpoint_reference=cp.get("checkpoint_reference"),
                     checkpoint_sha256=cp.get("checkpoint_sha256"),
                     raw_text="页面分类找到送审表" if submissions else "页面分类未找到送审表")
                for cp in (submissions or classified)]
    field = new_field("是" if submissions else "否",
                      min(cp["page_no"] for cp in submissions) if submissions else None, evidence)
    field.update(review_required=False, found_in_ocr=True,
                 derived_from="page_classification")
    return field
