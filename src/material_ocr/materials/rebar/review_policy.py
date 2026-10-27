"""Separate successful association notes from actionable field problems.

Values, evidence and association choices are immutable here. Missing and
unresolved fields keep their existing checks. Only explicitly known notes on
an independently associated field can be cleared; unfamiliar issues remain.
"""
from copy import deepcopy
import re

REVIEW_POLICY_VERSION = 'matched-fields-conflicts-v1'


def _reason(issue):
    return str(issue.get('reason') or issue.get('code') or '') if isinstance(issue, dict) else str(issue)


def _informational(issue):
    reason = _reason(issue)
    if reason in ('certificate_label_ocr_variant', 'nameplate_manufacturer_english_only',
                  'glm_output_truncated', 'glm_repeated_text'):
        return True
    if reason.startswith('检测到重复质保书编号') and '只合并为核验来源' in reason and not any(
            word in reason for word in ('冲突', '矛盾', '不同')):
        return True
    if 'nameplate_damaged_heat_label_confirmed_by_certificate' in reason:
        return True
    if 'mixed_roi_card_split_review' in reason and 'nameplate_grade_view_conflict' not in reason:
        return True
    if reason.startswith('铭牌牌号「') and '按数字部分' in reason and '和严格规格' in reason and '关联' in reason:
        return True
    return any(phrase in reason for phrase in (
        '铭牌已按独立图像窗口的唯一牌号、规格和炉批号关联',
        '补读视图与同一原页文字段共享唯一完整铭牌键，已合并OCR证据',
        '混合表单文字段与独立铭牌裁区的炉批号/牌号/规格一致且几何一一对应',
        '已按唯一证书炉批号精确关联该炉号唯一完整牌号/规格/炉批号标签组',
        '按唯一相邻附件组关联；裁区不是已验证卡片边界',
        '因清单证号定位的唯一证书炉批号与该炉号唯一完整铭牌标签组精确一致，已沿证号路线关联',
        '按相邻附件组关联并保留人工核验提示',
        '同页送货单表单已定位，货物行规格/牌号已匹配；',
    ))


def _associated(record, column):
    sources = record.get('sources') or {}
    role = {'规格品种': 'nameplate', '生产单位': 'nameplate', '炉批号': 'nameplate',
            '供应单位': 'delivery', '送货单编号': 'delivery', '质保书编号': 'certificate'}.get(column)
    return bool(role and sources.get(role))


def apply_review_policy(ledger):
    """Mutate display checks only, preserving raw notes for source inspection."""
    ledger['review_policy_version'] = REVIEW_POLICY_VERSION
    for record in ledger.get('records', []):
        notes = list(record.get('match_notes') or [])
        issues = []
        for issue in record.get('diagnostics') or []:
            if _informational(issue):
                if issue not in notes: notes.append(deepcopy(issue))
            else:
                issues.append(issue)
        record['diagnostics'] = issues
        record['match_notes'] = notes
        for column, field in record.get('fields', {}).items():
            # Human edits/confirmations retain their own state.
            if field.get('manual') or field.get('status') in ('manual', 'corrected', 'verified'):
                continue
            if not field.get('value') or not field.get('evidence') or not _associated(record, column):
                continue
            remaining, notes = [], list(field.get('review_notes') or [])
            for issue in field.get('diagnostics') or []:
                text = _reason(issue)
                # A plate's view conflict is unrelated to the delivery header.
                unrelated_plate = column in ('供应单位', '送货单编号') and (
                    text.startswith('独立铭牌补读与混合页读数') or
                    re.match(r'铭牌第\d+页(?:核验提示|牌号存在其他OCR读数候选)', text))
                # A heat discrepancy does not invalidate an independently
                # confirmed certificate number or company name.
                unrelated_heat = column != '炉批号' and '炉批号' in text and (
                    '仅有一个字母差异' in text or
                    (isinstance(issue, dict) and issue.get('code') == 'certificate_nameplate_heat_conflict'))
                if _informational(issue) or unrelated_plate or unrelated_heat:
                    if issue not in notes: notes.append(deepcopy(issue))
                else:
                    remaining.append(issue)
            field['review_notes'] = notes
            field['diagnostics'] = remaining
            if notes and not remaining and not field.get('conflict'):
                field.update(status='observed', found_in_ocr=True, review_required=False)
            elif remaining or field.get('conflict'):
                field.update(found_in_ocr=False, review_required=True)
            elif field.get('status') in (None, '', 'observed'):
                field['review_required'] = False
        fields = record.get('fields') or {}
        required = ('生产单位', '供应单位', '送货单编号', '炉批号', '质保书编号')
        if all((record.get('sources') or {}).get(role) for role in ('nameplate', 'delivery', 'certificate')) and all(
                fields.get(key, {}).get('value') and fields[key].get('status') == 'observed'
                and not fields[key].get('diagnostics') for key in required):
            record['match_status'] = 'matched'
    return ledger
