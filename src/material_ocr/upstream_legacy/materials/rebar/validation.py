"""辅助序号与核心检索字段分离；任何规则通过都不等同人工确认。"""


def validation_states(record):
    row_issues=[]
    core_issues=[]
    other_issues=[]
    core_fields={'specification','weight','manufacturer_certificate'}
    for issue in record['issues']:
        if issue.startswith(('row_number_','row_retry_')) or issue=='low_or_missing_ocr:row_no':
            row_issues.append(issue)
        elif (issue.startswith(('weight_','manufacturer_certificate_','certificate_','specification_'))
              or issue=='visible_ink_but_no_ocr_text'
              or issue in {f'low_or_missing_ocr:{f}' for f in core_fields}):
            core_issues.append(issue)
        else:
            other_issues.append(issue)
    for field in ('weight_t','certificate_no','manufacturer','diameter_mm'):
        if record.get(field) in (None,''):
            core_issues.append(f'missing_core_field:{field}')
    if record.get('row_no') is None and not row_issues:
        row_issues.append('row_number_missing_or_invalid')
    return {
        'row_number_status':'needs_review' if row_issues else 'rule_pass_review_required',
        'row_number_issues':row_issues,
        'search_fields_status':'needs_review' if core_issues else 'rule_pass_review_required',
        'search_field_issues':core_issues,
        'other_field_issues':other_issues,
        'ready_for_search':False,
    }
