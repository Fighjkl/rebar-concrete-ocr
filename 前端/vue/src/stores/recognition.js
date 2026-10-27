import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

const ACTIVE = new Set(['submitting', 'queued', 'running', 'reconnecting'])
const PHASES = {
  no_entry: '未检测到进场表，切换铭牌索引支路',
  nameplate_index: '汇总铭牌并跨页匹配附件',
  starting: '准备混合识别流程', classify: '阶段1 Paddle 页面分类',
  extract: '阶段2 报审表 Paddle / 进场表 GLM', inspection: '进场表单元格识别',
  entry_review: '检查进场表提取结果',
  upstream: 'Paddle 筛页与 GLM 进场表识别', recover: '校验并恢复进场表结果',
  attachments: '阶段3 Paddle 整页附件 OCR',
  matching: '阶段4 自动匹配', archiving: '阶段5 整理缓存', saving: '阶段5 保存结果'
}

const PAGE_PROGRESS_STAGES = new Set(['classify', 'extract', 'attachments'])
const CELL_PROGRESS_STAGES = new Set(['inspection'])

export function recognitionLabel(task) {
  if (!task) return ''
  if (task.connectionError) return '连接暂时中断，正在恢复任务状态…'
  if (task.status === 'queued') return '等待本地 OCR' + (task.queue_position ? '（队列第 ' + task.queue_position + ' 位）' : '')
  if (task.status === 'submitting') return '正在提交任务…'
  const label = PHASES[task.stage] || task.message || '识别中…'
  if (!(task.total > 0) || (!PAGE_PROGRESS_STAGES.has(task.stage) && !CELL_PROGRESS_STAGES.has(task.stage))) return label
  const unit = CELL_PROGRESS_STAGES.has(task.stage) ? '单元格' : '页'
  return label + ' ' + task.current + '/' + task.total + ' ' + unit
}

export const useRecognitionStore = defineStore('recognition', () => {
  const tasks = ref({})
  const polling = new Set()
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms))

  async function call(name, method = 'GET', body) {
    const controller = new AbortController()
    // Short requests only; aborting a status request never starts another job.
    const timeout = setTimeout(() => controller.abort(), 20000)
    try {
      const response = await fetch('/api/library/' + encodeURIComponent(name) + '/recognition', {
        method, signal: controller.signal,
        ...(body ? { body, headers: { 'Content-Type': 'application/x-www-form-urlencoded' } } : {})
      })
      const result = await response.json()
      if (!response.ok || (result.code && result.code !== 200)) throw new Error(result.msg || result.detail || ('HTTP ' + response.status))
      return result.data || result
    } finally { clearTimeout(timeout) }
  }

  function update(name, task) {
    tasks.value[name] = { ...task, connectionError: null }
    return tasks.value[name]
  }

  async function poll(name) {
    if (polling.has(name)) return
    polling.add(name)
    try {
      while (true) {
        try {
          const task = update(name, await call(name))
          if (!ACTIVE.has(task.status)) return
        } catch (error) {
          tasks.value[name] = { ...tasks.value[name], status: tasks.value[name]?.status || 'reconnecting', connectionError: error.message }
        }
        await wait(tasks.value[name]?.connectionError ? 5000 : 1200)
      }
    } finally { polling.delete(name) }
  }

  async function track(name) {
    if (!name || polling.has(name)) return
    try {
      const task = update(name, await call(name))
      if (ACTIVE.has(task.status)) void poll(name)
      return task
    } catch (error) {
      // The uploaded PDF remains usable while the OCR service reconnects.
      tasks.value[name] = { ...tasks.value[name], status: 'reconnecting', connectionError: error.message }
      void poll(name)
    }
  }

  async function start(name, body) {
    if (!name || isRunning(name)) return tasks.value[name]
    tasks.value[name] = { status: 'submitting', stage: 'starting' }
    try {
      const task = update(name, await call(name, 'POST', body))
      if (ACTIVE.has(task.status)) void poll(name)
      return task
    } catch (error) {
      // A lost POST response is ambiguous: query its job, never repost it.
      try {
        const task = update(name, await call(name))
        if (ACTIVE.has(task.status)) { void poll(name); return task }
        if (task.status === 'completed') return task
      } catch (statusError) {
        tasks.value[name] = { status: 'reconnecting', connectionError: statusError.message }
        void poll(name)
        return tasks.value[name]
      }
      tasks.value[name] = { status: 'failed', stage: 'failed', error: error.message }
      throw error
    }
  }

  function isRunning(name) { return !!name && ACTIVE.has(tasks.value[name]?.status) }
  const runningNames = computed(() => Object.keys(tasks.value).filter(isRunning))
  const runningCount = computed(() => runningNames.value.length)
  return { tasks, start, track, isRunning, runningNames, runningCount }
})
