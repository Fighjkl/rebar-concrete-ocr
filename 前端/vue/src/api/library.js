import request from './request'

export function uploadPdf(formData) {
  return request({
    url: '/api/library/upload',
    method: 'post',
    data: formData,
    headers: { 'Content-Type': 'multipart/form-data' }
  })
}

export function listLibrary() {
  return request({ url: '/api/library', method: 'get' })
}

export function getLibraryEntry(pdfName) {
  return request({ url: '/api/library/' + encodeURIComponent(pdfName), method: 'get' })
}

export function getPageOcr(pdfName, page) {
  return request({
    url: `/api/library/${encodeURIComponent(pdfName)}/pages/${page}/ocr`,
    method: 'get'
  })
}

export function runPageOcr(pdfName, page, formData) {
  return request({
    url: `/api/library/${encodeURIComponent(pdfName)}/pages/${page}/ocr`,
    method: 'post',
    data: formData,
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
  })
}

export function recognizeLibrary(pdfName, formData) {
  return request({
    url: `/api/library/${encodeURIComponent(pdfName)}/recognize`,
    method: 'post',
    data: formData,
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
  })
}

export function getLedger(pdfName) {
  return request({
    url: `/api/library/${encodeURIComponent(pdfName)}/ledger`,
    method: 'get'
  })
}

export function clearCache(pdfName, scope) {
  return request({
    url: `/api/library/${encodeURIComponent(pdfName)}/cache?scope=${scope}`,
    method: 'delete'
  })
}

export function deleteLibrary(pdfName) {
  return request({
    url: '/api/library/' + encodeURIComponent(pdfName),
    method: 'delete'
  })
}

export function imageUrl(pdfName, page, version) {
  const v = version ? '?v=' + version : ''
  return `/api/library/${encodeURIComponent(pdfName)}/pages/${page}/image${v}`
}

export function resplitLibrary(pdfName, formData) {
  return request({
    url: `/api/library/${encodeURIComponent(pdfName)}/resplit`,
    method: 'post',
    data: formData,
    headers: { 'Content-Type': 'multipart/form-data' }
  })
}

export function rotatePage(pdfName, page, formData) {
  return request({
    url: `/api/library/${encodeURIComponent(pdfName)}/pages/${page}/rotate`,
    method: 'post',
    data: formData,
    headers: { 'Content-Type': 'multipart/form-data' }
  })
}

export function historyList() {
  return request({ url: '/api/history', method: 'get' })
}