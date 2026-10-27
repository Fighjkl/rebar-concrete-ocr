"""General OCR-box repairs around the unchanged v3 anchor matcher.

No page numbers, company names or identifier values are encoded here.
Primary-view geometry takes precedence over rotated duplicate observations.
"""
from copy import deepcopy
import re
from material_ocr.anchor_v3 import matcher as m


def overlap(a,b):
    def bounds(p):
        return min(x for x,y in p),min(y for x,y in p),max(x for x,y in p),max(y for x,y in p)
    a,b=bounds(a),bounds(b)
    area=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    return area/max(1,min((a[2]-a[0])*(a[3]-a[1]),(b[2]-b[0])*(b[3]-b[1])))


def partial_size(raw):
    text=m.clean(raw)
    hit=re.fullmatch(r"[ΦФØ∅⌀]?(\d{1,2}(?:\.\d+)?)(?:MM|毫米)?[X×*]",text)
    if hit and 4<=float(hit[1])<=80:
        return dict(diameter_mm=float(hit[1]),length_mm=None)
    hit=re.fullmatch(r"[ΦФØ∅⌀]?(\d{1,2}(?:\.\d+)?)/(\d{1,5}(?:\.\d+)?)(MM|M)?",text)
    if hit and 4<=float(hit[1])<=80:
        return dict(diameter_mm=float(hit[1]),length_mm=float(hit[2])*(1000 if hit[3]=="M" else 1) if hit[3] else None,
                    length_raw=hit[2]+(hit[3] or ""))
    return None


def extract(blocks, known_heats=()):
    # Extremely tall multi-character boxes are not upright text lines; in
    # v3 their height could expand a neighbour search across the whole page.
    usable=[]
    for b in blocks:
        # Punctuation after an explicitly labelled heat is a delimiter, not
        # part of the identifier. Retain the original block for provenance.
        if re.search(r'(?:轧制批号|炉批号|炉号|批号)[:：]?[A-Z0-9/-]{5,}[.。]$',m.clean(b['text'])):
            b=dict(b,text=b['text'].rstrip('.。'))
        g=m.geometry(b["polygon"])
        if len(m.clean(b["text"]))>1 and g["height"]>g["width"]*2:
            continue
        usable.append(b)
    # Reconstruct one additional split label without changing the vendored v3.
    prepared=m.prepare_blocks(usable)
    for a in prepared:
        if m.clean(a["text"]) != "炉批":
            continue
        for b in prepared:
            if m.clean(b["text"]).strip(":：") != "号":
                continue
            h=max(a["g"]["height"],b["g"]["height"])
            x,y=m.local(b["g"]["center"],a["g"])
            gap=x-(a["g"]["width"]+b["g"]["width"])/2
            if x>0 and -.2*h<=gap<=2*h and abs(y)<.65*h and m.angle_gap(a["g"],b["g"])<18:
                usable.append(dict(id="joined_"+a["id"]+"_"+b["id"],text="炉批号",
                    polygon=m.union_polygon([a,b],a["g"]),score=m.minimum_score([a,b]),
                    source_block_ids=[a["id"],b["id"]],rotation=a.get("rotation",0)))
                break
    existing_spec_sources={sid for f in m.extract_fields(usable) if f["kind"]=="spec" and f.get("value")
                           for sid in f["anchor_source_block_ids"]}
    for a in prepared:
        text=m.clean(a["text"])
        alias=None
        if a["id"] not in existing_spec_sources and re.fullmatch(r"格[:：]?[ΦФØ∅⌀].*",text):
            alias="规"+a["text"]
        elif text in ("号:", "号："):
            # A truncated generic "号" is NOT a heat label unless its observed
            # right-hand value is independently present in a certificate.
            for b in prepared:
                if m.clean(b["text"]) not in known_heats:
                    continue
                x,y=m.local(b["g"]["center"],a["g"])
                h=max(a["g"]["height"],b["g"]["height"])
                if 0<x<12*h and abs(y)<.7*h and m.angle_gap(a["g"],b["g"])<18:
                    alias="炉批"+a["text"]
                    break
        if alias:
            usable.append(dict(a,id="context_"+a["id"],text=alias,
                source_block_ids=[a["id"]],contextual_label=True))
    fields=m.extract_fields(usable)
    for f in fields:
        if "context_" in f["id"]:
            f["issues"].append("partial_label_recovered_requires_review")
            f["issues"].append("anchor_ocr_variant")
            f["contextual_label"]=f["label"]
            original=[b["text"] for b in blocks if b.get("id") in f["anchor_source_block_ids"]]
            f["anchor_text"]=" ".join(original)
            f["label"]=f["anchor_text"]
    prepared=m.reconstruct_labels(m.prepare_blocks(usable))
    for f in fields:
        if f["kind"]!="spec" or f["value"] is not None:
            continue
        candidates=[]
        for b in prepared:
            value=partial_size(b["text"])
            if not value or m.angle_gap(f["g"],b["g"])>18:
                continue
            h=max(f["g"]["height"],b["g"]["height"])
            x,y=m.local(b["g"]["center"],f["g"])
            gap=x-(f["g"]["width"]+b["g"]["width"])/2
            if x<=0 or not -.6*h<=gap<=10*h or abs(y)>.9*h:
                continue
            candidates.append(dict(value=value,raw=b["text"],cost=max(0,gap)/h*.18+abs(y)/h,
                block_ids=f["anchor_source_block_ids"]+[b["id"]],value_polygon=b["polygon"],
                value_source_block_ids=[b["id"]],value_ocr_score=b.get("score"),
                ocr_min_score=min(f.get("anchor_score") or 0,b.get("score") or 0),
                value_score_scope="ocr_blocks_not_individual_characters"))
        if candidates:
            candidates.sort(key=lambda c:c["cost"])
            f.update(candidates[0])
            f["candidates"]=candidates
            f["issues"]=[i for i in f["issues"] if i!="anchor_value_missing"]+["length_not_extracted"]
            if len({str(c["value"]) for c in candidates if c["cost"]<candidates[0]["cost"]+.8})>1:
                f["issues"].append("anchor_value_ambiguous")
    # A delivery table's column heading is not a nameplate's specification label.
    result=[]
    for f in fields:
        peers=[]
        for b in prepared:
            x,y=m.local(b["g"]["center"],f["g"]);h=max(1,f["g"]["height"])
            if 0<x<20*h and abs(y)<1.2*h and m.angle_gap(f["g"],b["g"])<18 and re.search(r"签收|件数|产地|件重|重量",b["text"]):
                peers.append(b)
        categories={re.search(r"签收|件数|产地|件重|重量",b["text"])[0] for b in peers}
        if f["kind"]=="spec" and len(categories)>=2:
            continue
        result.append(f)
    return result


def merge_views(fields):
    """Input is ordered primary view first; never replace its geometry by score."""
    def bilingual(a,b):
        if bool(re.search(r"[\u4e00-\u9fff]",a["label"]))==bool(re.search(r"[\u4e00-\u9fff]",b["label"])):
            return False
        x,y=m.local(b["g"]["center"],a["g"]);h=max(a["g"]["height"],b["g"]["height"])
        left=x+(a["g"]["width"]-b["g"]["width"])/2
        return m.angle_gap(a["g"],b["g"])<18 and abs(left)<1.2*h and abs(y)<1.4*h
    merged=[]
    for f in fields:
        existing=next((d for d in merged if d["kind"]==f["kind"] and (
            overlap(d["anchor_polygon"],f["anchor_polygon"])>.55 or bilingual(d,f) or
            (d.get("value") and f.get("value") and overlap(d.get("value_polygon",d["anchor_polygon"]),
                                                        f.get("value_polygon",f["anchor_polygon"]))>.6))),None)
        if existing is None:
            merged.append(f)
            continue
        existing.setdefault("other_views",[]).append(dict(rotation=f["rotation"],value=deepcopy(f["value"]),issues=f["issues"]))
        if existing["value"] is None and f["value"] is not None:
            for k in ("value","raw","block_ids","value_polygon","value_source_block_ids","value_ocr_score","ocr_min_score","candidates"):
                if k in f:existing[k]=deepcopy(f[k])
            existing["issues"]=[i for i in f["issues"] if i!="anchor_value_missing"]
        elif f["value"] is not None and existing["value"]!=f["value"]:
            existing.setdefault("alternate_view_conflicts",[]).append(deepcopy(f))
    return merged
