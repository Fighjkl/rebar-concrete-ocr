export function formatDate(value) {
  if (!value) return ''
  const d = new Date(value)
  if (isNaN(d.getTime())) return value
  const pad = (n) => (n < 10 ? '0' + n : '' + n)
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds())
}

export function statusText(status) {
  return ({
    READY: '待识别',
    PROCESSING: '识别中',
    SUCCESS: '已完成',
    FAILED: '失败'
  })[status] || (status || '')
}

export function tagType(status) {
  return ({ SUCCESS: 'success', FAILED: 'danger', PROCESSING: 'warning', READY: 'info' })[status] || ''
}