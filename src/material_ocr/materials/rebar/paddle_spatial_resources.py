"""Legacy Paddle geometry extraction adapted to the unchanged ledger contract.

Only saved whole-page word boxes are used. No image crop, OCR, or business
association is performed here. Rejected legacy candidates remain auditable.
"""
from copy import deepcopy
from pathlib import Path
import re

from material_ocr.attachment_legacy import attachments as legacy
from material_ocr.attachment_legacy.certificate import certificate_identifiers
from material_ocr.attachment_legacy.layout import box, split_entities
from material_ocr.anchor_v3.matcher import inverse_point
from .glm_experiment import _certificate_text, _plain_delivery, _company_heading

RULES = legacy.load_rules(Path(legacy.__file__).with_name("attachment_rules.json"))
GRADE = r"(?:HRBF|HRB|HPB|RRB|CRB)\d{3,4}[EBF]?"


def evidence(cp, *, blocks=(), role="", **extra):
    return dict(page_no=cp["page_no"], polygon_page=None, precision="text_group",
        method="paddle_legacy_spatial", source_role=role,
        source_pixel_size=cp["original_pixel_size"], render_dpi=cp["dpi"],
        coordinate_system="original_page_pixels_top_left",
        checkpoint_reference=cp.get("checkpoint_reference"),
        checkpoint_sha256=cp.get("checkpoint_sha256"),
        raw_text="\n".join(b["text"] for b in blocks), **extra)


def converted_blocks(cp, view):
    w, h = cp["original_pixel_size"]
    return [dict(deepcopy(b), polygon_ocr=deepcopy(b["polygon"]),
        polygon_page=[list(inverse_point(p, view["rotation_ccw"], w, h))
                      for p in b["polygon"]]) for b in view["blocks"]]


def parsing_view(cp):
    from .paddle_attachment_orientation import quality
    angle=cp.get('rotation_ccw',cp['ocr_passes'][0]['rotation_ccw'])
    primary=next(v for v in cp['ocr_passes'] if v['rotation_ccw']==angle)
    choices=[primary]+[v for v in cp['ocr_passes'] if v.get('detail_retry')=='certificate_text'
                       and v['rotation_ccw']==angle]
    return max(choices,key=lambda v:quality(v['blocks'])['rank'])


def role_of(blocks):
    text = "\n".join(b["text"] for b in blocks)
    compact = legacy.compact_text(text).upper()
    # A footer saying "提出检测报告" is not a report title.
    if any(len(legacy.compact_text(b["text"])) <= 28 and
           re.fullmatch(r".{0,12}(?:进场检验记录|进场数量清单|报审表|检测报告|检验报告)(?:表)?",
                        legacy.compact_text(b["text"])) for b in blocks):
        return "excluded"
    if _certificate_text(text) or "QUALITYCERTIFICATE" in compact:
        return "quality_certificate"
    if any(x in compact for x in ("送货单", "发货单", "供货单", "送货通知", "配送单")):
        return "delivery_note"
    # Do not allow generic BATCH NO / HEAT NO alone to classify a plate as a certificate.
    inferred = legacy.classify_page_text(text, rules=RULES)
    return inferred if inferred != "quality_certificate" else "unknown"


def safe_observations(cp, regions, field):
    """Keep literal, located values; reject unbounded legacy neighbour guesses."""
    found = []
    for region in regions:
        for item in region.get("extracted_fields", {}).get(field, []):
            ev = deepcopy(item.get("evidence", {}))
            poly = ev.get("polygon_page")
            if not poly:
                continue
            value = item.get("normalized_value") or item["value"]
            if field in ("heat_lot_no", "certificate_no"):
                value = legacy.normalize_identifier(value)
            method = item.get("extraction_method", "labelled")
            if method == "spatial_label_neighbor":
                label_poly = ev.get("label_polygon_page")
                if not label_poly:
                    continue
                # Work in original coordinates, using the short edge as text height.
                import math
                h = max(1, min(math.dist(label_poly[i], label_poly[(i+1) % 4]) for i in range(4)))
                gap = min(math.dist(a, b) for a in label_poly for b in poly)
                if gap > 12*h:
                    continue
                # An inline value is authoritative for that label. Do not also
                # take a random neighbouring number from an adjacent column.
                label = ev.get("label", "")
                inline = legacy.extract_attachment_fields(label, rules=RULES).get(field, [])
                accepted, _ = legacy.validate_attachment_fields({field:inline}, region["document_type"])
                if accepted.get(field):
                    continue
            ev = {**evidence(cp, role=region["document_type"]), **ev,
                  "method":"paddle_legacy_spatial", "extraction_method":method}
            observed = next((b for b in region["blocks"] if b.get("polygon_page") == poly), None)
            if observed:
                ev.update(raw_text=observed["text"], ocr_score=observed.get("score"),
                          confidence_known=observed.get("score") is not None)
            found.append(dict(value=value, evidence=ev, row_id=region.get("row_id"),
                              extraction_method=method))
    unique = {}
    for item in found:
        key = (str(item["value"]), item["row_id"], str(item["evidence"].get("polygon_page")))
        unique[key] = item
    return list(unique.values())


def fields_values(cp, regions, field):
    return list(dict.fromkeys(str(x["value"]) for x in safe_observations(cp, regions, field)))


def certificate(cp, blocks, regions, entity_id):
    # The legacy explicit-number locator sees the FULL entity, including the
    # title/header above its table. It never receives only table-row blocks.
    index = dict(pdf_sha256=cp.get("pdf_sha256"), pages=[dict(page_no=cp["page_no"],
        source_image=cp.get("source_image"), rotation_ccw=cp.get("rotation_ccw",0),
        regions=[dict(region_id=f"p{cp['page_no']:03d}_full", blocks=blocks,
                      text="\n".join(b["text"] for b in blocks))])])
    located = certificate_identifiers(index, {cp["page_no"]})
    # Damaged Chinese number labels still end in 编号. Require a compact,
    # letter-prefixed serial in the header, excluding licence/template codes.
    table_top=min([box(b)[1] for b in blocks if re.match(r'规格|炉批号|牌号炉批号',legacy.compact_text(b['text']))] or [float('inf')])
    for label in blocks:
        raw=legacy.compact_text(label['text'])
        if box(label)[1]>=table_top or not re.fullmatch(r'[\u4e00-\u9fff]{0,2}编号[:：]?',raw):continue
        x0,y0,x1,y1=box(label);h=max(1,y1-y0)
        options=[b for b in blocks if re.fullmatch(r'[A-Z]{1,4}\d{6,}(?:\.\d+)?',legacy.compact_text(b['text']),re.I)
                 and -.2*h<box(b)[0]-x1<12*h and abs((box(b)[1]+box(b)[3]-y0-y1)/2)<h*.8]
        if len(options)==1:
            b=options[0];value=legacy.compact_text(b['text'])
            if value not in {v['value'] for v in located}:
                located.append(dict(value=value,method='damaged_certificate_number_label',
                    evidence=dict(page_no=cp['page_no'],polygon_page=b['polygon_page'],
                        raw_text=b['text'],label_raw_text=label['text'],ocr_score=b.get('score'))))
    numbers = [dict(value=x["value"], evidence={**evidence(cp, role="certificate"),
        **x["evidence"], "method":"paddle_legacy_certificate_label",
        "extraction_method":x["method"]}) for x in located]
    # Also preserve legacy aliases not included in the specialized locator.
    generic = [x for x in safe_observations(cp, regions, "certificate_no")
               if legacy.compact_label(x["evidence"].get("label",""))!="CERTIFICATE"]
    numbers += [x for x in generic if x["value"] not in {n["value"] for n in numbers}]
    rows = [r for r in regions if r.get("layout_role") == "table_row"]
    heats = safe_observations(cp, rows, "heat_lot_no")
    if not heats:
        heats = safe_observations(cp, regions, "heat_lot_no")
    certs = list(dict.fromkeys(x["value"] for x in numbers))
    lots = list(dict.fromkeys(x["value"] for x in heats))
    issues = []
    if not certs:
        issues.append("certificate_number_unreadable")
    if len(certs) > 1:
        issues.append("certificate_number_view_conflict")
    if not lots:
        issues.append("certificate_heat_unreadable")
    return dict(candidate_id=entity_id, page_no=cp["page_no"],
        certificate_numbers=certs, heat_lots=lots,
        certificate_number_observations=numbers, heat_observations=heats,
        grade_candidates=sorted(set(re.findall(GRADE, legacy.compact_text(
            "\n".join(b["text"] for b in blocks)).upper()))),
        grade_ocr_candidates=[], issues=issues,
        evidence=evidence(cp, blocks=blocks, role="certificate"),
        evidence_views=[x["evidence"] for x in numbers+heats])


def labelled_supplier(cp, blocks):
    """Explicit supplier cells outrank the document issuer/header company."""
    observations=[]
    seen_label=False
    table_top=min([box(b)[1] for b in blocks if legacy.compact_text(b['text']) in
                  ('规格/材质','规格材质','规格型号','规格','品名')] or [float('inf')])
    for b in blocks:
        if box(b)[1]>table_top:continue  # footer signature/stamp is not a header value
        match=re.match(r"^(?:供应商|供应单位|供货单位)[:：]?(.*)$",legacy.compact_text(b["text"]))
        if not match:
            continue
        seen_label=True
        inline=re.split(r"联系人|联系电话|电话|客户|收货单位|地址",match[1],maxsplit=1)[0].strip(":：;；,，")
        company_pattern=r"[\u4e00-\u9fffA-Za-z0-9（）()·]{3,}(?:公司|工厂|钢厂|经营部|商行|集团)"
        values=[(inline,b)] if re.fullmatch(company_pattern,inline) else []
        if not match[1]:
            x0,y0,x1,y1=box(b);h=max(1,y1-y0)
            nearby=[]
            for v in blocks:
                a,c,d,e=box(v)
                if -.2*h<=a-x1<12*h and abs((c+e-y0-y1)/2)<.75*h:
                    if re.fullmatch(company_pattern,legacy.compact_text(v["text"])):
                        nearby.append((a-x1,v))
            if nearby:
                v=min(nearby,key=lambda item:item[0])[1]
                values=[(legacy.compact_text(v["text"]),v)]
        for value,v in values:
            observations.append(dict(value=value,evidence={**evidence(cp,blocks=[b,v] if b is not v else [v],role="delivery"),
                "polygon_page":v["polygon_page"],"precision":"word_block",
                "ocr_score":v.get("score"),"confidence_known":v.get("score") is not None,
                "extraction_method":"explicit_supplier_label"}))
    if not seen_label:
        return None
    values=list(dict.fromkeys(o["value"] for o in observations))
    return dict(value=values[0] if len(values)==1 else "",candidates=observations,
        evidence=observations[0]["evidence"] if len(values)==1 else evidence(cp,role="delivery"),
        issues=[] if len(values)==1 else ["supplier_label_conflict" if values else "supplier_label_value_unreadable"])


def delivery(cp, blocks, regions, entity_id):
    ordered = sorted(blocks, key=lambda b:(box(b)[1],box(b)[0]))
    text = "\n".join(b["text"] for b in ordered)
    local_cp = dict(cp, raw_text=text)
    # Keep the existing top-company semantic policy; actual box evidence is
    # attached below. No data is borrowed from a different form or page.
    heading_text = re.split(r"送货单|发货单|供货单|(?:货物)?配送单", text, maxsplit=1)[0]
    heading = _company_heading(local_cp, heading_text, "delivery")
    if heading.get("value") and not re.search(r"公司|工厂|钢厂|经营部|商行|集团$",heading["value"]):
        heading["issues"].append("delivery_heading_is_not_identified_supplier")
        heading["value"]=""
    heading["evidence"] = evidence(cp, blocks=ordered, role="delivery")
    for c in heading.get("candidates", []):
        related = [b for b in blocks if legacy.compact_text(c["value"]) in legacy.compact_text(b["text"])]
        c["evidence"] = evidence(cp, blocks=related, role="delivery")
        if len(related) == 1:
            c["evidence"].update(polygon_page=related[0]["polygon_page"], precision="word_block",ocr_score=related[0].get('score'))
    located=[c['evidence'] for c in heading.get('candidates',[]) if c.get('value')==heading.get('value')]
    if len(located)==1:heading['evidence']=located[0]
    explicit=labelled_supplier(cp,blocks)
    if explicit is not None:
        heading=explicit
    numbers = []
    number_evidence = []
    for b in blocks:
        m = re.search(r"(?:\bNO[.:：]?|(?:送货|配送)单号[码]?[:：]?|单据编号[:：]?)\s*([A-Z0-9][A-Z0-9/-]*\d[A-Z0-9/-]*)",
                      legacy.normalize_text(b["text"]), re.I)
        candidates = [(m[1], b)] if m else []
        if re.fullmatch(r"(?:NO[.:：]?|(?:送货|配送)单号[码]?[:：]?|单据编号[:：]?)",
                        legacy.compact_text(b["text"]), re.I):
            x0,y0,x1,y1=box(b); h=max(1,y1-y0)
            adjacent=[]
            for v in blocks:
                a,c,d,e=box(v)
                if a >= x1-h*.25 and a-x1 < 12*h and abs((c+e-y0-y1)/2)<h*.75:
                    if re.fullmatch(r"[A-Z0-9/-]*\d[A-Z0-9/-]*",legacy.compact_text(v["text"]),re.I):
                        adjacent.append((a-x1,v))
            if adjacent:
                v=min(adjacent,key=lambda x:x[0])[1]
                candidates.append((legacy.compact_text(v["text"]),v))
        for value,v in candidates:
            numbers.append(value)
            number_evidence.append(dict(value=value,evidence={**evidence(cp,blocks=[v],role="delivery"),
                "polygon_page":v["polygon_page"],"precision":"word_block","ocr_score":v.get("score")}))
    numbers=list(dict.fromkeys(numbers))
    rows=[]
    for r in regions:
        if r.get("layout_role") != "table_row":
            continue
        grade=fields_values(cp,[r],"grade")
        diameter=fields_values(cp,[r],"diameter_mm")
        if len(grade)>1 or len(diameter)>1:
            continue
        if grade or diameter:
            rows.append(dict(grade=grade[0] if grade else "",diameter_mm=diameter[0] if diameter else "",
                logical_row=r["physical_row_index"], issues=[],
                evidence=evidence(cp,blocks=r["blocks"],role="delivery",row_id=r["row_id"])))
    # Preserve the existing explicit one-line delivery syntax as a fallback,
    # only inside this delivery entity. Certificates never use this text parser.
    if not rows:
        for parsed in _plain_delivery(local_cp):
            rows.extend(parsed["rows"])
        for row in rows:
            row["evidence"]=evidence(cp,blocks=blocks,role="delivery")
    issues=list(heading["issues"])
    if len(numbers)!=1:
        issues.append("delivery_number_conflict" if numbers else "delivery_number_unreadable")
    amounts={}
    for b in blocks:
        text=legacy.compact_text(b['text'])
        if re.fullmatch(r'\d{1,3}\.\d{2,3}',text) and float(text)>0 and (b.get('score') or 0)>=.8:
            amounts.setdefault(text,[]).append(dict(value=text,evidence={**evidence(cp,blocks=[b],role='delivery'),
                'polygon_page':b['polygon_page'],'ocr_score':b.get('score')}))
    return dict(candidate_id=entity_id,page_no=cp["page_no"],supplier=heading["value"],
        supplier_heading=heading,delivery_no=numbers[0] if len(numbers)==1 else "",
        repeated_quantity_observations=[v for values in amounts.values() if len(values)>=2 for v in values],
        delivery_number_candidates=numbers,delivery_number_observations=number_evidence,
        rows=rows,issues=issues,evidence=evidence(cp,blocks=blocks,role="delivery"))


def spatial_resources(checkpoints):
    output=dict(records=[],nameplates=[],deliveries=[],certificates=[],issues=[],pages=[])
    for cp in checkpoints:
        if cp.get("orientation",{}).get("review_required"):
            output["issues"].append(dict(code="attachment_orientation_or_ocr_review",
                page_no=cp["page_no"],reason="整页方向重试后识别仍弱，请检查原图及方向诊断"))
        # Select one coherent page orientation for certificate/delivery parsing;
        # v3 separately fuses all OCR passes for arbitrarily oriented nameplates.
        angle=cp.get("rotation_ccw",cp["ocr_passes"][0]["rotation_ccw"])
        view=parsing_view(cp)
        blocks=converted_blocks(cp,view)
        role=role_of(blocks)
        from .multiform_resources import split_attachment_forms
        entities=split_attachment_forms(blocks) if role!="excluded" else []
        entities=entities or [(role,blocks)]
        audit=[]
        for i,(hint,entity_blocks) in enumerate(entities):
            actual=role_of(entity_blocks)
            if actual=="excluded":
                continue
            # Explicit geometric entity split takes precedence over broad aliases.
            entity_role=hint if hint in ("delivery_note","quality_certificate","nameplate") else actual
            eid=f"paddle-spatial:p{cp['page_no']:03d}:entity{i+1}"
            regions=legacy.build_regions(entity_blocks,page_no=cp["page_no"],
                source_image=cp.get("source_image"),rotation_ccw=angle,rules=RULES,
                document_type=entity_role,region_id=eid)
            audit.extend(regions)
            if entity_role=="quality_certificate":
                output["certificates"].append(certificate(cp,entity_blocks,regions,eid))
            elif entity_role=="delivery_note":
                item=delivery(cp,entity_blocks,regions,eid)
                from .paddle_delivery_number import recover_number
                item=recover_number(item,cp)
                from .multiform_resources import delivery_rows
                recovered=delivery_rows(cp,entity_blocks)
                if not recovered:
                    from .paddle_attachment_detail import delivery_header_blocks
                    supplemented = delivery_header_blocks(cp, entity_blocks)
                    # Keep supplemental headers within this entity's band,
                    # including mixed pages with a plate above the delivery.
                    lo=min(box(b)[1] for b in entity_blocks);hi=max(box(b)[3] for b in entity_blocks)
                    supplemented=[b for b in supplemented if lo<=sum(box(b)[1::2])/2<=hi]
                    recovered = delivery_rows(cp, supplemented)
                    for row in recovered:
                        row["issues"].append("whole_page_header_recovery_requires_review")
                        row["header_recovery"] = "whole_page_detail; original row values retained"
                if recovered:
                    item["legacy_rows"]=item["rows"]
                    item["rows"]=recovered
                from .paddle_delivery_view import recover_delivery
                item=recover_delivery(item,cp,entity_blocks)
                output["deliveries"].append(item)
        if role=="excluded" and any(t in cp.get("raw_text","") for t in ("进场数量清单","进场检验记录")):
            output["issues"].append(dict(code="entry_on_unscreened_page",page_no=cp["page_no"],
                reason="附件页含进场表标题，请核对上游筛页"))
        output["pages"].append(dict(page_no=cp["page_no"],full_page_available=True,
            document_type=role,rotation_ccw=angle,spatial_regions=audit))
    from .paddle_delivery_view import resolve_supplier_variants
    resolve_supplier_variants(output['deliveries'])
    return output
