// Presentation only: original diagnostics, evidence and candidate order stay intact.
const FIELD_ALIASES = {
  供货日期: '供货日期与工程材料报审签认日期',
  质证编号: '质保书编号', 质量证明书编号: '质保书编号', 质保证明书编号: '质保书编号',
  规格: '规格品种', 牌号: '规格品种', 型号: '规格品种',
  document_no: '文件编号', material_name: '材料名称', supply_date: '供货日期与工程材料报审签认日期',
  entry_date: '供货日期与工程材料报审签认日期', use_location: '使用部位', usage_location: '使用部位',
  manufacturer: '生产单位', supplier: '供应单位', delivery_no: '送货单编号',
  grade: '规格品种', specification: '规格品种', diameter_mm: '规格品种',
  weight: '验收数量', quantity_t: '验收数量', weight_t: '验收数量',
  certificate_no: '质保书编号', heat_lot_no: '炉批号'
}
const MATCH_FIELDS = ['规格品种', '生产单位', '供应单位', '送货单编号', '炉批号', '质保书编号']
const CODE_FIELDS = {
  match_not_found: MATCH_FIELDS, multiple_match_candidates: MATCH_FIELDS,
  specification_mismatch_candidates: MATCH_FIELDS, duplicate_resource: MATCH_FIELDS,
  nameplate_keywords_unverified: MATCH_FIELDS,
  conflicting_grade_columns: MATCH_FIELDS, model_or_grade_unreadable: MATCH_FIELDS,
  specification_or_grade_unreadable: MATCH_FIELDS,
  certificate_not_found: ['质保书编号', '炉批号'], certificate_not_unique: ['质保书编号', '炉批号'],
  certificate_nameplate_heat_conflict: ['质保书编号', '炉批号', '规格品种', '生产单位'],
  duplicate_entry_record: ['验收数量', '规格品种'],
  certificate_number_unreadable: ['质保书编号'],
  delivery_not_found: ['供应单位', '送货单编号'], delivery_not_unique: ['供应单位', '送货单编号'],
  delivery_row_unresolved: ['供应单位', '送货单编号'],
  delivery_keywords_unverified: ['供应单位', '送货单编号', '规格品种'],
  duplicate_delivery_row: ['供应单位', '送货单编号', '验收数量'],
  entry_quantity_unreadable: ['验收数量'], entry_quantity_unit_missing: ['验收数量']
}
const REASONS = {
  low_ocr_confidence: 'OCR 置信度不足，请核对原图',
  body_context_anchors_incomplete: '报审表正文定位信息不完整',
  document_no_incomplete_or_prefix_only: '文件编号不完整或仅识别到前缀',
  document_no_visual_review_required: '文件编号需要核对原图',
  document_no_format_uncertain: '文件编号格式不确定',
  not_detected_requires_review: '未识别到，需要核对原图',
  required_field_not_detected: '必填字段未识别到',
  date_missing_or_multiple: '日期缺失或存在多个值', invalid_calendar_date: '日期无效',
  field_contains_form_labels_check_crop: '字段混入表单标签，请核对原图',
  material_not_confirmed_as_rebar: '材料名称尚未确认为钢筋',
  conflicting_grade_columns: '进场清单的牌号列与规格列矛盾',
  model_or_grade_unreadable: '型号或牌号未清晰识别',
  specification_or_grade_unreadable: '规格或牌号未清晰识别',
  certificate_number_unreadable: '质保书编号未清晰识别',
  entry_quantity_unreadable: '进场数量未清晰识别',
  entry_quantity_unit_missing: '进场数量缺少单位证据',
  missing_keyword: '关键词未识别到', conflicting_values: '识别值存在冲突',
  unverified_keyword: '关键词尚未核验成功', unclear_ocr: 'OCR 识别不清晰'
}

const fieldName = name => FIELD_ALIASES[name] || name
function stable(value) {
  if (Array.isArray(value)) return value.map(stable)
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k => [k, stable(value[k])]))
  return value
}
function issueKey(issue) {
  const locations = (issue.locations || []).map(loc => JSON.stringify(stable(loc))).sort()
  return JSON.stringify([issue.code || '', issue.field || '', issue.reason || '', locations])
}
export function uniqueDiagnostics(issues = []) {
  const seen = new Set()
  return issues.filter(issue => {
    const key = issueKey(issue)
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}
function affectsField(issue, label) {
  const col = fieldName(label)
  if ((issue.field || '').split(/[、/，,]/).some(name => fieldName(name.trim()) === col)) return true
  if (CODE_FIELDS[issue.code]?.includes(col)) return true
  if (issue.code?.startsWith('low_or_missing_ocr:')) {
    return fieldName(issue.code.slice('low_or_missing_ocr:'.length)) === col
  }
  return false
}
export function fieldReviewDiagnostics(row, label) {
  // Always retain the cell's own issues, including unfamiliar future codes.
  const own = row.cells?.[label]?.diagnostics || []
  const all = uniqueDiagnostics([...own, ...(row.diagnostics || [])])
  const ownKeys = new Set(own.map(issueKey))
  const confirmed = row.cells?.[label]?.found_in_ocr === true
  const issues = all.filter(issue => ownKeys.has(issueKey(issue)) || (!confirmed && affectsField(issue, label)))
  const shown = new Set(issues.map(issueKey))
  return { issues, otherIssues: all.filter(issue => !shown.has(issueKey(issue))) }
}
export function diagnosticText(issue) {
  const reason = issue.reason || issue.code || '待核验异常'
  if (REASONS[reason]) return REASONS[reason]
  const separator = reason.indexOf(':')
  if (separator > 0) {
    const name = reason.slice(0, separator), detail = reason.slice(separator + 1)
    if (name === 'low_or_missing_ocr') return `${fieldName(detail)}：OCR 置信度不足或缺失`
    if (FIELD_ALIASES[name] && REASONS[detail]) return `${fieldName(name)}：${REASONS[detail]}`
  }
  // Some backend row issues include a Chinese introduction plus the raw code.
  if (reason === 'OCR字段或表格未检验成功：' + issue.code) {
    return '进场清单：' + diagnosticText({ code: issue.code })
  }
  return reason
}
