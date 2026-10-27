"""Complete a missing entry grade using already associated attachment evidence."""
from copy import deepcopy
import re
from .multiform_resources import GRADE, compact, decimal


def linked_objects(row, resources, role):
    objects=resources.get('nameplates' if role=='nameplate' else 'certificates',[])
    source=(row.get('sources') or {}).get(role) or {}
    refs=[source] if source else row.get('draft_'+role+'_candidates',[])
    found=[]
    for ref in refs:
        matches=[o for o in objects if (o.get('candidate_id')==ref['candidate_id']
                 if ref.get('candidate_id') else o.get('page_no')==ref.get('page_no'))]
        if len(matches)==1 and matches[0] not in found:found.append(matches[0])
    return found


def complete_specification_fields(ledger, resources):
    entries={e['record_id']:e for e in resources.get('records',[])}
    for row in ledger.get('records',[]):
        field=row['fields'].get('规格品种',{})
        entry=entries.get(row['record_id'],{})
        diameter=decimal(entry.get('diameter_mm'))
        if (row.get('reviewed') or field.get('manual') or field.get('conflict')
                or field.get('status') in ('manual','corrected','verified') or entry.get('grade')
                or diameter is None or row.get('cross_document_conflict')):continue
        match=re.fullmatch(r'[ΦФØ∅⌀]?(\d+(?:\.\d+)?)(?:MM)?',compact(field.get('value','')))
        if not match or decimal(match[1])!=diameter:continue
        plates=linked_objects(row,resources,'nameplate')
        certs=linked_objects(row,resources,'certificate')
        grades=set();evidence=[];conflict=False
        for obj in plates+certs:
            is_plate=obj in plates
            heading=obj.get('grade_evidence') or {}
            literals=([obj['grade']] if obj.get('grade') else []) if is_plate else list(obj.get('grade_candidates',[]))
            literals+=list(heading.get('candidates',[]) if is_plate else obj.get('grade_ocr_candidates',[]))
            literals=[v.get('value',v.get('raw_value')) if isinstance(v,dict) else v for v in literals]
            if any(not isinstance(v,str) or not re.fullmatch(GRADE,v) for v in literals):
                conflict=True;continue
            values=set(literals)
            grades.update(values)
            if decimal(obj.get('diameter_mm')) not in (None,diameter):conflict=True
            if is_plate:
                if heading.get('issues') or decimal(obj.get('diameter_mm'))!=diameter:continue
                for b in heading.get('blocks',[]):
                    if b.get('polygon') and (b.get('score') or 0)>=.8 and set(re.findall(GRADE,compact(b['text'])))==values:
                        ev=deepcopy(obj.get('evidence',{}))
                        ev.update(polygon_page=deepcopy(b['polygon']),precision='word_block',
                                  raw_text=b['text'],ocr_score=b['score'],source_role='nameplate')
                        evidence.append(ev)
            elif decimal(obj.get('diameter_mm'))==diameter:
                for obs in obj.get('grade_observations',[]):
                    ev=obs.get('evidence') or {}
                    if obs.get('value') in values and ev.get('polygon_page') and (ev.get('ocr_score') or 0)>=.8:
                        evidence.append(deepcopy(ev))
        if conflict or len(grades)!=1 or not evidence:continue
        grade=next(iter(grades))
        row.setdefault('specification_field_history',deepcopy(field))
        field.setdefault('review_notes',[]).extend(deepcopy(field.get('diagnostics',[])))
        field.update(value=f'{grade}/Φ{format(diameter.normalize(),"f")}',status='observed',
                     review_required=False,found_in_ocr=True,diagnostics=[],
                     specification_acceptance='associated_attachment_unique_grade_same_diameter')
        field['evidence']=deepcopy(field.get('evidence') or [])+evidence
        pages=sorted({v['page_no'] for v in field['evidence'] if v.get('page_no')})
        field.update(source_pages=pages,page=evidence[0].get('page_no'),polygon=evidence[0]['polygon_page'])
    return ledger
