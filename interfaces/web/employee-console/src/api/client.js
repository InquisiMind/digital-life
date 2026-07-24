// 统一 HTTP 客户端。沿用 ContactsTab 的 safeFetch 模式：先 text 再 parse，
// 错误降级为 { error: msg }，让调用方统一用 if(d.error) 判定。

const API_KEY_HEADER = 'X-API-Key'

function pickApiKey() {
  // 后端注入或 vite 开发变量；缺失时尝试 localStorage
  if (typeof window !== 'undefined' && window.__DL_CONSOLE__?.apiKey) {
    return window.__DL_CONSOLE__.apiKey
  }
  if (typeof import.meta !== 'undefined' && import.meta.env?.VITE_API_KEY) {
    return import.meta.env.VITE_API_KEY
  }
  if (typeof localStorage !== 'undefined') {
    return localStorage.getItem('digital-life-api-key') || ''
  }
  return ''
}

// Root: 同源 ''；开发模式由 vite proxy 转 http://127.0.0.1:8642
function apiRoot() {
  if (typeof window !== 'undefined' && window.__DL_CONSOLE__?.apiRoot) {
    return window.__DL_CONSOLE__.apiRoot.replace(/\/$/, '')
  }
  return ''  // 同源
}

export async function safeFetch(path, opts = {}) {
  // path 必须以 '/' 开头（不依赖上下文）
  const url = path.startsWith('http') ? path : apiRoot() + path
  const headers = { ...(opts.headers || {}) }
  if (!headers['Content-Type'] && opts.body && typeof opts.body === 'object') {
    headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(opts.body)
  }
  const key = pickApiKey()
  if (key) headers[API_KEY_HEADER] = key

  try {
    const r = await fetch(url, { ...opts, headers })
    const text = await r.text()
    let payload
    try {
      payload = text ? JSON.parse(text) : {}
    } catch {
      payload = { error: text }
    }
    if (!r.ok && !payload.error) {
      payload.error = `HTTP ${r.status} ${r.statusText || ''}`.trim()
    }
    payload.__status = r.status
    return payload
  } catch (e) {
    return { error: e.message || String(e) }
  }
}

// 常用动词 helper
export const api = {
  get: (path, params) => {
    if (params) {
      const qs = new URLSearchParams(
        Object.fromEntries(Object.entries(params).filter(([_, v]) => v != null && v !== ''))
      ).toString()
      path = qs ? `${path}?${qs}` : path
    }
    return safeFetch(path)
  },
  post: (path, body) => safeFetch(path, { method: 'POST', body }),
  patch: (path, body) => safeFetch(path, { method: 'PATCH', body }),
  put: (path, body) => safeFetch(path, { method: 'PUT', body }),
  delete: (path) => safeFetch(path, { method: 'DELETE' }),
}

// === System-wide API（跨实例）===========================================

export const systemApi = {
  overview: () => api.get('/api/system/overview'),
  instances: () => api.get('/api/system/instances'),
  createInstance: (body) => api.post('/api/system/instances', body),
  patchInstance: (iid, body) => api.patch(`/api/system/instances/${iid}`, body),
  deleteInstance: (iid) => api.delete(`/api/system/instances/${iid}`),
  resetAffair: (iid, body) => api.post(`/api/system/instances/${iid}/affairs/reset`, body),
  abortWake: (iid, wakeId, reason) => api.post(`/api/system/instances/${iid}/wakes/${wakeId}/abort`, { reason }),
  setInstanceActive: (iid, active, reason) => api.post(`/api/system/instances/${iid}/active`, { active, reason }),
  wechatLogin: (iid, onProgress) => {
    // 长轮询（最多 120s），用 AbortController 超时控制
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 130000)
    return safeFetch(`/api/system/instances/${iid}/wechat-login`, {
      method: 'POST',
      signal: controller.signal,
      body: JSON.stringify({}),
    }).finally(() => clearTimeout(timeout))
  },
  wechatQrcode: (iid) =>
    safeFetch(`/api/system/instances/${iid}/wechat-login/qrcode`, { method: 'POST', body: JSON.stringify({}) }),
  wechatLoginStatus: (iid) =>
    safeFetch(`/api/system/instances/${iid}/wechat-login/status`),
  gatewayRestart: (reason) => api.post('/api/system/gateway/restart', { reason }),
  // 汇总多实例 token 用量 —— 给 OverviewView 系统级走势图, 不带 iid
  systemBudgetSeries: (hours = 24, bucket = 'hour') => api.get('/api/system/budget/series', { hours, bucket }),
  projects: (iid) => api.get('/api/system/projects', iid ? { iid } : {}),
  projectDetail: (pid) => api.get(`/api/system/projects/${pid}`),
  projectTasks: (pid) => api.get(`/api/system/projects/${pid}/tasks`),
  createProject: (body) => api.post('/api/system/projects', body),
  deleteProject: (pid) => api.delete(`/api/system/projects/${pid}`),
  skills: (iid) => api.get('/api/system/skills', iid ? { iid } : {}),
  subscribeSkill: (instanceId, skill, subscribed) =>
    api.post('/api/system/skills/subscribe', { instance_id: instanceId, skill, subscribed }),
  eventTypes: () => api.get('/api/system/event-types'),
  createEventType: (body) => api.post('/api/system/event-types', body),
  updateEventType: (typeId, body) => api.put(`/api/system/event-types/${typeId}`, body),
  deleteEventType: (typeId) => api.delete(`/api/system/event-types/${typeId}`),
}

// === Instance-scoped API ===============================================
// 注意：旧 employee_console 的 API 是按 instance 染色的 /api/employee/{iid}/...，
// 这里包一层让 view 简洁地使用。

export const instanceApi = (iid) => ({
  status:        () => api.get(`/api/employee/${iid}/status`),
  sessions:      () => api.get(`/api/employee/${iid}/sessions`),
  sessionDetail: (sid) => api.get(`/api/employee/${iid}/sessions/${sid}`),
  todos:         () => api.get(`/api/employee/${iid}/todos`),
  // 注：实际 endpoint 是 /tasks（POST/PATCH/DELETE），不是 /todos/{id}（后者只 GET 列表）
  //     之前误调 /todos/{id} 会 400，所以 update/delete 改走 /tasks
  createTodo:    (body) => api.post(`/api/employee/${iid}/tasks`, body),
  updateTodo:    (tid, body) => api.patch(`/api/employee/${iid}/tasks/${tid}`, body),
  deleteTodo:    (tid) => api.delete(`/api/employee/${iid}/tasks/${tid}`),
  calendar:      (weekStart) => api.get(`/api/employee/${iid}/calendar`, weekStart ? { week_start: weekStart } : {}),
  createSchedule:(body) => api.post(`/api/employee/${iid}/schedules`, body),
  updateSchedule:(sid, body) => api.patch(`/api/employee/${iid}/schedules/${sid}`, body),
  deleteSchedule:(sid) => api.delete(`/api/employee/${iid}/schedules/${sid}`),
  projects:      () => api.get(`/api/employee/${iid}/projects`),
  config:        () => api.get(`/api/employee/${iid}/config`),
  updateConfig:  (values) => api.patch(`/api/employee/${iid}/config`, { values }),
  prompts:       () => api.get(`/api/employee/${iid}/prompts`),
  updatePrompt:  (name, body) => api.patch(`/api/employee/${iid}/prompts/${name}`, body),
  contacts:      () => api.get(`/api/employee/${iid}/contacts`),
  createContact: (body) => api.post(`/api/employee/${iid}/contacts`, body),
  updateContact: (id, body) => api.patch(`/api/employee/${iid}/contacts/${id}`, body),
  deleteContact: (id) => api.delete(`/api/employee/${iid}/contacts/${id}`),
  blockContact:  (id, blocked, reason = '') => api.post(`/api/employee/${iid}/contacts/${id}/block`, { blocked, reason }),
  mergeContacts: (sourceId, targetId) => api.post(`/api/employee/${iid}/contacts/merge`, { source_id: sourceId, target_id: targetId }),
  memories:      (name) => api.get(`/api/employee/${iid}/memories/${name}`),
  associations:  () => api.get(`/api/employee/${iid}/associations`),
  // Feature 002 — 统一切片层(chunks 表)访问
  chunks:        (p = {}) => api.get(`/api/employee/${iid}/chunks`,
                  { q: p.q, source: p.source, limit: p.limit, offset: p.offset }),
  chunkDetail:   (id) => api.get(`/api/employee/${iid}/chunks/${id}`),
  wakeSnapshot:  (limit, offset = 0) => api.get(`/api/employee/${iid}/wakes`, { limit: limit || 30, offset }),
  wakeDetail:    (wakeId) => api.get(`/api/employee/${iid}/wakes/${wakeId}`),
  wakeCallInput: (wakeId, callSeq) => api.get(`/api/employee/${iid}/wakes/${wakeId}/input/${callSeq}`),
  budget:        () => api.get(`/api/employee/${iid}/budget`),
  budgetSeries:  (hours = 24, bucket = 'hour') => api.get(`/api/employee/${iid}/budget/series`, { hours, bucket }),
  vitalsSeries:  (hours = 24) => api.get(`/api/employee/${iid}/vitals/series`, { hours }),
  socialStatus:  () => api.get(`/api/employee/${iid}/social/status`),
  socialRevoke:  () => api.post(`/api/employee/${iid}/social/revoke`),
  events:        () => api.get(`/api/employee/${iid}/events`),
})
