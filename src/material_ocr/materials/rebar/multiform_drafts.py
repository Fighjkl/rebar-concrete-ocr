"""Evidence-based draft supplements around the unchanged two strict routes."""
from collections import defaultdict
from copy import deepcopy
import re
from .multiform_resources import decimal, compact


def near_text(a,b):
    """One literal edit, only for *two* independently corroborating table cells."""
    a,b=compact(a),compact(b)
    if not a or not b:
        return False
    if a==b:
        return True
    if len(a)==len(b):
        return sum(x!=y for x,y in zip(a,b))==1
    if len(a)>len(b):
        a,b=b,a
    return len(b)==len(a)+1 and any(b[:i]+b[i+1:]==a for i in range(len(b)))


def company(value):
    return re.sub(r"\s+","",value or "")


def complete_multiform_drafts(ledger, resources):
    from tools.compare_ocr_ledgers import new_field
    out=deepcopy(ledger)
    entries={e["record_id"]:e for e in resources.get("records",[])}
    certs=resources.get("certificates",[])
    plates=resources.get("nameplates",[])
    deliveries=resources.get("deliveries",[])
    # Reserve specification candidates before considering weight fallbacks.
    # Even an ambiguous specification must not be consumed by another row's
    # matching weight. Explicit certificate routes remain unchanged.
    spec_candidates={rid:[p for p in plates
        if e.get("grade") and e.get("diameter_mm")
        and p.get("grade")==e["grade"] and p.get("diameter_mm")==e["diameter_mm"]]
        for rid,e in entries.items() if not e.get("certificate_no")}
    spec_claims=defaultdict(set)
    for rid,ps in spec_candidates.items():
        for p in ps:
            for heat in p["heat_lots"]:
                spec_claims[heat].add(rid)
    strong={}
    claims=defaultdict(list)
    for rid,e in entries.items():
        if e.get("certificate_no") or not e.get("manufacturer") or decimal(e.get("weight_t")) is None:
            continue
        hits=[]
        for c in certs:
            if len(c["heat_lots"])!=1 or len(c["certificate_numbers"])!=1:
                continue
            if decimal(c.get("total_quantity_t"))!=decimal(e["weight_t"]):
                continue
            ps=[p for p in plates if c["heat_lots"][0] in p["heat_lots"] and
                company(p.get("manufacturer"))==company(e["manufacturer"])]
            if spec_candidates.get(rid):
                ps=[p for p in ps if p in spec_candidates[rid]]
            if spec_claims[c["heat_lots"][0]] - {rid}:
                continue
            if not ps:
                continue
            if c.get("manufacturer") and company(c["manufacturer"])!=company(e["manufacturer"]):
                continue
            hits.append((c,ps))
        # Duplicate copies with conflicting certificate identifiers are not unique.
        if len({(c["heat_lots"][0],c["certificate_numbers"][0]) for c,ps in hits})==1:
            strong[rid]=hits
            claims[hits[0][0]["heat_lots"][0]].append(rid)
    for row in out["records"]:
        e=entries.get(row["record_id"],{})
        if row.get("reviewed"):
            continue
        notes=row.setdefault("draft_notes",[])
        changes=list(row.get("draft_filled_fields",[]))
        selected=[];selected_certs=[]
        before=deepcopy(row["fields"])
        prior_association={k:deepcopy(row.get(k)) for k in
            ("sources","match_candidates","diagnostics","draft_nameplate_candidates","match_route","match_path")}

        def put(col,value,objects,note,replace=False):
            if row["fields"][col].get("manual") or row["fields"][col].get("status") in ("manual","corrected","verified"):
                return
            if not value or (row["fields"][col].get("value") and not replace):
                return
            if row["fields"][col].get("value")==value:
                return
            ev=[]
            for o in objects:
                ev.append(deepcopy(o.get("evidence",{})))
                ev.extend(deepcopy(o.get("evidence_views",[])))
            pages=sorted({v["page_no"] for v in ev if v.get("page_no")})
            f=new_field(value,pages[0] if len(pages)==1 else None,ev,[note],status="needs_review")
            f.update(draft_filled=True,source_pages=pages)
            row["fields"][col]=f
            changes.append(col)
            notes.append(note)

        hits=strong.get(row["record_id"],[])
        if hits and len(claims[hits[0][0]["heat_lots"][0]])==1:
            selected_certs=[c for c,ps in hits]
            selected=[p for c,ps in hits for p in ps]
            heat=selected_certs[0]["heat_lots"][0]
            old=row["fields"]["炉批号"].get("value")
            if old and old!=heat:
                row["superseded_automatic_fields"]=before
                # Dependent association fields cannot remain from the old heat.
                for col in ("炉批号","质保书编号","送货单编号","供应单位","生产单位"):
                    row["fields"][col]=new_field()
                notes.append(f"原规格路线候选 {old} 与本行重量不符；改列同生产单位、同重量且铭牌与证书炉批号一致的候选 {heat}。原自动结果已保留。")
            for p in selected:
                if (p.get("grade")!=e.get("grade") or p.get("diameter_mm")!=e.get("diameter_mm")):
                    notes.append(f"原文冲突：进场表 {e.get('grade','')} Φ{e.get('diameter_mm','')}；附件 {p.get('grade','')} Φ{p.get('diameter_mm','')}。以同厂家、同重量 {e.get('weight_t')} 吨列为草稿候选，未改进场表规格。")
                    row["cross_document_conflict"]=True
            put("炉批号",heat,selected+selected_certs,"同生产单位、同重量与铭牌—证书炉批号共同支持，暂填草稿",True)
        else:
            heat=row["fields"]["炉批号"].get("value")
            selected=[p for p in plates if heat in p["heat_lots"]] if heat else []
            selected_certs=[c for c in certs if heat in c["heat_lots"]] if heat else []
            # Preserve an already uniquely located card (e.g. adjacent to the
            # exact certificate); identical heats on another copy are not a
            # reason to broaden the delivery association again.
            located={p.get('candidate_id') for p in row.get('draft_nameplate_candidates',[])}
            source_id=(row.get('sources',{}).get('nameplate') or {}).get('candidate_id')
            if source_id:located={source_id}
            if len(located)==1:
                narrowed=[p for p in selected if p.get('candidate_id') in located]
                if narrowed:selected=narrowed
            if (not e.get("certificate_no") and not spec_candidates.get(row["record_id"])
                and heat in claims and row["record_id"] not in claims[heat]
                and decimal(e.get("weight_t")) is not None
                and any(decimal(c.get("total_quantity_t")) is not None and
                        decimal(c["total_quantity_t"])!=decimal(e["weight_t"]) for c in selected_certs)):
                row["unresolved_multiform_candidates"]=[
                    dict(heat_lots=p["heat_lots"],diameter_mm=p["diameter_mm"],page_no=p["page_no"],
                         candidate_id=p["candidate_id"]) for p in selected]
                row["superseded_automatic_fields"]=before
                for col in ("炉批号","质保书编号","送货单编号","供应单位"):
                    row["fields"][col]=new_field(issues=["与另一进场记录的附件归属冲突，候选暂不写入确定字段"],status="needs_review")
                    changes.append(col)
                notes.append("规格候选的证书重量与本行不同，且已被另一条同厂家同重量记录定位；保留候选待确认，不重复充当整批附件。")
                row["cross_document_conflict"]=True
                selected=[];selected_certs=[]
                row["sources"]={}
                row["draft_nameplate_candidates"]=[]
                row["match_candidates"]={}
        if selected:
            numbers={n for c in selected_certs for n in c["certificate_numbers"]}
            if len(numbers)==1 and not e.get("certificate_no"):
                put("质保书编号",next(iter(numbers)),selected_certs,"来自同炉批号质量证明书，暂填草稿")
            manufacturers={p.get("manufacturer") for p in selected if p.get("manufacturer")}
            if len(manufacturers)==1:
                put("生产单位",next(iter(manufacturers)),selected,"来自已关联铭牌/同炉批号证书，暂填草稿")
            row["multiform_nameplate_candidates"]=[dict(candidate_id=p["candidate_id"],page_no=p["page_no"],
                heat_lots=p["heat_lots"],grade=p.get("grade"),diameter_mm=p["diameter_mm"]) for p in selected]
            if len({p["candidate_id"] for p in selected})==1:
                row.setdefault("sources",{})["nameplate"]=dict(page_no=selected[0]["page_no"],
                    candidate_id=selected[0]["candidate_id"],association_method="multiform_evidence_draft",
                    matched_heat_lots=selected[0]["heat_lots"])
            if len({c.get("candidate_id") for c in selected_certs})==1 and selected_certs:
                row.setdefault("sources",{})["certificate"]=dict(page_no=selected_certs[0]["page_no"],
                    candidate_id=selected_certs[0].get("candidate_id"),association_method="same_heat_certificate_draft",
                    matched_heat_lots=selected_certs[0]["heat_lots"])
            if hits:
                row["draft_nameplate_candidates"]=deepcopy(row["multiform_nameplate_candidates"])
                row["match_candidates"]=dict(nameplates=deepcopy(selected),
                    certificate_pages=deepcopy(selected_certs),deliveries=[])
                row["draft_association_method"]="manufacturer_and_quantity_with_plate_certificate_heat"
            if (not e.get("certificate_no") and spec_candidates.get(row["record_id"])
                and all(p in spec_candidates[row["record_id"]] for p in selected)):
                row["draft_association_method"]="specification_before_quantity"
                mismatches=sorted({c["total_quantity_t"] for c in selected_certs
                    if decimal(c.get("total_quantity_t")) is not None
                    and decimal(e.get("weight_t")) is not None
                    and decimal(c["total_quantity_t"])!=decimal(e["weight_t"])})
                if mismatches:
                    note=(f"按牌号和直径优先关联炉批号；进场表重量 {e['weight_t']} 吨与证书重量 "
                          f"{', '.join(mismatches)} 吨不一致，保留进场表原值，待核验。")
                    notes.append(note)
                    for col in ("炉批号","质保书编号","送货单编号","验收数量"):
                        field=row["fields"][col]
                        if field.get("status") in ("manual","corrected","verified"):
                            continue
                        field.update(status="needs_review",review_required=True,found_in_ocr=False)
                        field.setdefault("diagnostics",[]).append(note)
                        changes.append(col)
            # Even when the strict route filled a different full specification,
            # keep the entry's original grade/diameter and show conflicts beside it.
            if row.get("cross_document_conflict"):
                from tools.compare_ocr_ledgers import entry_field_evidence
                row["fields"]["规格品种"]=new_field(
                    f"{e.get('grade','')}/Φ{e.get('diameter_mm','')}",e.get("page_no"),
                    entry_field_evidence(e,"specification"),["保留进场表原文；附件规格不同"],status="needs_review")
        # Match delivery *rows* across pages. One document may serve many
        # records; row evidence is kept instead of consuming the whole page.
        ds={p["diameter_mm"] for p in selected} or {e.get("diameter_mm")}
        ds.discard(None); ds.discard("")
        gs={p.get("grade") for p in selected if p.get("grade")} or ({e["grade"]} if e.get("grade") else set())
        quantities={c.get("total_quantity_t") for c in selected_certs if c.get("total_quantity_t")} or ({e["weight_t"]} if e.get("weight_t") else set())
        entry_length=decimal((e.get("fields",{}).get("specification",{}).get("parsed") or {}).get("length_mm"))
        candidates=[]
        for d in deliveries:
            for dr in d["rows"]:
                candidate_notes=[]
                delivery_length=decimal(dr.get("length_mm"))
                if entry_length and delivery_length and entry_length!=delivery_length:
                    continue
                grade_ok=not dr.get("grade") or not gs or dr["grade"] in gs
                quantity_matches=(decimal(e.get('weight_t')) is not None
                    and decimal(dr.get('quantity_t'))==decimal(e.get('weight_t')))
                grade_variant=(not grade_ok and dr.get('diameter_mm') in ds and quantity_matches
                    and any(near_text(dr.get('grade'),g) for g in gs))
                exact=dr.get("diameter_mm") in ds and (grade_ok or grade_variant)
                # A missing multiplication sign is only a draft candidate
                # when the entry explicitly supplies both dimensions.
                collapsed=(not dr.get("diameter_mm") and grade_ok and entry_length
                    and len(ds)==1 and re.sub(r'(?:HRBF|HRB|HPB|RRB|CRB)\d{3,4}[EBF]?','',compact(dr.get("specification_raw",""))) in {
                        next(iter(ds))+format((entry_length/1000).normalize(),"f"),
                        next(iter(ds))+format(entry_length.normalize(),"f")})
                # A joined diameter/standard length is review evidence only:
                # require a located card, matching grade and an independently
                # near quantity. Do not turn arbitrary long digits into sizes.
                compact_spec=re.sub(r'(?:HRBF|HRB|HPB|RRB|CRB)\d{3,4}[EBF]?','',compact(dr.get('specification_raw','')))
                collapsed_candidate=(not dr.get('diameter_mm') and not entry_length and len(ds)==1
                    and bool(selected) and dr.get('grade') in gs
                    and compact_spec in {next(iter(ds))+s for s in ('9','12','9000','12000')}
                    and any(near_text(dr.get('quantity_raw',''),format(decimal(q).normalize(),'f')) for q in quantities if decimal(q) is not None))
                # Repair neither cell. Only jointly corroborate two bad OCR
                # cells as a candidate, with a visible discrepancy note.
                near=(not dr.get("diameter_mm") and grade_ok and len(ds)==1 and
                      near_text(dr.get("specification_raw",""),next(iter(ds))) and
                      any(near_text(dr.get("quantity_raw",""),format(decimal(q).normalize(),"f")) for q in quantities))
                if not exact and not near and not collapsed and not collapsed_candidate:
                    continue
                if collapsed_candidate:
                    candidate_notes.append(f"送货行规格 {dr.get('specification_raw')} 疑似直径与长度连写，数量 {dr.get('quantity_raw')} 与进场/证书近似；结合铭牌规格仅列候选，原文和数量冲突保留待核验。")
                if grade_variant:
                    candidate_notes.append(f"送货行牌号 {dr['grade']} 与铭牌 {sorted(gs)} 差一字符；仅以同直径和进场数量一致列为候选，保留牌号冲突待核验。")
                if collapsed:
                    candidate_notes.append(f"送货行规格原文 {dr.get('specification_raw')} 疑似缺少乘号；按进场表直径及长度 {entry_length}mm 关联候选，未改原文，待核验。")
                if near:
                    candidate_notes.append(f"送货行原文规格 {dr.get('specification_raw')}、重量 {dr.get('quantity_raw')} 疑似 OCR 错字，与证书两字段各相差一字符；仅草稿关联，未改原文。")
                if dr.get("quantity_t") and quantities and all(decimal(dr["quantity_t"])!=decimal(q) for q in quantities):
                    candidate_notes.append(f"送货行重量原文 {dr['quantity_t']} 与证书 {sorted(quantities)} 不一致，送货号仅为规格候选。")
                candidates.append(dict(d, evidence=dr["evidence"],matched_delivery_row=dr,
                    association_notes=candidate_notes,
                    evidence_views=[o["evidence"] for o in d.get("delivery_number_observations",[])]))
        all_candidates=candidates
        same_card=[d for d in candidates if d['page_no'] in {p['page_no'] for p in selected}]
        if same_card:candidates=same_card
        elif selected and any(d['page_no'] in {p['page_no'] for p in selected} for d in deliveries):
            # A located card already has a delivery form; an unreadable row on
            # that form must not redirect it to another shipment of the same size.
            candidates=[]
        elif len(candidates)>1:
            quantity_hits=[d for d in candidates if decimal(e.get('weight_t')) is not None
                           and decimal(d['matched_delivery_row'].get('quantity_t'))==decimal(e.get('weight_t'))]
            if quantity_hits:candidates=quantity_hits
        for d in candidates:notes.extend(d.get('association_notes',[]))
        if len(candidates)!=len(all_candidates):
            row['other_delivery_candidates']=[dict(candidate_id=d['candidate_id'],page_no=d['page_no'],
                delivery_no=d.get('delivery_no'),row=d['matched_delivery_row']) for d in all_candidates if d not in candidates]
        row["shared_delivery_candidates"]=[dict(candidate_id=d["candidate_id"],page_no=d["page_no"],
            delivery_no=d["delivery_no"],row=d["matched_delivery_row"]) for d in candidates]
        if hits and selected:
            row["match_candidates"]["deliveries"]=deepcopy(candidates)
        current_number=row["fields"]["送货单编号"].get("value")
        prior_rows=[dr for d in deliveries if d.get("delivery_no")==current_number
                    for dr in d["rows"] if dr.get("diameter_mm") in ds]
        if (entry_length and prior_rows
                and all(decimal(dr.get("length_mm")) is not None
                        and decimal(dr["length_mm"])!=entry_length for dr in prior_rows)):
            note="原送货单关联仅直径相同，但长度与进场表不符；撤销自动关联并保留候选待核验。"
            for col in ("送货单编号","供应单位"):
                field=row["fields"][col]
                if field.get("manual") or field.get("status") in ("manual","corrected","verified"):
                    continue
                row.setdefault("superseded_delivery_fields",{})[col]=deepcopy(field)
                row["fields"][col]=new_field(issues=[note],status="needs_review")
                changes.append(col)
            row.setdefault("sources",{}).pop("delivery",None)
            notes.append(note)
        nos={d["delivery_no"] for d in candidates if d.get("delivery_no")}
        if len(nos)==1:
            put("送货单编号",next(iter(nos)),candidates,"按进场表或已关联附件规格定位跨页送货行；同一单据可供多条记录共用，需核验",True)
            if len({d["candidate_id"] for d in candidates})==1:
                row.setdefault("sources",{})["delivery"]=dict(page_no=candidates[0]["page_no"],
                    candidate_id=candidates[0]["candidate_id"],association_method="cross_page_delivery_rows_draft",
                    delivery_no=next(iter(nos)),logical_rows=[d["matched_delivery_row"].get("logical_row") for d in candidates])
        elif len(nos)>1:
            amounts=[decimal(d["matched_delivery_row"].get("quantity_t")) for d in candidates]
            # Multiple shipments are supported only by distinct numbered forms,
            # one matching row each, and an explicit entry total. Never use the
            # weight to select a different specification or discard a candidate.
            if (len(candidates)==len(nos) and amounts and all(q is not None and q>0 for q in amounts)
                    and sum(amounts)==decimal(e.get("weight_t"))):
                put("送货单编号","、".join(dict.fromkeys(d["delivery_no"] for d in candidates)),candidates,
                    "同规格多张配送单的行数量合计等于进场数量；保留各单号及原文，需核验",True)
            else:
                notes.append("同规格对应多个送货单编号，保留候选，不能按页序任选。")
        suppliers={d.get("supplier") for d in candidates if d.get("supplier")}
        if len(suppliers)==1:
            supplier_sources=[dict(d,evidence=(d.get("supplier_heading") or {}).get("evidence") or d["evidence"]) for d in candidates]
            put("供应单位",next(iter(suppliers)),supplier_sources,"来自已关联送货单明确供货单位",False)
        if changes:
            row["strict_association_before_multiform"]=prior_association
            row.setdefault("strict_match_status",row["match_status"])
            row["match_status"]="draft_filled_needs_review"
            row["draft_filled_fields"]=list(dict.fromkeys(changes))
            row["reviewed"]=False
            row["matched_heat_lots"]=[row["fields"]["炉批号"]["value"]] if row["fields"]["炉批号"].get("value") else []
            for col in set(changes):
                row["fields"][col].setdefault("diagnostics",[]).extend(n for n in notes
                    if n not in row["fields"][col]["diagnostics"])
        row["draft_notes"]=list(dict.fromkeys(notes))
    out["draft_completion_policy"]="observed_fields_multiform_spec_first_v2"
    from .company_ledger import accept_company_fields
    from .specification_ledger import complete_specification_fields
    return complete_specification_fields(accept_company_fields(out,resources),resources)
