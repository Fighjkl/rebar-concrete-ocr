"""Recover text geometry across multi-form attachments. No new OCR or crops.

Every added value retains its actual text boxes. Certificate corroboration may
complete plate metadata, but never edits the plate's observed heat/specification.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re
from statistics import median
from material_ocr.attachment_legacy.layout import box

GRADE = r"(?:HRBF|HRB|HPB|RRB|CRB)\d{3,4}[EBF]?"


def compact(text):
    return re.sub(r"\s+", "", str(text)).upper()


def center(b):
    x,y,r,t=box(b)
    return (x+r)/2,(y+t)/2


def height(b):
    return max(1,box(b)[3]-box(b)[1])


def number(text):
    text=compact(text)
    hit=re.fullmatch(r"(\d+(?:\.\d+)?)(?:T|吨)?",text)
    return hit[1] if hit else ""


def decimal(text):
    try:
        return Decimal(str(text)) if text not in ("",None) else None
    except InvalidOperation:
        return None


def diameter(text):
    # A numeral within the explicitly located specification column. Non-numeric
    # prefixes remain review evidence; no digit is changed or dropped.
    hit=re.fullmatch(r"\D*(\d{1,2}(?:\.\d+)?)\D*",compact(text))
    return format(float(hit[1]),"g") if hit and 4<=float(hit[1])<=80 else ""


def delivery_specification(text):
    """Separate diameter and length without repairing omitted separators."""
    text=re.sub(GRADE,"",compact(text))
    hit=re.fullmatch(r"[ΦФØ∅⌀]?(\d{1,2}(?:\.\d+)?)(?:MM)?[X×*+](\d+(?:\.\d+)?)(MM|M|米|毫米)?",text)
    if not hit or not 4<=float(hit[1])<=80:
        return dict(diameter_mm=diameter(text),length_mm=None)
    length=float(hit[2])
    # Bare 9/12 in the specification column is conventional metre notation.
    metres=hit[3] in ("M","米") or (not hit[3] and length<=30)
    return dict(diameter_mm=format(float(hit[1]),"g"),length_mm=length*(1000 if metres else 1),
                length_unit_source="explicit" if hit[3] else "conventional_rebar_length")


def delivery_weight_header(text):
    return bool(re.fullmatch(r"(?:重量|数量|发货数(?:量)?|配送数(?:量)?)(?:[（(]?(?:吨|T)[)）]?)?",compact(text)))


def delivery_weight_anchor(block, blocks):
    if delivery_weight_header(block["text"]):
        return True
    # A damaged 发货数 heading can still locate a weight column when its
    # separately detected tonnes unit corroborates the meaning and position.
    # Preserve the literal heading; do not generalize 发货款 to mean quantity.
    if not re.fullmatch(r"发货.{1,2}",compact(block["text"])):
        return False
    return any(re.fullmatch(r"[（(]?(?:吨|T)[)）]?",compact(unit["text"]))
               and (unit.get("score") or 0)>=.60
               and abs(center(unit)[0]-center(block)[0])<height(block)
               and 0<center(unit)[1]-center(block)[1]<1.5*height(block)
               for unit in blocks)


def observation(cp, blocks, method, **extra):
    from .paddle_spatial_resources import evidence
    return dict(evidence(cp,blocks=blocks,role="attachment"),method=method,
        blocks=[dict(text=b["text"],polygon_page=b["polygon_page"],score=b.get("score"))
                for b in blocks], **extra)


def split_delivery_forms(blocks):
    titles=sorted([b for b in blocks if re.fullmatch(
                   r"(?:\d{4}年)?(?:送货单|发货单|供货单|送货通知单|(?:货物)?配送单)",
                   compact(b["text"]))],key=lambda b:center(b)[1])
    if len(titles)<2:
        return []
    # Only stacked titles. Side-by-side layouts must not be mixed into bands.
    if any(abs(center(b)[0]-center(titles[0])[0])>5*max(height(b),height(titles[0])) for b in titles):
        return []
    cuts=[center(b)[1]-3*height(b) for b in titles[1:]]
    bounds=[-float("inf")]+cuts+[float("inf")]
    return [("delivery_note",[b for b in blocks if lo<=center(b)[1]<hi])
            for lo,hi in zip(bounds,bounds[1:])]


def split_attachment_forms(blocks):
    """Keep the delivery issuer immediately above its title inside that form."""
    from material_ocr.attachment_legacy.layout import split_entities
    entities=split_delivery_forms(blocks) or split_entities(blocks)
    for i,(role,items) in enumerate(entities):
        if role!='delivery_note' or i==0:continue
        titles=[b for b in items if re.fullmatch(r'(?:送货|发货|供货|配送)单',compact(b['text']))]
        if not titles:continue
        title=min(titles,key=lambda b:center(b)[1])
        preceding=entities[i-1][1]
        headings=[b for b in preceding if re.fullmatch(r'[\u4e00-\u9fffA-Z（）()·]{3,}(?:有限公司|有限责任公司)',compact(b['text']))
                  and 0<center(title)[1]-center(b)[1]<3*max(height(b),height(title))]
        if len(headings)!=1:continue
        heading=headings[0]
        preceding.remove(heading);items.append(heading)
    return entities


def same_row(a,b,factor=.8):
    return abs(center(a)[1]-center(b)[1])<=factor*max(height(a),height(b))


def right_value(label,blocks,parser):
    hits=[]
    for b in blocks:
        val=parser(b["text"])
        gap=box(b)[0]-box(label)[2]
        if val and -.2*height(label)<=gap<=10*height(label) and same_row(label,b):
            hits.append((gap,b,val))
    return min(hits,key=lambda x:x[0])[1:] if hits else (None,"")


def delivery_rows(cp,blocks):
    headers=[b for b in blocks if compact(b["text"]) in ("规格","规格型号","型号规格","直径","规格/材质","规格材质")]
    rows=[]
    for spec in headers:
        nearby=[b for b in blocks if same_row(spec,b)]
        weights=[b for b in nearby if delivery_weight_header(b["text"])]
        if not weights:
            weights=[b for b in nearby if delivery_weight_anchor(b,blocks)]
        if len(weights)!=1:
            continue
        weight=weights[0]
        bottom=min([box(b)[1] for b in blocks if center(b)[1]>center(spec)[1]+height(spec)
                    and re.match(r"合计|总计|说明",compact(b["text"]))] or [float("inf")])
        anchors=sorted([b for b in blocks if box(spec)[3]<center(b)[1]<bottom
            and abs(center(b)[0]-center(spec)[0])<=max(height(spec)*3,(box(spec)[2]-box(spec)[0])/2)
            and re.search(r"\d",b["text"])],key=lambda b:center(b)[1])
        for b in anchors:
            # Assign each cell to its closest specification row, exclusively.
            # Handwriting boxes may overlap vertically without belonging to both rows.
            rowblocks=[v for v in blocks if same_row(b,v,1.25)
                       and min(anchors,key=lambda a:abs(center(a)[1]-center(v)[1])) is b
                       and box(spec)[3]<center(v)[1]<bottom
                       and (len(v["text"])<3 or box(v)[2]-box(v)[0]>height(v)*.5)]
            vals=[v for v in rowblocks if abs(center(v)[0]-center(weight)[0])<height(weight)*3
                  and not re.fullmatch(r"[（(]?(?:吨|T)[)）]?",compact(v["text"]))]
            grades=[re.search(GRADE,compact(v["text"])) for v in rowblocks]
            grades={g[0] for g in grades if g}
            spec_value=delivery_specification(b["text"])
            qty=number(vals[0]["text"]) if len(vals)==1 else ""
            rows.append(dict(grade=next(iter(grades)) if len(grades)==1 else "",
                **spec_value,quantity_t=qty,quantity_unit="unprinted",
                specification_raw=b["text"],quantity_raw=vals[0]["text"] if len(vals)==1 else "",
                logical_row=len(rows)+1,issues=["handwritten_delivery_row_requires_review"],
                evidence=observation(cp,rowblocks,"paddle_document_header_columns")))
    return rows


def heat_columns(blocks):
    labels=[b for b in blocks if compact(b["text"]).strip(":：.") in
            ("批号","炉批号","炉号","炉罐号","牌号炉批号","HEATNO","BATCHNO")]
    # Vertically stacked 批 / 号 is a table header, not an inline label.
    for a in blocks:
        if compact(a["text"]) not in ("批","炉"):
            continue
        for b in blocks:
            if compact(b["text"])!="号":
                continue
            dx=abs(center(a)[0]-center(b)[0]);dy=center(b)[1]-center(a)[1]
            if dx<height(a) and height(a)<dy<6*height(a):
                labels.append(dict(b,text=a["text"]+"号"))
                break
    # Some mills print both a rolling batch and a smelting heat. The existing
    # ledger route uses the explicit batch column; a second heat column is
    # source evidence, not another candidate batch for the same product.
    batches=[b for b in labels if '批' in compact(b['text']) or 'BATCH' in compact(b['text'])]
    return batches or labels


def enrich_certificates(resources, checkpoints):
    from .paddle_spatial_resources import converted_blocks, parsing_view
    cps={c["page_no"]:c for c in checkpoints}
    for cert in resources["certificates"]:
        cp=cps[cert["page_no"]]
        angle=cp.get("rotation_ccw",cp["ocr_passes"][0]["rotation_ccw"])
        view=parsing_view(cp)
        blocks=converted_blocks(cp,view)
        # Restrict to the already identified certificate entity on mixed pages.
        entity_text=cert["evidence"].get("raw_text","").splitlines()
        blocks=[b for b in blocks if b["text"] in entity_text]
        cert['grade_observations']=[dict(value=g,evidence=observation(cp,[b],
            'paddle_certificate_grade',polygon_page=b['polygon_page'],precision='word_block',
            ocr_score=b.get('score'))) for b in blocks for g in set(re.findall(GRADE,compact(b['text'])))]
        heats=list(cert["heat_lots"])
        for label in heat_columns(blocks):
            if cert['heat_lots'] and not any(t in compact(label['text']) for t in ('批','BATCH')):
                continue  # do not append a separate smelting column to an extracted batch
            for b in blocks:
                value=compact(b["text"])
                if not re.fullmatch(r"[A-Z0-9][A-Z0-9/-]{5,24}",value) or len(re.findall(r"\d",value))<5:
                    continue
                if value.startswith(("HRB","HPB","GB","XK")) or re.fullmatch(r"20\d\d[-/]\d{1,2}[-/]\d{1,2}",value):
                    continue
                dx=abs(center(b)[0]-center(label)[0]);dy=center(b)[1]-center(label)[1]
                if dx>max(height(label)*1.5,(box(label)[2]-box(label)[0])/2) or not 0<dy<18*height(label):
                    continue
                # Require a material-specification value or steel grade on
                # the same data row, avoiding footer identifiers below a column.
                peers=[v for v in blocks if same_row(b,v)]
                spec_heads=[v for v in blocks if compact(v["text"]) in ("规格","直径","DIMENSION")]
                has_spec=any(delivery_specification(v["text"])['diameter_mm'] and any(
                    abs(center(v)[0]-center(h)[0])<height(h)*1.5 and center(h)[1]<center(v)[1]
                    for h in spec_heads) for v in peers)
                if not has_spec and not any(re.search(GRADE,compact(v["text"])) for v in peers):
                    continue
                if value not in heats:
                    heats.append(value)
                    ev=observation(cp,[label,b],"paddle_multiline_heat_column")
                    cert["heat_observations"].append(dict(value=value,evidence=ev))
                    cert["evidence_views"].append(ev)
        cert["heat_lots"]=heats
        if heats:
            cert["issues"]=[i for i in cert["issues"] if i!="certificate_heat_unreadable"]
        # Heading company only; stamps, purchasers and supplier names are excluded.
        titles=[b for b in blocks if "质量证明书" in compact(b["text"]) or compact(b["text"])=="QUALITYCERTIFICATE"]
        cutoff=max([center(b)[1] for b in titles] or [0])
        title_cutoff=cutoff
        table_heads=[b for b in blocks if re.fullmatch(r'规格(?:[*/＊水]长度)?|(?:牌号)?炉批号|化学成[分：:]?.*',compact(b['text']))]
        if table_heads:
            cutoff=max(cutoff,min(box(b)[1] for b in table_heads))
        companies=[]
        for b in blocks:
            if center(b)[1]>cutoff+height(b):
                continue
            hit=re.search(r"[\u4e00-\u9fff（）()]{3,}?(?:有限公司|有限责任公司|股份有限公司|钢厂)",b["text"])
            customer_labels=[v for v in blocks if re.search(r'.?货单位|购货|供货|收货|需方|提货|订货|客户',v['text']) and same_row(v,b) and center(v)[0]<center(b)[0]]
            standalone=re.fullmatch(r'[\u4e00-\u9fff（）()]{3,}(?:有限公司|有限责任公司|钢厂)',compact(b['text']))
            heading_position=center(b)[1]<=title_cutoff+height(b)
            if hit and (standalone or heading_position) and not customer_labels and not re.search(r".?货单位|购货|供货|收货|需方|提货|订货|客户",b["text"]):
                companies.append((hit[0],b))
        names={v for v,b in companies}
        cert["manufacturer"]=next(iter(names)) if len(names)==1 else ""
        cert["manufacturer_evidence"]=[observation(cp,[b],"paddle_certificate_heading",
            polygon_page=b['polygon_page'],precision='word_block',ocr_score=b.get('score')) for v,b in companies]
        cert['manufacturer_heading']=dict(value=cert['manufacturer'],evidence=cert['manufacturer_evidence'],
            issues=[] if len(names)==1 else ['manufacturer_heading_unresolved'])
        quantities=[]
        for b in blocks:
            if re.fullmatch(r"(?:总重量|总理重|总重|合计重量|合计)(?:[T吨：:（()）.]*)",compact(b["text"])):
                if compact(b["text"])=="合计":
                    continue  # A table header can sit next to a chemical value.
                val,q=right_value(b,blocks,number)
                if val:
                    quantities.append(dict(value=q,evidence=observation(cp,[b,val],"paddle_certificate_total")))
        if not quantities and len(heats)==1:
            heatblocks=[b for b in blocks if heats[0] in compact(b["text"])]
            weightheads=[b for b in blocks if compact(b["text"]) in ("重量","重","合计：","合计:")]
            for h in weightheads:
                has_t=any(compact(u["text"]) in ("T","(T)","（T）","吨") and
                          abs(center(u)[0]-center(h)[0])<height(h)*1.5 and
                          0<center(u)[1]-center(h)[1]<4*height(h) for u in blocks)
                if not has_t:
                    continue
                for b in blocks:
                    if (number(b["text"]) and abs(center(b)[0]-center(h)[0])<height(h)*1.5
                        and any(same_row(b,hb,1.2) for hb in heatblocks)):
                        quantities.append(dict(value=number(b["text"]),
                            evidence=observation(cp,[h,b],"paddle_single_heat_weight_column")))
        # This is a document total, never a per-heat quantity on a multi-heat certificate.
        cert["total_quantity_observations"]=quantities
        qs={decimal(q["value"]) for q in quantities}
        cert["total_quantity_t"]=str(next(iter(qs))) if len(qs)==1 else ""
        spec=[]
        for h in blocks:
            if compact(h["text"]).strip(":：") not in ("规格","直径","DIMENSION"):
                continue
            val,dia=right_value(h,blocks,diameter) if ":" in h["text"] or "：" in h["text"] else (None,"")
            if val:
                spec.append((dia,val))
            else:
                for b in blocks:
                    if (diameter(b["text"]) and abs(center(b)[0]-center(h)[0])<height(h)*1.4
                        and 0<center(b)[1]-center(h)[1]<16*height(h)):
                        if any(any(heat in compact(v["text"]) for heat in heats) for v in blocks if same_row(b,v)):
                            spec.append((diameter(b["text"]),b))
        ds={d for d,b in spec}
        cert["diameter_mm"]=next(iter(ds)) if len(ds)==1 else ""
        cert["specification_observations"]=[dict(value=d,evidence=observation(cp,[b],"paddle_certificate_spec")) for d,b in spec]


def enrich_nameplates(resources):
    for p in resources["nameplates"]:
        matches=[c for c in resources["certificates"]
                 if set(p["heat_lots"]) & set(c["heat_lots"])
                 and (not c.get("diameter_mm") or c["diameter_mm"]==p["diameter_mm"])]
        for field in ("grade","manufacturer"):
            vals={g for c in matches for g in (c.get("grade_candidates",[]) if field=="grade"
                                             else [c.get("manufacturer")]) if g}
            if not p.get(field) and len(vals)==1:
                p[field]=next(iter(vals))
                p.setdefault("metadata_cross_document",{})[field]=[
                    dict(page_no=c["page_no"],candidate_id=c["candidate_id"],evidence=c["evidence"])
                    for c in matches]
                p["issues"].append(field+"_from_same_heat_certificate_requires_review")
                p["evidence_views"].extend(deepcopy(c["evidence"]) for c in matches)
