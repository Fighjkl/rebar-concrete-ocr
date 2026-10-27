// Spring/proxy failures can return text instead of the API JSON envelope.
// Keep the real failure visible instead of exposing a JSON SyntaxError.
export async function readApiResponse(response, fallback = '请求失败') {
  const text = await response.text()
  let body
  try {
    body = JSON.parse(text)
  } catch {
    const detail = text.trim() === 'Invalid CORS request'
      ? '服务端跨域配置拒绝了请求，请重启更新后的 Java 服务后重试'
      : '服务端返回了非 JSON 响应'
    throw new Error(`${fallback}（HTTP ${response.status}）：${detail}`)
  }
  if (!response.ok || body?.code !== 200) {
    throw new Error(body?.msg || body?.message || `${fallback}（HTTP ${response.status}）`)
  }
  return body
}
