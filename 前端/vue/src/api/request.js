import axios from 'axios'
import { ElMessage } from 'element-plus'

const request = axios.create({
  baseURL: '/',
  timeout: 0,
  headers: { 'Content-Type': 'application/json' }
})

request.interceptors.response.use(
  response => {
    const data = response.data
    if (data && typeof data === 'object' && 'code' in data) {
      if (data.code === 200) return data
      ElMessage.error(data.msg || '请求失败')
      const err = new Error(data.msg || '请求失败')
      err.code = data.code
      err.data = data.data
      return Promise.reject(err)
    }
    return { code: 200, data }
  },
  error => {
    const body = error.response && error.response.data
    // Axios may throw a JSON parsing SyntaxError when Spring rejects a CORS
    // preflight with its plain-text "Invalid CORS request" response.  Prefer
    // the server's actionable message so the user is not shown the parser
    // exception (and keep the original error for callers that need status).
    let msg = body && typeof body === 'object' ? body.msg : ''
    if (!msg && typeof body === 'string' && body.trim()) {
      msg = body.trim() === 'Invalid CORS request'
        ? '服务端跨域配置拒绝了请求，请重启更新后的 Java 服务后重试'
        : body.trim()
    }
    msg = msg || error.message
    ElMessage.error(msg || '网络错误')
    return Promise.reject(error)
  }
)

export default request
