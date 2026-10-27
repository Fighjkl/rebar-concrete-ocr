"""Trusted bundle reconstruction and field-review validation for ledger writers."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

INVENTORIES = dict(certificate='certificate_page_inventory', nameplate='nameplate_inventory', delivery='delivery_inventory')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def active_bundles(result, state):
    """Use server candidates and records; client snapshots cannot change OCR."""
    if state and (state.get('pdf_sha256') != result['pdf_sha256'] or state.get('schema_version') != 'four-form-review-state-v1'):
        raise ValueError('审核状态与当前 PDF 或审核版本不一致')
    groups = {g['group_id']: g for g in result.get('association_groups', [])}
    inventories = {role: {c['candidate_id']: c for c in result.get(name, [])} for role, name in INVENTORIES.items()}
    released = state.get('released_auto_groups', [])
    manual = state.get('manual_bundles', {})
    if not isinstance(released, list) or not isinstance(manual, dict) or any(g not in groups for g in [*released, *manual]):
        raise ValueError('审核状态中包含不存在的物料分组')
    bundles = [deepcopy(b) for b in result.get('four_form_bundles', [])
               if b['group_id'] not in released and b['group_id'] not in manual]
    for group_id, incoming in manual.items():
        if incoming.get('state') != 'human_confirmed' or incoming.get('group_id') != group_id or not incoming.get('confirmed_at'):
            raise ValueError('人工捆绑缺少确认记录')
        forms = {}
        for role in INVENTORIES:
            candidate_id = incoming.get('forms', {}).get(role, {}).get('candidate_id')
            if candidate_id not in inventories[role]:
                raise ValueError('所选表单不在当前 PDF 的候选清单中')
            forms[role] = deepcopy(inventories[role][candidate_id])
        members = [e for e in result['entries'] if e['association']['group_id'] == group_id]
        forms['inspection'] = [dict(record_id=e['record_id'], page_no=e['record']['page_no'], row_no=e['record'].get('row_no')) for e in members]
        bundles.append(dict(bundle_id='bundle-'+group_id.removeprefix('group-'), group_id=group_id,
            record_ids=[e['record_id'] for e in members], state='human_confirmed', forms=forms,
            confirmed_at=incoming['confirmed_at'], comment=str(incoming.get('comment', ''))[:4000],
            shared_certificate_confirmed=incoming.get('shared_certificate_confirmed') is True))
    used = {role: {} for role in INVENTORIES}
    for bundle in bundles:
        for role in INVENTORIES:
            candidate_id = bundle['forms'][role]['candidate_id']
            previous = used[role].get(candidate_id)
            if previous and (role != 'certificate' or not (bundle.get('shared_certificate_confirmed') or previous.get('shared_certificate_confirmed'))):
                raise ValueError('同一铭牌或送货行被重复捆绑，或共用证书未确认')
            used[role][candidate_id] = bundle
    return sorted(bundles, key=lambda b: (b['forms']['nameplate']['page_no'], b['forms']['nameplate']['candidate_id']))


def validate_values(values, columns):
    errors, cleaned = [], {}
    for column in columns:
        key, label = column['key'], column['label']
        value = values.get(key, '')
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            errors.append(f'{label}必须是文字或数值')
            value = ''
        text = str(value).strip()
        if len(text) > 500 or any(ord(ch) < 32 for ch in text):
            errors.append(f'{label}过长或包含无效字符')
        if not text and column.get('required'):
            errors.append(f'{label}未填写')
        if text and column.get('type') == 'number':
            try:
                # Public input is explicitly in tonnes; kg must be converted by the reviewer.
                quantity = Decimal(re.sub(r'\s*(?:t|吨)\s*$', '', text, flags=re.I))
                if not quantity.is_finite() or quantity <= 0 or quantity > Decimal('1000000'):
                    raise InvalidOperation
                text = format(quantity, 'f')
            except InvalidOperation:
                errors.append(f'{label}须填写大于零的吨数（例如 38.855）')
        if text and column.get('type') == 'date':
            try:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
                    raise ValueError
                datetime.strptime(text, '%Y-%m-%d')
            except ValueError:
                errors.append(f'{label}须填写有效日期 YYYY-MM-DD')
        cleaned[key] = text
    return cleaned, errors


def preview_ledger(result, state):
    profile = result.get('filling_profile')
    if not profile:
        raise ValueError('这份旧结果尚无填表字段，请用保存的 OCR 重新生成匹配页')
    bundles = active_bundles(result, state)
    columns, contexts = profile['columns'], profile.get('approval_contexts', [])
    context_by_id = {c['context_id']: c for c in contexts}
    keys = ['material_name', 'usage_location', 'supply_date']
    identical = len({tuple(c['fields'][k]['value'] for k in keys) for c in contexts}) == 1
    default_context = contexts[0]['context_id'] if contexts and identical else ''
    reviews = state.get('field_reviews', {})
    if not isinstance(reviews, dict):
        raise ValueError('字段审核状态格式不正确')
    profile_version = digest(profile)
    entry_by_id = {e['record_id']: e['record'] for e in result.get('entries', [])}
    rows = []
    for bundle in bundles:
        group_id, forms = bundle['group_id'], bundle['forms']
        signature = digest(dict(pdf=result['pdf_sha256'], profile=profile_version, group_id=group_id,
            field_policy='rebar-inspection-first-v2' if profile['material_id'] == 'rebar' else None,
            inspection_records={rid: entry_by_id.get(rid) for rid in bundle['record_ids']},
            selections={role: forms[role]['candidate_id'] for role in INVENTORIES},
            state=bundle['state'], confirmed_at=bundle.get('confirmed_at')))
        saved = reviews.get(group_id, {})
        current = saved.get('signature') == signature
        context_id = saved.get('context_id', default_context) if current else default_context
        context = context_by_id.get(context_id)
        if context_id and not context:
            raise ValueError('所选报审表不在当前结果中')
        fields = deepcopy(context['fields']) if context else {}
        for role in INVENTORIES:
            fields.update(deepcopy(profile['resources'][role].get(forms[role]['candidate_id'], {})))
        if profile['material_id'] == 'rebar':
            from material_ocr.materials.rebar.ledger_sources import (inspection_fields,
                resolve_quantity, resolve_specification, sourced)
            resources = {role: {key: sourced(info, role, dict(candidate_id=forms[role]['candidate_id'],
                page_no=forms[role]['page_no'], row_id=forms[role].get('row_id')))
                for key, info in profile['resources'][role].get(forms[role]['candidate_id'], {}).items()}
                for role in INVENTORIES}
            for resource in resources.values():
                fields.update(resource)
            inspections = [sourced(profile['resources'].get('inspection', {}).get(rid,
                inspection_fields(entry_by_id.get(rid, {}), rid))['quantity_t'], 'inspection')
                for rid in bundle['record_ids']]
            fields['specification'] = resolve_specification(resources['nameplate'], resources['delivery'])
            fields['quantity_t'] = resolve_quantity(inspections, resources['delivery'].get('quantity_t',
                sourced(None, 'delivery')))
        for column in columns:
            fields.setdefault(column['key'], dict(value='', status='missing', candidates=[], warnings=[]))
        extracted = {column['key']: fields[column['key']]['value'] for column in columns}
        edited = saved.get('values', {}) if current else {}
        if not isinstance(edited, dict) or set(edited)-set(extracted):
            raise ValueError('字段补正包含不支持的字段')
        values, errors = validate_values(dict(extracted, **edited), columns)
        for key in values:
            fields[key]['reviewed_value'] = values[key]
            fields[key]['corrected'] = values[key] != extracted[key]
        if contexts and not context:
            errors.append('请先选择正确的报审表页')
        approval = current and saved.get('confirmed') is True and bool(saved.get('confirmed_at')) and not errors
        if saved.get('confirmed') and not current:
            errors.append('表单关联或提取版本已变化，请重新确认字段')
        rows.append(dict(row_id=forms['nameplate']['candidate_id'], group_id=group_id,
            bundle_id=bundle['bundle_id'], bundle_state=bundle['state'], signature=signature,
            context_id=context_id, fields=fields, values=values, errors=errors,
            approved=approval, confirmed_at=saved.get('confirmed_at') if approval else None,
            review_origin=saved.get('review_origin', 'browser_field_review') if current else None,
            review_note=str(saved.get('note', ''))[:4000] if current else '',
            record_ids=bundle['record_ids'], sources={role: dict(candidate_id=forms[role]['candidate_id'],
                page_no=forms[role]['page_no'], row_id=forms[role].get('row_id')) for role in INVENTORIES},
            inspection_pages=sorted({p['page_no'] for p in forms['inspection']}),
            pdf_sha256=result['pdf_sha256'], pdf_name=result.get('pdf_name', '')))
    pending = len(result.get('association_groups', []))-len(bundles)
    return dict(schema_version='material-ledger-preview-v1', material_id=profile['material_id'],
        pdf_sha256=result['pdf_sha256'], pdf_name=result.get('pdf_name', ''),
        columns=columns, approval_contexts=contexts, rows=rows,
        counts=dict(bundled_rows=len(rows), approved_rows=sum(r['approved'] for r in rows),
                    pending_matching_groups=pending), quantity_unit=profile.get('quantity_unit'))
