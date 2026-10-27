"""Whole-page Paddle text + unmodified v3 anchor pairs -> existing ledger input.

No image crops or physical-card rectangles are produced here. Ambiguous pairs
remain in the review report, not in the set of matchable nameplate resources.
"""
from copy import deepcopy
import re

from material_ocr.anchor_v3.matcher import (
    extract_fields, restore_field, fuse_fields, match_fields, inverse_point,
    geometry, local, angle_gap,
)
from .glm_experiment import _certificate_text
from .paddle_spatial_resources import spatial_resources
from . import paddle_anchor_recovery as recovery


def anchor_page(checkpoint, known_heats=()):
    """Recover additional formats/views before v3's global anchor pairing."""
    width, height = checkpoint["original_pixel_size"]
    fields, blocks = [], []
    primary=checkpoint.get("rotation_ccw",checkpoint["ocr_passes"][0]["rotation_ccw"])
    for view in sorted(checkpoint["ocr_passes"],key=lambda v:v["rotation_ccw"]!=primary):
        angle = view["rotation_ccw"]
        source = deepcopy(view["blocks"])
        for block in source:
            block["rotation"] = angle
        fields.extend(restore_field(f, angle, width, height)
                      for f in recovery.extract(source, known_heats))
        for block in source:
            block["polygon"] = [list(inverse_point(p, angle, width, height)) for p in block["polygon"]]
            block["duplicate_secondary_view"]=any(b["rotation"]!=angle and
                recovery.overlap(b["polygon"],block["polygon"])>.65 for b in blocks)
            blocks.append(block)
    return match_fields(recovery.merge_views(fields),
        config={"max_horizontal_h":20.0,"max_cost":20.0}), blocks


def nearby_metadata(blocks, result):
    """Assign observed grade/company text exclusively; never fill across pairs."""
    fields = {f["id"]: f for f in result["fields"]}
    targets = [(r["id"], fields.get(r["batch_id"]) or fields.get(r["spec_id"]))
               for r in result["records"]]
    assigned = {r["id"]: {"grade": [], "manufacturer": []} for r in result["records"]}
    ambiguous = {r["id"]: set() for r in result["records"]}
    for block in blocks:
        if block.get("duplicate_secondary_view"):
            continue
        text = re.sub(r"\s+", "", block["text"]).upper()
        grades = re.findall(r"(?:HRBF|HRB|HPB|RRB|CRB)\d{3,4}[EBF]?", text)
        # Do not treat labelled purchaser/supplier as the plate manufacturer.
        companies = ([] if re.search(r"购货|收货|供应|供货|需方|客户|建设单位|监理单位", text) else
                     re.findall(r"[\u4e00-\u9fffA-Z（）()·]{3,}(?:有限公司|有限责任公司|钢厂|钢铁集团)", text))
        for kind, values, height_limit in (("grade", grades, 6), ("manufacturer", companies, 18)):
            if not values:
                continue
            g = geometry(block["polygon"])
            candidates = []
            for rid, field in targets:
                if not field:
                    continue
                base = field["g"]
                h = max(base["height"], g["height"], 1)
                x, y = local(g["center"], base)
                left = x - g["width"] / 2 + base["width"] / 2
                # A plate's grade label may be below its heat label. Preserve
                # exclusive nearest-pair ownership, rather than assuming every
                # vendor prints the grade above the heat.
                bottom_limit = 6 if kind == "grade" or re.search(r"生产厂|制造厂|生产单位",text) else .7
                if angle_gap(base, g) > 18 or not -height_limit <= y / h <= bottom_limit:
                    continue
                if abs(left) / h > (5 if kind == "manufacturer" else 10):
                    continue
                candidates.append((abs(y) / h + abs(left) / h, rid))
            candidates.sort()
            if not candidates:
                continue
            if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < 1.5:
                for _, rid in candidates[:2]:
                    ambiguous[rid].add(kind)
                continue
            rid = candidates[0][1]
            if block.get("score") is None or block["score"] < .8 or len(set(values)) != 1:
                ambiguous[rid].add(kind)
            else:
                assigned[rid][kind].append(dict(value=values[0], block=block))
    output = {}
    for rid, kinds in assigned.items():
        output[rid] = {}
        for kind, observations in kinds.items():
            values = sorted({v["value"] for v in observations})
            valid = len(values) == 1 and kind not in ambiguous[rid]
            output[rid][kind] = dict(value=values[0] if valid else "", candidates=values,
                blocks=[v["block"] for v in observations],
                issues=[] if valid else [kind + "_local_ownership_unresolved"])
    return output


def block_evidence(checkpoint, block, role="nameplate"):
    return dict(page_no=checkpoint["page_no"], polygon_page=deepcopy(block["polygon"]),
        source_pixel_size=checkpoint["original_pixel_size"], render_dpi=checkpoint["dpi"],
        coordinate_system="original_page_pixels_top_left", precision="word_block",
        method="paddle_whole_page_anchor_v3", source_role=role, raw_text=block["text"],
        ocr_score=block.get("score"), confidence_known=block.get("score") is not None,
        checkpoint_reference=checkpoint["checkpoint_reference"],
        checkpoint_sha256=checkpoint["checkpoint_sha256"])


def nameplate_resources(checkpoint, known_heats=()):
    result, blocks = anchor_page(checkpoint, known_heats)
    metadata = nearby_metadata(blocks, result)
    by_id = {f["id"]: f for f in result["fields"]}
    accepted = []
    for pair in result["records"]:
        # The existing two-route matcher assumes candidate input is usable.
        # Gate at this adapter, without changing either business route.
        eligible = bool(pair.get("specification") and pair.get("batch_number") and
                        "pairing_ambiguous" not in pair["issues"])
        pair["eligible_for_business_matching"] = eligible
        if not eligible:
            continue
        info = metadata[pair["id"]]
        ids = set()
        for key in ("spec_id", "batch_id"):
            f = by_id.get(pair[key], {})
            for name in ("block_ids", "anchor_source_block_ids", "value_source_block_ids"):
                ids.update(f.get(name, []))
        sources = [b for b in blocks if b["id"] in ids]
        ev = dict(page_no=checkpoint["page_no"], polygon_page=None, precision="text_group",
            method="paddle_whole_page_anchor_v3", source_role="nameplate",
            kind="constrained_anchor_pair", physical_card_boundary_verified=False,
            source_pixel_size=checkpoint["original_pixel_size"], render_dpi=checkpoint["dpi"],
            checkpoint_reference=checkpoint["checkpoint_reference"],
            checkpoint_sha256=checkpoint["checkpoint_sha256"],
            raw_text="\n".join(b["text"] for b in sources))
        heading = deepcopy(info["manufacturer"])
        heading["evidence"] = [block_evidence(checkpoint, b) for b in heading["blocks"]]
        issues = pair["issues"] + info["grade"]["issues"] + heading["issues"] + [
            "铭牌按整页文字锚点配对，未检测实体卡片边界；请核对原图。"]
        plate = dict(candidate_id=f"paddle-anchor:p{checkpoint['page_no']:03d}:{pair['id']}",
            page_no=checkpoint["page_no"], scope="paddle_anchor_pair",
            grade=info["grade"]["value"], diameter_mm=format(pair["specification"]["diameter_mm"], "g"),
            heat_lots=[pair["batch_number"]], manufacturer=heading["value"], manufacturer_heading=heading,
            grade_evidence=info["grade"], evidence=ev, issues=issues, spatial_unresolved=False,
            physical_card_boundary_verified=False, anchor_pairing_result=deepcopy(pair),
            evidence_views=[block_evidence(checkpoint, b) for b in sources])
        accepted.append(plate)
    return accepted, result


def assemble_attachments(checkpoints):
    resources = spatial_resources(checkpoints)
    from .multiform_resources import enrich_certificates, enrich_nameplates
    enrich_certificates(resources, checkpoints)
    known_heats={h for c in resources["certificates"] for h in c["heat_lots"]}
    resources["nameplates"] = []
    reports = {}
    for cp in checkpoints:
        text = re.sub(r"\s+", "", cp["raw_text"])
        # A certificate contains size/lot columns too; it is not a nameplate.
        if _certificate_text(cp["raw_text"]) or any(
                title in text for title in ("进场数量清单", "进场检验记录", "检测报告", "检验报告", "报审表")):
            continue
        plates, report = nameplate_resources(cp, known_heats)
        resources["nameplates"].extend(plates)
        if report["fields"]:
            reports[cp["page_no"]] = report
            for pair in report["records"]:
                if not pair["eligible_for_business_matching"]:
                    resources["issues"].append(dict(code="anchor_pair_requires_review", page_no=cp["page_no"],
                        pair_id=pair["id"], reason="规格/炉批号配对或字段值需要复核，未作为确定铭牌参与业务匹配",
                        anchor_review_status=pair["review_status"]))
    # Keep provenance honest while retaining the old parsers and all raw text.
    def mark(value):
        if isinstance(value, dict):
            if value.get("method") == "glm_native_logical_output":
                value.update(method="paddle_whole_page_text", inference_model="local-paddle")
            for item in value.values():
                mark(item)
        elif isinstance(value, list):
            for item in value:
                mark(item)
    enrich_nameplates(resources)
    mark(resources)
    return resources, reports
