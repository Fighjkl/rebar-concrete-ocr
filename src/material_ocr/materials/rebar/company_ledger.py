"""Judge company names using their own observed name boxes, independently of IDs."""
from copy import deepcopy
import re


def compact(value):
    return re.sub(r'\s+', '', str(value or ''))


def accept_company_fields(ledger, resources):
    from tools.compare_ocr_ledgers import new_field
    from .draft_completion import key as identifier_key,one_edit
    entries={e['record_id']:e for e in resources.get('records',[])}
    for row in ledger.get('records', []):
        if row.get('reviewed'):
            continue
        heat=row['fields'].get('炉批号',{}).get('value')
        plates=[p for p in resources.get('nameplates',[]) if heat and heat in p['heat_lots']]
        entry=entries.get(row['record_id'],{})
        number=entry.get('certificate_no') or ''
        certificates=[c for c in resources.get('certificates',[]) if number and
            any(identifier_key(number)==identifier_key(n) for n in c['certificate_numbers'])]
        if not certificates:
            near=[c for c in resources.get('certificates',[]) if number and
                  any(one_edit(number,n) for n in c['certificate_numbers'])]
            if len(near)==1:certificates=near
        if not certificates and len(row.get('draft_certificate_candidates',[]))==1:
            candidate=row['draft_certificate_candidates'][0]
            certificates=[c for c in resources.get('certificates',[]) if c['page_no']==candidate['page_no']
                          and c['certificate_numbers']==candidate['certificate_numbers']]
        if not certificates and not number:
            # Specification-route entries need not contain a certificate No.
            # Their already associated, heat-matching certificate can still
            # provide a clear issuer name independently of the card heading.
            source=(row.get('sources') or {}).get('certificate') or {}
            matched=[c for c in resources.get('certificates',[]) if heat and heat in c['heat_lots']
                     and (c.get('candidate_id')==source['candidate_id'] if source.get('candidate_id')
                          else c.get('page_no')==source.get('page_no'))]
            if len(matched)==1:certificates=matched
        cert_names={c.get('manufacturer') for c in certificates if c.get('manufacturer')}
        field=row['fields'].get('生产单位',{})
        if (len(cert_names)==1 and compact(next(iter(cert_names)))==compact(entry.get('manufacturer'))
                and not field.get('manual') and field.get('status') not in ('manual','corrected','verified')):
            value=next(iter(cert_names))
            if not field.get('value') or (value.endswith(field['value']) and len(value)-len(field['value'])==1):
                row.setdefault('company_field_history',{})['生产单位']=deepcopy(field)
                source_evidence=[ev for c in certificates for ev in c.get('manufacturer_evidence',[])]
                row['fields']['生产单位']=new_field(value,evidence=source_evidence,status='needs_review')
                plates=certificates
            elif field.get('value')==value:
                # A clear issuer heading corroborated by the entry is usable
                # even if the card's manufacturer label was not located.
                plates=certificates
        ids={d['candidate_id'] for d in row.get('shared_delivery_candidates',[])}
        source=(row.get('sources',{}).get('delivery') or {}).get('candidate_id')
        if source: ids.add(source)
        for d in resources.get('deliveries',[]):
            f=row['fields'].get('供应单位',{})
            if (d.get('delivery_no') and d['delivery_no']==row['fields'].get('送货单编号',{}).get('value')
                    and d['page_no']==f.get('page')):
                ids.add(d['candidate_id'])
        deliveries=[d for d in resources.get('deliveries',[]) if d['candidate_id'] in ids]
        for column,objects,key in [('生产单位',plates,'manufacturer'),('供应单位',deliveries,'supplier')]:
            field=row['fields'].get(column,{})
            if (not field.get('value') or field.get('manual') or field.get('conflict')
                    or field.get('status') in ('manual','corrected','verified')):
                continue
            values={compact(o.get(key)) for o in objects if o.get(key)}
            if values!={compact(field['value'])}:
                continue
            evidence=[]
            valid=bool(objects)
            for obj in objects:
                heading=obj.get(key+'_heading') or {}
                ev=heading.get('evidence') or []
                ev=ev if isinstance(ev,list) else [ev]
                located=[v for v in ev if v.get('polygon_page')
                         and compact(field['value']) in compact(v.get('raw_text'))
                         and isinstance(v.get('ocr_score'),(int,float)) and v['ocr_score']>=.8]
                if heading.get('issues') or compact(heading.get('value'))!=compact(field['value']) or not located:
                    valid=False;break
                evidence.extend(deepcopy(located))
            if not valid:
                continue
            field.setdefault('review_notes',[]).extend(deepcopy(field.get('diagnostics') or []))
            field.update(evidence=evidence,diagnostics=[],status='observed',review_required=False,
                         found_in_ocr=True,company_acceptance='unique_located_name_score_at_least_0.8')
            pages=sorted({v['page_no'] for v in evidence})
            field.update(source_pages=pages,page=pages[0] if len(pages)==1 else None)
    return ledger
