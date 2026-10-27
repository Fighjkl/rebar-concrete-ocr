"""Concrete entries: keep the first observed certificate row and every source."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .extraction import normalized


_EMPTY_CERTIFICATES = {'', '-', '--', '—', '——', '/', '无', '空', '未填', '未填写',
                       '暂无', '不适用', 'N/A', 'NONE', 'NULL', '?', '未识别', '未知'}
_FIELDS = {
    'document_no': '文件编号', 'material_name': '材料名称', 'specification': '规格品种',
    'producer': '生产单位', 'supplier': '供应单位', 'supply_date': '供货日期',
    'entry_date': '进场日期', 'use_location': '使用部位', 'batch_no': '炉批号',
    'quantity_m3': '验收数量',
}


def _certificate_key(value):
    key = normalized(value).upper()
    return '' if key in _EMPTY_CERTIFICATES else key


def _source(row):
    evidence = deepcopy(row.get('field_evidence') or {})
    location = deepcopy(evidence.get('warranty_no') or {})
    location.setdefault('page_no', row.get('page_no'))
    location.update(record_id=row.get('record_id'), row_no=row.get('row_no'),
                    field='质保书编号')
    location.setdefault('raw_text', row.get('warranty_no', ''))
    return {'record_id': row.get('record_id'), 'page_no': row.get('page_no'),
            'row_no': row.get('row_no'),
            'values': {key: deepcopy(row.get(key, '')) for key in ('warranty_no', *_FIELDS)},
            'field_evidence': evidence, 'locations': [location],
            'issues': deepcopy(row.get('issues', []))}


def _conflicts(group):
    conflicts = {}
    for key in _FIELDS:
        observed = {}
        for row in group:
            raw = row.get(key)
            if raw is None or not str(raw).strip():
                continue
            value = normalized(raw).upper()
            if key == 'quantity_m3':
                try:
                    number = Decimal(value)
                    if number.is_finite():
                        value = str(number.normalize())
                except InvalidOperation:
                    pass
            observed.setdefault(value, []).append(raw)
        if len(observed) > 1:
            conflicts[key] = list(dict.fromkeys(raw for values in observed.values() for raw in values))
    return conflicts


def deduplicate_certificate_records(records):
    """Deduplicate one PDF before attachment matching, using observed IDs only.

    Input is in PDF page order and physical extraction order within each page.
    Missing IDs and IDs later inferred from attachments never merge entries.
    Later rows cannot replace or fill the first row's values or sum its volume.
    """
    rows = deepcopy(records)
    groups = {}
    for row in rows:
        key = _certificate_key(row.get('warranty_no'))
        if key:
            groups.setdefault(key, []).append(row)
    removed, summaries = set(), []
    for key, group in groups.items():
        if len(group) < 2:
            continue
        first = group[0]
        sources = [_source(row) for row in group]
        conflicts = _conflicts(group)
        locations = [location for source in sources for location in source['locations']]
        reason = (f'重复质保书编号“{first["warranty_no"]}”，台账保留首次提取记录；'
                  '重复来源保留供核验，未累加验收数量。')
        if conflicts:
            reason += '重复行字段不一致：' + '、'.join(_FIELDS[name] for name in conflicts) + '；仍保留首条值。'
        diagnostic = {
            'code': 'duplicate_certificate_record_conflict' if conflicts else 'duplicate_certificate_record_merged',
            'field': '质保书编号', 'reason': reason, 'locations': locations,
            'certificate_no': first['warranty_no'], 'conflicts': conflicts,
            'page_nos': list(dict.fromkeys(source['page_no'] for source in sources)),
            'action': 'keep_first_merge_evidence_do_not_sum',
        }
        first.update(duplicate_source_records=sources,
                     duplicate_record_ids=[row.get('record_id') for row in group[1:]],
                     deduplication_conflicts=conflicts,
                     certificate_deduplication_diagnostics=[diagnostic])
        summaries.append({'certificate_no': first['warranty_no'], 'normalized_certificate_no': key,
                          'kept_record_id': first.get('record_id'), 'kept_page_no': first.get('page_no'),
                          'merged_record_ids': first['duplicate_record_ids'],
                          'page_nos': diagnostic['page_nos'], 'conflicts': conflicts})
        removed.update(id(row) for row in group[1:])
    kept = [row for row in rows if id(row) not in removed]
    return kept, {'input_record_count': len(rows), 'output_record_count': len(kept),
                  'merged_record_count': len(removed), 'groups': summaries,
                  'key': 'normalized_nonempty_observed_warranty_no',
                  'scope': 'single_pdf', 'first_record': 'page_order_then_extraction_order',
                  'quantities_summed': False}
