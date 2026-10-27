"""Resolve rebar ledger sources without guessing OCR or adding duplicate quantities."""
from copy import deepcopy
from decimal import Decimal
import re

from material_ocr.materials.rebar.parsing import parse_weight


def _structured_quantity_unit(record, raw):
    """Use a mass unit only from this quantity cell's explicit saved header."""
    quantity = record.get('fields', {}).get('quantity') or {}
    parsed = quantity.get('value')
    if (not isinstance(parsed, dict) or quantity.get('issues') or
            quantity.get('unit_source') != 'column_header' or
            re.sub(r'\s+','',quantity.get('raw_text','')) != re.sub(r'\s+','',raw)):
        return None, None
    header = quantity.get('unit_header_evidence') or quantity.get('header_evidence') or {}
    texts = [str(token.get('text','')) for token in header.get('ocr_tokens', [])]
    units = {'t':'t','吨':'t','kg':'kg','公斤':'kg','千克':'kg'}
    declared = units.get(str(parsed.get('unit','')).lower())
    if not declared:
        return None, None
    for text in texts:
        match = re.fullmatch(r'(?:进场数量|验收数量|到货数量|数量|重量)[（(](t|吨|kg|公斤|千克)[）)]',
                             re.sub(r'\s+','',text),re.I)
        if match and units[match.group(1).lower()] == declared:
            return declared, deepcopy(header)
    return None, None


def inspection_fields(record, record_id, evidence=None):
    from material_ocr.materials.rebar.filling import decimal_text, field
    effective = record.get('effective_cells', record.get('raw_cells', {}))
    raw = effective.get('weight', '')
    ev = deepcopy(evidence or record.get('field_evidence', {}).get('weight') or {})
    ev.update(source_role='inspection', record_id=record_id, page_no=record.get('page_no'),
              row_no=record.get('row_no'), cell_id=record.get('source_cells', {}).get('weight'),
              source_result=record.get('source_result'), raw_text=raw or record.get('weight_t'),
              method='inspection_entry_quantity', label='进场数量', normalized_unit='t')
    unit = re.search(r'(kg|千克|t|吨)\s*$', raw, re.I)
    ev['raw_unit'] = unit.group(1).lower() if unit else None
    candidates, warnings = [], []
    if raw:
        parse_raw = raw
        if not unit:
            header_unit, header_evidence = _structured_quantity_unit(record,raw)
            if header_unit:
                parse_raw = raw+header_unit
                ev.update(raw_unit=header_unit,unit_source='column_header',
                          unit_header_evidence=header_evidence)
        value, error = parse_weight(parse_raw)
        if error:
            warnings.append('检验记录进场数量无法解析或缺少单位，请核对原图。')
        else:
            candidates.append(dict(value=decimal_text(value), evidence=[ev]))
            normalized = decimal_text(record.get('weight_t'))
            if normalized and Decimal(normalized) > 0 and Decimal(normalized) != Decimal(value):
                other = dict(ev, method='inspection_normalized_entry_quantity', raw_text=record['weight_t'])
                candidates.append(dict(value=normalized, evidence=[other]))
                warnings.append('检验数量原文与保存的标准化值不一致，需人工核对。')
    else:
        value = decimal_text(record.get('weight_t'))
        if value and Decimal(value) > 0:
            ev['method'] = 'inspection_normalized_entry_quantity'
            candidates.append(dict(value=value, evidence=[ev]))
    result = field(candidates, warnings)
    result['source_role'] = 'inspection'
    return dict(quantity_t=result)


def sourced(info, role, source=None):
    """Attach the actual source to every observation, including legacy profiles."""
    from material_ocr.materials.rebar.filling import field
    info = deepcopy(info or field())
    info['source_role'] = role
    for candidate in info['candidates']:
        if not candidate.get('evidence'):
            candidate['evidence'] = [dict(source or {}, source_role=role)]
        for ev in candidate['evidence']:
            ev.setdefault('source_role', role)
            for key, value in (source or {}).items():
                ev.setdefault(key, value)
    return info


def resolve_quantity(inspections, delivery):
    from material_ocr.materials.rebar.filling import field
    primary = field([c for item in inspections for c in item['candidates']],
                    list(dict.fromkeys(w for item in inspections for w in item.get('warnings', []))))
    primary['source_role'] = 'inspection'
    if primary['candidates']:
        if primary['status'] == 'ambiguous':
            primary['warnings'].append('同一捆绑的检验记录进场数量不一致，请人工复核；重复记录不累加。')
        elif delivery.get('candidates') and {c['value'] for c in delivery['candidates']} != {primary['value']}:
            primary['warnings'].append('检验进场数量与送货行重量不一致；验收数量优先采用检验记录，请核对差异。')
            primary['alternatives'] = deepcopy(delivery['candidates'])
        return primary
    fallback = deepcopy(delivery)
    fallback['warnings'] = primary['warnings'] + fallback.get('warnings', []) + [
        '检验进场数量未完整提取，暂以关联送货行重量（吨）补充，需人工核对。']
    return fallback


def resolve_specification(plate, delivery):
    from material_ocr.materials.rebar.filling import field
    primary = deepcopy(plate.get('specification', field()))
    alternate = deepcopy(delivery.get('specification', field()))
    if primary['candidates']:
        if alternate['candidates'] and {c['value'] for c in alternate['candidates']} != {c['value'] for c in primary['candidates']}:
            primary['warnings'].append('铭牌规格与关联送货行规格不一致，优先保留铭牌值，请核对差异。')
            primary['alternatives'] = alternate['candidates']
        return primary
    chosen, roles = {}, set()
    for key in ('grade', 'diameter_mm'):
        p, d = plate.get(key, field()), delivery.get(key, field())
        if p['candidates'] and d['candidates'] and {c['value'] for c in p['candidates']} != {c['value'] for c in d['candidates']}:
            result = field(alternate['candidates'], ['铭牌已识别的牌号或直径与送货行不一致，不能自动补规格，请核对原图。'])
            result.update(value='', status='ambiguous', source_role='delivery')
            return result
        chosen[key] = p if p['candidates'] else d
        roles.add(chosen[key].get('source_role', 'nameplate' if p['candidates'] else 'delivery'))
    candidates = [dict(value=f"{g['value']}/Ф{d['value']}", evidence=g['evidence']+d['evidence'])
                  for g in chosen['grade']['candidates'] for d in chosen['diameter_mm']['candidates']]
    # Legacy resources can contain a complete delivery specification alone.
    result = field(candidates or alternate['candidates'], [
        '铭牌规格未完整提取，使用关联送货物理行规格补充，请核对原图。'] if candidates or alternate['candidates'] else primary['warnings'])
    result['source_role'] = next(iter(roles)) if len(roles) == 1 else 'mixed'
    return result
