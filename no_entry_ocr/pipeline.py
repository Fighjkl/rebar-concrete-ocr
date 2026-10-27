"""Nameplate-first matching, isolated from production routes and entry logic."""
from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import csv
import html
import json
from pathlib import Path

from .extract import guard, save
from .parsers import adapt, blocks, companies, compact, ev, submission

COLUMNS=['供应商是否报审','文件编号','材料名称','规格品种','生产单位','供应单位',
         '供货日期与工程材料报审签认日期','使用部位','送货单编号','炉批号','验收数量','质保书编号']


def candidate(value, obj, evidence=None):
    return dict(value=str(value or ''), page=obj.get('page_no'), source_id=obj.get('candidate_id'),
                evidence=evidence or obj.get('evidence') or {})


def field(observations=(), reasons=(), first=False):
    obs=[deepcopy(o) for o in observations if o.get('value') not in ('',None)]
    values=sorted({str(o['value']) for o in obs})
    issues=list(reasons)
    if not values:issues.append('未识别到可用值')
    if len(values)>1:issues.append('存在多个不同候选值')
    value=values[0] if len(values)==1 else (obs[0]['value'] if obs and first else '')
    evidence=[]
    for o in obs:
        e=o.get('evidence') or []
        evidence.extend(e if isinstance(e,list) else [e])
    pages=sorted({o['page'] for o in obs if o.get('page') is not None})
    return dict(value=value,status='needs_review' if issues else 'observed',review_required=bool(issues),
        diagnostics=list(dict.fromkeys(issues)),candidates=obs,evidence=evidence,source_pages=pages,
        page=pages[0] if len(pages)==1 else None,conflict=len(values)>1)


def context(submissions,cps,manifest):
    raw={c['page_no']:c for c in cps}; observations={k:[] for k in COLUMNS}
    mapping={'document_no':'文件编号','supply_date':'供货日期与工程材料报审签认日期',
             'use_location':'使用部位','material_name':'材料名称'}
    for cp in sorted(submissions,key=lambda c:c['page_no']):
        form=cp.get('submission_form',{})
        parsed=submission(raw[cp['page_no']]) if cp['page_no'] in raw else {}
        for key,col in mapping.items():
            old=form.get('fields',{}).get(key,{})
            value=parsed.get(col) or (dict(value=old['value'],evidence=dict(page_no=cp['page_no'],
                checkpoint_reference=cp.get('checkpoint_reference'),source_role='submission',
                raw_text=old.get('raw_text'),polygon_page=old.get('polygon_page'))) if old.get('value') else None)
            if value:observations[col].append(dict(value,page=cp['page_no']))
    result={k:field(v,first=k=='文件编号') for k,v in observations.items() if v}
    result['供应商是否报审']=field([dict(value='是' if any(p['decision']=='submission' for p in manifest['pages']) else '否',
        evidence=dict(method='page_classification',source_role='classification'))])
    return result


def compatible(plate,cert):
    heats=set(plate.get('heat_lots',[])) & set(cert.get('heat_lots',[]))
    if not heats:return False
    grades=set(cert.get('grade_candidates',[]));grade=plate.get('grade')
    if grade and grades and grade not in grades:return False
    rows=[r for r in cert.get('batch_rows',[]) if r['heat_lot'] in heats]
    diameters={r['diameter_mm'] for r in rows if r.get('diameter_mm')}
    return not (plate.get('diameter_mm') and diameters and plate['diameter_mm'] not in diameters)


def equal_quantity(a,b):
    try:return a not in ('',None) and b not in ('',None) and Decimal(str(a))==Decimal(str(b))
    except InvalidOperation:return False


def company_candidates(plate,certs,by_page):
    """Located issuer names; only corroborated common suffixes remove logo text."""
    out=[]
    local=[]
    if plate.get('page_no') in by_page:
        local=companies(blocks(by_page[plate['page_no']]))
    for c in certs:
        value=c.get('manufacturer')
        if not value:continue
        matches=[]
        for name,b in local:
            suffix=''
            for a,z in zip(name[::-1],value[::-1]):
                if a!=z:break
                suffix=a+suffix
            if len(suffix)>=10 and ('钢' in suffix or '金属' in suffix):matches.append((suffix,b))
        matches.sort(key=lambda x:len(x[0]),reverse=True)
        if matches and (len(matches)==1 or len(matches[0][0])>len(matches[1][0])):
            value,b=matches[0]
            evidence=[c['evidence'],ev(by_page[plate['page_no']],[b],'nameplate')]
        else:evidence=c.get('manufacturer_evidence') or c.get('evidence')
        out.append(candidate(value,c,evidence))
    if not out and plate.get('manufacturer'):
        out=[candidate(plate['manufacturer'],plate)]
    return out


def build(resources,cps,submissions,manifest):
    guard(manifest)
    plates=deepcopy(resources.get('nameplates',[]));certificates=resources.get('certificates',[])
    for plate in plates:
        related=[c for c in certificates if compatible(plate,c)]
        for key,values in [('grade',{g for c in related for g in c.get('grade_candidates',[])}),
                           ('diameter_mm',{r['diameter_mm'] for c in related for r in c.get('batch_rows',[])
                                          if r['heat_lot'] in plate.get('heat_lots',[]) and r.get('diameter_mm')})]:
            if not plate.get(key) and len(values)==1:
                plate[key]=next(iter(values))
                plate.setdefault('completion_evidence',[]).extend(c['evidence'] for c in related)
    deliveries=resources.get('deliveries',[]);by_page={c['page_no']:c for c in cps}
    shared=context(submissions,cps,manifest)
    rows=[];traces=[];links=[]
    for i,p in enumerate(plates):
        # Page numbers identify evidence only; never used to score associations.
        certs=[c for c in certificates if compatible(p,c)]
        rejected=[c['candidate_id'] for c in certificates if set(p.get('heat_lots',[]))&set(c.get('heat_lots',[])) and c not in certs]
        grades={p.get('grade')} if p.get('grade') else {g for c in certs for g in c.get('grade_candidates',[])}
        ds={p.get('diameter_mm')} if p.get('diameter_mm') else {r['diameter_mm'] for c in certs for r in c.get('batch_rows',[]) if r['heat_lot'] in p.get('heat_lots',[]) and r.get('diameter_mm')}
        grade=next(iter(grades)) if len(grades)==1 else ''
        diameter=next(iter(ds)) if len(ds)==1 else ''
        f={k:deepcopy(shared.get(k) or field()) for k in COLUMNS}
        if not f['材料名称']['value'] and grade:f['材料名称']=field([candidate('钢筋',p)])
        f['规格品种']=field([candidate(f'{grade} Φ{diameter}',p, [p.get('evidence',{})]+p.get('completion_evidence',[]))] if grade and diameter else [],
            [] if grade and diameter else ['铭牌牌号或直径不完整，未能以炉批号唯一补全'])
        f['炉批号']=field([candidate(h,p) for h in p.get('heat_lots',[])])
        if p.get('spatial_unresolved'):
            f['炉批号']=field(f['炉批号']['candidates'],['铭牌锚点存在空间归属歧义'])
        f['生产单位']=field(company_candidates(p,certs,by_page))
        f['质保书编号']=field([candidate(n,c) for c in certs for n in c.get('certificate_numbers',[])],
                              ['同炉批号质保书存在牌号或规格冲突'] if rejected else [])
        rows.append(dict(record_id=p['candidate_id'],index_source='nameplate',nameplate_page=p['page_no'],
                         fields=f,sources=dict(nameplate=p['candidate_id'],certificates=[c['candidate_id'] for c in certs]),
                         index_key=dict(heat_lots=p.get('heat_lots',[]),grade=grade,diameter_mm=diameter),
                         extraction_notes=p.get('issues',[])))
        traces.append(dict(record_id=p['candidate_id'],certificates=[c['candidate_id'] for c in certs],
                           rejected_certificate_conflicts=rejected,delivery_candidates=[]))
        for d in deliveries:
            for j,r in enumerate(d.get('rows',[])):
                rd=compact(r.get('diameter_mm'));rg=compact(r.get('grade'))
                rh=set(r.get('heat_lots',[]))
                if r.get('heat_lot'):rh.add(r['heat_lot'])
                reason=None
                if rh:
                    if rh & set(p.get('heat_lots',[])) and (not rd or rd==diameter) and (not rg or rg==grade):reason='explicit_heat'
                elif diameter and rd==diameter and (not rg or rg==grade):
                    # An ungraded delivery row requires one grade for that diameter
                    # across the entire nameplate index, never the closest page.
                    possible={x.get('grade') for x in plates if x.get('diameter_mm')==diameter}
                    if rg or possible=={grade}:reason='grade_diameter' if rg else 'diameter_unique_grade'
                if reason:links.append(dict(record=i,delivery=d,row=r,row_index=j,rule=reason))
    # Secondary candidates only: a receipt with already matched specification
    # rows can corroborate an unreadable row using its linked certificate batch.
    # A readable conflicting specification is NEVER overridden by weight.
    direct_links=list(links)
    for d in deliveries:
        established={c for l in direct_links if l['delivery']['candidate_id']==d['candidate_id']
                     for c in rows[l['record']]['sources']['certificates']}
        for j,r in enumerate(d.get('rows',[])):
            if r.get('diameter_mm') or r.get('heat_lots') or r.get('heat_lot'):continue
            candidates=[]
            for i,p in enumerate(plates):
                cs=[c for c in certificates if c['candidate_id'] in established and compatible(p,c)]
                if any(br['heat_lot'] in p.get('heat_lots',[]) and
                       (not br.get('diameter_mm') or br['diameter_mm']==p.get('diameter_mm')) and
                       equal_quantity(br.get('quantity_t'),r.get('quantity_t'))
                       for c in cs for br in c.get('batch_rows',[])):
                    candidates.append(i)
            if len(candidates)==1:
                links.append(dict(record=candidates[0],delivery=d,row=r,row_index=j,
                                  rule='corroborated_batch_weight_candidate'))
    assignments=Counter((l['delivery']['candidate_id'],l['row_index']) for l in links)
    for i,row in enumerate(rows):
        chosen=[l for l in links if l['record']==i];f=row['fields']; p=plates[i]
        for col,key in [('送货单编号','delivery_no'),('供应单位','supplier')]:
            obs=[candidate(l['delivery'].get(key),l['delivery'],
                 (l['delivery'].get('supplier_heading') or {}).get('evidence') if key=='supplier' else None) for l in chosen]
            uncertain=any(l['rule']=='corroborated_batch_weight_candidate' for l in chosen)
            reasons=['送货行规格不清，仅按已关联质保书的批次重量生成候选'] if uncertain else []
            if key=='supplier' and not any(o['value'] for o in obs) and chosen:
                reasons.append('送货附件未识别到供应单位全称，不能用生产单位代填')
            f[col]=field(obs,reasons)
        quantities=[];notes=[]
        if len(p.get('heat_lots',[]))!=1 or p.get('spatial_unresolved'):
            notes.append('铭牌炉批号或空间归属不唯一，数量关联仍需核验')
        for l in chosen:
            d=l['delivery'];r=l['row'];identity=(d['candidate_id'],l['row_index'])
            traces[i]['delivery_candidates'].append(dict(candidate_id=d['candidate_id'],row_index=l['row_index'],
                match_rule=l['rule'],index_claimants=assignments[identity]))
            if assignments[identity]>1:notes.append('同一送货行对应多张铭牌，数量不能重复分配')
            if d.get('quantity_basis'):notes.extend(d.get('issues',[]))
            if r.get('issues'):notes.extend(str(x) for x in r['issues'])
            if l['rule']=='corroborated_batch_weight_candidate':notes.append('重量辅助匹配仅为候选，须核验规格')
            if r.get('quantity_t'):quantities.append(candidate(r['quantity_t'],d,r.get('evidence')))
        if len(chosen)>1:notes.append('存在多条送货记录，未自动求和或择一')
        f['验收数量']=field(quantities,notes)
        if any(assignments[(l['delivery']['candidate_id'],l['row_index'])]>1 for l in chosen):f['验收数量']['value']=''
        # Certificate quantities remain evidence candidates, not claimed receipt totals.
        if not quantities:
            cert_ids=set(row['sources']['certificates'])
            qs=[candidate(r['quantity_t'],c,r['evidence']) for c in certificates if c['candidate_id'] in cert_ids
                for r in c.get('batch_rows',[]) if r['heat_lot'] in p.get('heat_lots',[]) and r.get('quantity_t')
                and (not r.get('diameter_mm') or r['diameter_mm']==p.get('diameter_mm'))]
            f['验收数量']=field(qs,['仅有质保书批次重量，尚未确认实际验收数量'])
            if sum(x['index_key']==row['index_key'] for x in rows)>1:
                f['验收数量']['value']=''
                f['验收数量']['diagnostics'].append('同炉批号同规格有多张铭牌，质保书重量未分配')
        row['sources']['deliveries']=sorted({l['delivery']['candidate_id'] for l in chosen})
        row['review_required']=any(v['review_required'] for v in f.values())
    matched_cert={c for r in rows for c in r['sources']['certificates']}
    matched_delivery={(l['delivery']['candidate_id'],l['row_index']) for l in links}
    unmatched=[dict(kind='certificate',candidate_id=c['candidate_id'],page_no=c['page_no'],reason='无唯一兼容铭牌炉批号')
               for c in certificates if c['candidate_id'] not in matched_cert]
    for c in certificates:
        if c['candidate_id'] not in matched_cert:continue
        linked_heats={h for row in rows if c['candidate_id'] in row['sources']['certificates']
                      for h in row['index_key']['heat_lots']}
        for heat in sorted(set(c.get('heat_lots',[]))-linked_heats):
            unmatched.append(dict(kind='certificate_batch',candidate_id=c['candidate_id'],
                page_no=c['page_no'],heat_lot=heat,reason='此质保书批次没有对应铭牌，未新建记录'))
    for d in deliveries:
        for j,r in enumerate(d.get('rows',[]) or [{}]):
            if (d['candidate_id'],j) not in matched_delivery:
                unmatched.append(dict(kind='delivery_row',candidate_id=d['candidate_id'],page_no=d['page_no'],
                    row_index=j,row=r,reason='没有匹配铭牌；未据此新建台账记录'))
    covered={p['page_no'] for p in plates+certificates+deliveries+submissions}
    excluded={p['page_no'] for p in resources.get('pages',[]) if p.get('document_type')=='excluded'}
    excluded.update(resources.get('excluded_experiment_pages',[]))
    for cp in cps:
        if cp['page_no'] not in covered|excluded:
            unmatched.append(dict(kind='unparsed_page',page_no=cp['page_no'],
                                  reason='附件尚未解析为可匹配记录，需查看原图'))
    return dict(schema_version='no-entry-nameplate-v1',columns=COLUMNS,records=rows,match_trace=traces,
        unmatched_attachments=unmatched,classification_review_pages=manifest.get('review_pages',[]),
        excluded_report_pages=sorted(set(resources.get('excluded_experiment_pages',[]))),
        summary=dict(nameplates=len(plates),records=len(rows),unmatched_attachments=len(unmatched),
            pending_fields=sum(f['review_required'] for r in rows for f in r['fields'].values()),
            page_order_used_for_matching=False,glm_calls=0))


def report(ledger,out,cps):
    out=Path(out)
    with (out/'ledger.csv').open('w',encoding='utf-8-sig',newline='') as fp:
        writer=csv.writer(fp);writer.writerow(['铭牌页']+COLUMNS)
        for row in ledger['records']:writer.writerow([row['nameplate_page']]+[row['fields'][c]['value'] for c in COLUMNS])
    e=lambda x:html.escape(str(x))
    body=['<!doctype html><meta charset="utf-8"><title>无进场表·铭牌索引测试</title>',
        '<style>body{font:14px sans-serif;margin:24px}table{border-collapse:collapse}td,th{border:1px solid #bbb;padding:8px;min-width:90px;vertical-align:top}.pending{background:#fff1d6}small{display:block;color:#555}summary{cursor:pointer}pre{white-space:pre-wrap;max-width:500px}img{max-width:90vw}</style>',
        '<h1>无进场表：铭牌索引实验</h1><p>独立测试结果，未接入原前端。附件按炉批号、牌号和规格匹配，页序不参与匹配。</p>',
        '<p>数量单位：吨。橙色字段需要核验，展开可查看原因和原始证据。CSV 不含核验标记，请以本页及 JSON 为准。</p>',
        '<pre>'+e(json.dumps(ledger['summary'],ensure_ascii=False,indent=2))+'</pre><table><tr><th>铭牌页</th>']
    body.extend('<th>'+e(c)+'</th>' for c in COLUMNS);body.append('</tr>')
    for row in ledger['records']:
        body.append('<tr><td><a href="#p'+str(row['nameplate_page'])+'">p'+str(row['nameplate_page'])+'</a></td>')
        for c in COLUMNS:
            f=row['fields'][c];body.append('<td class="'+('pending' if f['review_required'] else '')+'">'+e(f['value'] or '—'))
            body.append('<small>'+','.join('p'+str(p) for p in f['source_pages'])+'</small><details><summary>'+('待核验' if f['review_required'] else '证据')+'</summary>')
            body.append('<pre>'+e(json.dumps(f,ensure_ascii=False,indent=2))+'</pre></details></td>')
        body.append('</tr>')
    body.append('</table><h2>未匹配附件</h2><pre>'+e(json.dumps(ledger['unmatched_attachments'],ensure_ascii=False,indent=2))+'</pre>')
    # Symlinks reference only the existing immutable OCR images; no production files change.
    image_dir=out/'images';image_dir.mkdir(exist_ok=True)
    for cp in sorted(cps,key=lambda c:c['page_no']):
        src=cp.get('source_image')
        if not src or not Path(src).is_file():continue
        dest=image_dir/f'p{cp["page_no"]:03d}.png'
        if not dest.exists():dest.symlink_to(Path(src).resolve())
        body.append(f'<details id="p{cp["page_no"]}"><summary>原图 p{cp["page_no"]}</summary><img loading="lazy" src="images/{dest.name}"></details>')
    (out/'index.html').write_text('\n'.join(body),encoding='utf8')


def process(resources,cps,submissions,manifest,out):
    enriched=adapt(resources,cps)
    save(Path(out)/'adapted_resources.json',enriched)
    ledger=build(enriched,cps,submissions,manifest)
    save(Path(out)/'ledger.json',ledger);save(Path(out)/'match_trace.json',ledger['match_trace'])
    report(ledger,out,cps)
    return ledger
