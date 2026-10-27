import request from './request'

// 总台账：跨 PDF 聚合视图，所有取自 ledger.json（只读）。

export function getMasterLedger(materialType = 'rebar') {
  return request({ url: '/api/master-ledger', method: 'get', params: { material_type: materialType } })
}

export function exportMasterLedger(format = 'xlsx', materialType = 'rebar') {
  // 直接下载：让浏览器处理 attachment 头，axios 不参与。
  const fmt = format || 'xlsx'
  window.location.href = '/api/master-ledger/export?format=' + encodeURIComponent(fmt) + '&material_type=' + encodeURIComponent(materialType)
}

export function patchMasterField(pdfName, recordIndex, field, value) {
  return request({
    url: '/api/library/' + encodeURIComponent(pdfName) + '/ledger/field',
    method: 'patch',
    data: { record_index: recordIndex, field, value }
  })
}

export function clearAllLedgers() {
  return request({ url: '/api/master-ledger/cache', method: 'delete' })
}

// 单卷台账：与总台账同款行列结构（columns + rows），供单卷详情页以总台账样式渲染。
export function getPdfLedgerRows(pdfName) {
  return request({
    url: '/api/library/' + encodeURIComponent(pdfName) + '/ledger/rows',
    method: 'get'
  })
}

// 导出单卷台账：直接下载（浏览器处理 attachment 头）。
export function exportPdfLedger(pdfName, format = 'xlsx') {
  const fmt = format || 'xlsx'
  window.location.href = '/api/library/' + encodeURIComponent(pdfName) + '/export?format=' + encodeURIComponent(fmt)
}
