"""Merge repeated certificate entries while retaining every observed source."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re


_CERTIFICATE_EMPTY = {'', '-', '--', '—', '——', '/', '无', '未填', '未填写', '暂无',
                      '不适用', 'N/A', 'NONE', 'NULL'}
_DEDUP_FIELDS = ('material_name', 'grade', 'diameter_mm', 'weight_t', 'manufacturer',
                 'entry_date', 'usage_location')
_EMPTY_CELL = re.compile(r'^[\s\-‐‑‒–—―_]*$')


def _placeholder_only_glm_row(row):
    """Recognize an observed template row, not a failed partial extraction.

    GLM transcribes empty ruled cells as ``---``.  The coordinate matcher
    correctly preserves that raw text, but it is not an additional entry.
    Require a complete-looking set of cell observations and no substantive
    parsed value (including the serial).  A ``?``, unreadable text, a lone
    serial, or any unassigned OCR therefore stays available for review.
    """
    raw = row.get('raw_cells')
    if (row.get('extraction_backend') != 'glm_coordinate_cell_v1' or
            not isinstance(raw, dict) or len(raw) < 3):
        return False
    if any('unresolved_ocr' in str(issue) for issue in row.get('issues', [])):
        return False
    values = list(raw.values()) + [row.get(name) for name in
        ('row_no', 'certificate_no', 'grade', 'diameter_mm', 'weight_t') + _DEDUP_FIELDS]
    return all(value is None or _EMPTY_CELL.fullmatch(str(value)) for value in values)


def _certificate_key(value):
    value = re.sub(r'\s+', '', '' if value is None else str(value)).upper()
    return '' if value in _CERTIFICATE_EMPTY else value


def _field_locations(record):
    """Return evidence with record identity attached, without inventing boxes."""
    locations = []
    for name, evidence in (record.get('field_evidence') or {}).items():
        if isinstance(evidence, dict):
            item = deepcopy(evidence)
            item.update(field=name, page_no=record.get('page_no'),
                        row_no=record.get('row_no'), record_id=record.get('record_id'),
                        source_role='inspection')
            item.setdefault('polygon_page', None)
            if item not in locations:
                locations.append(item)
    if not locations:
        evidence = deepcopy(record.get('evidence') or {})
        evidence.update(page_no=record.get('page_no'), row_no=record.get('row_no'),
                        record_id=record.get('record_id'), source_role='inspection')
        evidence.setdefault('polygon_page', None)
        locations.append(evidence)
    return locations


def _conflicting_duplicate_fields(group):
    conflicts = {}
    for name in _DEDUP_FIELDS:
        values = {}
        for row in group:
            raw = row.get(name)
            if raw is None or not str(raw).strip():
                continue
            value = re.sub(r'\s+', '', str(raw)).upper()
            if name in {'diameter_mm', 'weight_t'}:
                try:
                    number = Decimal(value)
                    if number.is_finite():
                        value = str(number.normalize())
                except InvalidOperation:
                    pass
            values.setdefault(value, []).append(raw)
        if len(values) > 1:
            conflicts[name] = list(dict.fromkeys(v for group_values in values.values() for v in group_values))
    return conflicts


def deduplicate_certificate_records(records):
    """Keep the first physical row for each non-empty certificate number.

    Repeated rows are merged as review evidence, never as quantities. GLM
    geometry rows containing only empty/dash placeholders are retained as
    located omitted-template metadata, not shown as unidentified entries.
    The returned bundle is a deep copy; raw extraction inputs remain intact.
    """
    bundle = deepcopy(records)
    previous_summary = bundle.get('certificate_number_deduplication') or {}
    omitted = deepcopy(previous_summary.get('omitted_placeholder_records', []))
    source_rows = []
    for row in bundle.get('records', []):
        if _placeholder_only_glm_row(row):
            omitted.append(dict(record_id=row.get('record_id'), page_no=row.get('page_no'),
                row_no=row.get('row_no'), pdf_sha256=row.get('pdf_sha256') or bundle.get('pdf_sha256'),
                raw_cells=deepcopy(row['raw_cells']), locations=_field_locations(row),
                issues=deepcopy(row.get('issues', [])),
                reason='GLM表格几何行仅含空白或横线占位符，无序号及有效业务字段；保留来源，不作为台账记录。'))
        else:
            source_rows.append(row)
    ordered = sorted(enumerate(source_rows), key=lambda pair: (
        pair[1].get('page_no') if pair[1].get('page_no') is not None else float('inf'),
        pair[1].get('row_no') if pair[1].get('row_no') is not None else float('inf'),
        str(pair[1].get('record_id') or ''), pair[0]))
    groups = {}
    for _, row in ordered:
        key = _certificate_key(row.get('certificate_no'))
        if key:
            pdf_sha = row.get('pdf_sha256') or bundle.get('pdf_sha256')
            groups.setdefault((pdf_sha, key), []).append(row)

    removed_ids = set()
    diagnostics = {row.get('record_id'): deepcopy(row['certificate_deduplication_diagnostics'])
                   for row in source_rows if row.get('certificate_deduplication_diagnostics')}
    summaries = deepcopy(previous_summary.get('groups', []))
    for (pdf_sha, key), group in groups.items():
        if len(group) < 2:
            continue
        first = group[0]
        first_id = first.get('record_id')
        conflicts = _conflicting_duplicate_fields(group)
        all_locations = []
        sources = []
        merged_field_evidence = first.setdefault('merged_field_evidence', {})
        for row in group:
            row_id = row.get('record_id')
            row_locations = _field_locations(row)
            all_locations.extend(loc for loc in row_locations if loc not in all_locations)
            sources.append(dict(record_id=row_id, page_no=row.get('page_no'), row_no=row.get('row_no'),
                values={name: deepcopy(row.get(name)) for name in ('certificate_no',) + _DEDUP_FIELDS},
                locations=row_locations, issues=deepcopy(row.get('issues', [])),
                source_result=row.get('source_result')))
            if row is first:
                continue
            for name, evidence in (row.get('field_evidence') or {}).items():
                merged_field_evidence.setdefault(name, [])
                item = deepcopy(evidence)
                if isinstance(item, dict):
                    item.update(field=name, page_no=row.get('page_no'), row_no=row.get('row_no'),
                                record_id=row_id, source_role='inspection')
                    item.setdefault('polygon_page', None)
                if item not in merged_field_evidence[name]:
                    merged_field_evidence[name].append(item)
            removed_ids.add(id(row))

        first['duplicate_record_ids'] = list(dict.fromkeys(
            list(first.get('duplicate_record_ids', [])) + [s['record_id'] for s in sources[1:] if s['record_id']]))
        first['duplicate_source_records'] = sources
        first['deduplication_conflicts'] = conflicts
        issue_code = 'duplicate_certificate_record_conflict' if conflicts else 'duplicate_certificate_record_merged'
        reason = (f'检测到重复质保书编号 {first.get("certificate_no") or key}，台账保留首次出现记录；'
                  '重复记录只合并为核验来源，未累加验收数量。')
        if conflicts:
            labels = {'grade': '牌号', 'diameter_mm': '规格', 'weight_t': '验收数量',
                      'manufacturer': '生产单位', 'material_name': '材料名称',
                      'entry_date': '进场日期', 'usage_location': '使用部位'}
            reason += '重复行存在字段冲突：' + '、'.join(labels.get(name, name) for name in conflicts) + '；保留首条值待核验。'
        diagnostic = dict(code=issue_code, field='质保书编号', reason=reason,
            locations=all_locations, certificate_no=first.get('certificate_no') or key,
            record_ids=[s['record_id'] for s in sources], page_nos=list(dict.fromkeys(
                s['page_no'] for s in sources if s.get('page_no') is not None)),
            conflicts=deepcopy(conflicts), source_issues=[dict(record_id=s['record_id'],
                page_no=s['page_no'], issues=deepcopy(s['issues'])) for s in sources if s.get('issues')],
            action='keep_first_merge_evidence_do_not_sum')
        diagnostics[first_id] = [diagnostic]
        first['certificate_deduplication_diagnostics'] = deepcopy(diagnostics[first_id])
        summaries = [item for item in summaries if not (
            item.get('pdf_sha256') == pdf_sha and _certificate_key(item.get('certificate_no')) == key)]
        summaries.append(dict(pdf_sha256=pdf_sha, certificate_no=first.get('certificate_no') or key,
            kept_record_id=first_id, merged_record_ids=[s['record_id'] for s in sources[1:]],
            page_nos=diagnostic['page_nos'], conflicts=deepcopy(conflicts)))

    bundle['records'] = [row for _, row in ordered if id(row) not in removed_ids]
    merged_count = sum(len(item['merged_record_ids']) for item in summaries)
    summary = dict(input_record_count=len(bundle['records']) + merged_count + len(omitted), output_record_count=len(bundle['records']),
                   merged_record_count=merged_count, groups=summaries,
                   omitted_placeholder_record_count=len(omitted), omitted_placeholder_records=omitted,
                   key='normalized_nonempty_certificate_no', first_record='page_no,row_no,record_id order',
                   quantities_summed=False)
    bundle['certificate_number_deduplication'] = summary
    return bundle, diagnostics, summary


def entry_duplicate_diagnostics(records):
    by_id = {r['record_id']: r for r in records.get('records', [])}
    diagnostics = {}
    seen = set()
    for group in records.get('duplicate_candidates', []):
        members = list(dict.fromkeys(i for i in group.get('record_ids', []) if i in by_id))
        signature = tuple(sorted(members))
        if len(members) < 2 or signature in seen:
            continue
        seen.add(signature)
        locations = []
        for record_id in members:
            record = by_id[record_id]
            evidence = record.get('field_evidence', {})
            for name in ('specification', 'weight', 'certificate_no'):
                ev = deepcopy(evidence.get(name) or {})
                if not ev and name != 'specification':
                    continue
                ev.update(page_no=record.get('page_no'), record_id=record_id,
                          source_role='inspection')
                ev.setdefault('polygon_page', None)
                if ev not in locations:
                    locations.append(ev)
        issue = dict(code='duplicate_entry_record', field='进场记录',
            reason='进场记录存在内容重复候选，保留所有来源行，未合计或自动删除；请在导出前核对是否重复登记。',
            locations=locations, record_ids=members,
            match_fields=deepcopy(group.get('match_fields', [])),
            action='review_do_not_sum_or_delete_automatically')
        for record_id in members:
            diagnostics.setdefault(record_id, []).append(deepcopy(issue))
    return diagnostics
