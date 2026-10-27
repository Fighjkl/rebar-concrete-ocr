"""Fill supported draft fields without requiring the entire chain to validate.

Never approves a record or edits OCR text. Near identifiers require a single
candidate, preserve both readings, and are explicitly marked for review.
"""
from copy import deepcopy
import re


def key(value):
    return re.sub(r"[^A-Z0-9]","",str(value or "").upper())


def one_edit(a,b):
    a,b=key(a),key(b)
    if min(len(a),len(b))<8 or abs(len(a)-len(b))>1 or a==b:
        return False
    if len(a)==len(b):
        return sum(x!=y for x,y in zip(a,b))==1
    if len(a)>len(b):a,b=b,a
    return any(b[:i]+b[i+1:]==a for i in range(len(b)))


def moved_digit(a,b):
    """A long identifier with exactly one displaced digit, never substitutions."""
    a,b=key(a),key(b)
    return (len(a)==len(b) and len(a)>=12 and a!=b and
            any(a[i].isdigit() and a[i]==b[j] and a[:i]+a[i+1:]==b[:j]+b[j+1:]
                for i in range(len(a)) for j in range(len(b)) if i!=j))


def corroborated_preceding_plate(entry,cert,plates,deliveries):
    """Page proximity is usable only with independent company/spec/quantity."""
    from .multiform_resources import decimal,compact
    if not entry.get('manufacturer') or not entry.get('diameter_mm') or decimal(entry.get('weight_t')) is None:
        return []
    if compact(cert.get('manufacturer'))!=compact(entry['manufacturer']):return []
    hits=[]
    for p in plates:
        if (p['page_no']!=cert['page_no']-1 or p.get('diameter_mm')!=entry['diameter_mm']
                or compact(p.get('manufacturer'))!=compact(entry['manufacturer']) or len(p['heat_lots'])!=1):continue
        if entry.get('grade') and p.get('grade')!=entry['grade']:continue
        ds=[d for d in deliveries if d['page_no']==p['page_no'] and d.get('delivery_no')]
        if len(ds)!=1:continue
        d=ds[0]
        row_hits=[r for r in d['rows'] if r.get('diameter_mm')==entry['diameter_mm']
                  and decimal(r.get('quantity_t'))==decimal(entry['weight_t'])]
        totals={decimal(o['value']) for o in d.get('repeated_quantity_observations',[])}
        if row_hits or (not d['rows'] and totals=={decimal(entry['weight_t'])}):hits.append(p)
    return hits if len(hits)==1 else []


def complete_drafts(ledger,resources):
    from tools.compare_ocr_ledgers import new_field
    output=deepcopy(ledger)
    entries={r["record_id"]:r for r in resources.get("records", [])}
    certificates=resources.get("certificates", [])
    plates=resources.get("nameplates", [])
    deliveries=resources.get("deliveries", [])
    for row in output["records"]:
        if row.get('reviewed'):continue
        entry=entries.get(row["record_id"], {})
        notes=[];changes=[];certs=[];chosen=[]
        original=entry.get("certificate_no") or ""
        def fill(column,value,objects,note):
            current=row["fields"][column]
            if not value or current.get("value") or current.get('manual') or current.get('status') in ('manual','corrected','verified'):
                return
            ev=[deepcopy(o.get("evidence",{})) for o in objects]
            pages=sorted({o["page_no"] for o in objects})
            row["fields"][column]=new_field(value,pages[0] if len(pages)==1 else None,ev,
                [note],status="needs_review")
            row["fields"][column].update(draft_filled=True,source_pages=pages)
            changes.append(column)
            notes.append(note)
        if key(original):
            certs=[c for c in certificates if key(original) in {key(n) for n in c["certificate_numbers"]}]
            if not certs:
                near=[c for c in certificates if any(one_edit(original,n) for n in c["certificate_numbers"])]
                if len(near)==1:
                    certs=near
                    notes.append(f"证书编号存在一处字符差异：清单 {original}，证书 OCR {near[0]['certificate_numbers']}；唯一近似候选用于草稿关联，未更改原文")
                elif near:
                    row["draft_unresolved_certificate_candidates"]=[
                        dict(page_no=c["page_no"], certificate_numbers=c["certificate_numbers"],
                             heat_lots=c["heat_lots"]) for c in near]
                    notes.append("发现多个近似证书编号候选，无法唯一归属，保留候选待核验")
                else:
                    moved=[c for c in certificates if any(moved_digit(original,n) for n in c['certificate_numbers'])]
                    if len(moved)==1 and corroborated_preceding_plate(entry,moved[0],plates,deliveries):
                        certs=moved
                        notes.append(f"证书编号疑似一位数字错位：进场表 {original}，证书 {moved[0]['certificate_numbers']}；同厂家、规格、送货数量及相邻铭牌共同支持，仅草稿关联，保留原文待核验")
            heats={h for c in certs for h in c["heat_lots"]}
            chosen=[p for p in plates if {key(h) for h in heats}.intersection(key(h) for h in p["heat_lots"])]
            if not chosen and len(heats)==1:
                near=[p for p in plates if any(one_edit(next(iter(heats)),h) for h in p["heat_lots"])
                      and (not entry.get("diameter_mm") or p.get("diameter_mm")==entry["diameter_mm"])]
                if len(near)>1 and len(certs)==1:
                    adjacent=corroborated_preceding_plate(entry,certs[0],near,deliveries)
                    if adjacent:near=adjacent
                if len(near)==1:
                    chosen=near
                    notes.append(f"证书炉批号 {sorted(heats)} 与铭牌 {near[0]['heat_lots']} 存在字符差异；暂采用铭牌原文，保留两种读数待核验")
            if not chosen and not heats and len(certs)==1:
                chosen=corroborated_preceding_plate(entry,certs[0],plates,deliveries)
                if chosen:
                    notes.append('证书炉批号未读出；同厂家、同规格、同送货数量及紧邻证书的唯一铭牌共同支持，炉批号仅填铭牌原文，待核验证书对应关系')
            if len(chosen)>1 and len(certs)==1:
                adjacent=[p for p in chosen if p["page_no"]==certs[0]["page_no"]-1]
                if len(adjacent)==1:
                    chosen=adjacent
                    notes.append("同炉批号出现于多个铭牌页，草稿按紧邻证书前一页的唯一铭牌归组，需核验附件归属")
            plate_heats={h for p in chosen for h in p["heat_lots"]}
            if len(plate_heats)==1:
                fill("炉批号",next(iter(plate_heats)),chosen,"炉批号有铭牌原文，先填草稿；其他校验不阻止已有字段显示")
            elif len(heats)==1:
                fill("炉批号",next(iter(heats)),certs,"炉批号来自已定位质量证明书，铭牌关联尚未唯一确认，先填草稿")
            elif len(heats)>1:
                notes.append(f"已定位证书包含多个炉批号 {sorted(heats)}，铭牌未唯一定位，保留候选待核验")
            row["draft_certificate_candidates"]=[dict(page_no=c["page_no"],certificate_numbers=c["certificate_numbers"],heat_lots=c["heat_lots"]) for c in certs]
        else:
            # Still require both entry keywords when there is no certificate.
            chosen=[p for p in plates if entry.get("grade") and entry.get("diameter_mm") and
                    p.get("grade")==entry["grade"] and p.get("diameter_mm")==entry["diameter_mm"]]
            hs={h for p in chosen for h in p["heat_lots"]}
            if len(hs)==1:
                fill("炉批号",next(iter(hs)),chosen,"规格与牌号定位的铭牌炉批号一致，先填草稿")
        if chosen:
            if entry.get("diameter_mm") and any(p.get("diameter_mm") and
                    p["diameter_mm"] != entry["diameter_mm"] for p in chosen):
                notes.append("候选铭牌规格与进场表不一致；已有字段仅作为草稿，需核验归属")
            companies={p.get("manufacturer") for p in chosen if p.get("manufacturer")}
            if len(companies)==1:
                fill("生产单位",next(iter(companies)),chosen,"相关铭牌中的生产单位读数一致，先填草稿")
            samepage=[d for d in deliveries if d["page_no"] in {p["page_no"] for p in chosen}]
            for col,field in (("供应单位","supplier"),("送货单编号","delivery_no")):
                vals={d.get(field) for d in samepage if d.get(field)}
                if len(vals)==1:
                    fill(col,next(iter(vals)),samepage,"采用相关铭牌同页送货单的唯一字段值；货物行校验问题另列")
            row["draft_nameplate_candidates"]=[dict(page_no=p["page_no"],heat_lots=p["heat_lots"],
                diameter_mm=p.get("diameter_mm"),candidate_id=p["candidate_id"]) for p in chosen]
        if changes:
            # Preserve the strict result alongside the explicitly requested draft.
            row["strict_match_status"]=row["match_status"]
            row["match_status"]="draft_filled_needs_review"
            row["draft_filled_fields"]=changes
            row["matched_heat_lots"]=[row["fields"]["炉批号"]["value"]] if row["fields"]["炉批号"]["value"] else []
            row["draft_notes"]=list(dict.fromkeys(notes))
            for col in changes:
                row["fields"][col]["diagnostics"]+=list(dict.fromkeys(notes))
            row["reviewed"]=False
        elif notes:
            row["draft_notes"]=list(dict.fromkeys(notes))
    output["draft_completion_policy"]="observed_fields_first_v1"
    from .multiform_drafts import complete_multiform_drafts
    return complete_multiform_drafts(output, resources)
