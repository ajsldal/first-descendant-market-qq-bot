import axios from 'axios'

export const api = axios.create({
  baseURL: '/api',
  withCredentials: true,
  timeout: 15000,
})

api.interceptors.request.use((config) => {
  const method = (config.method || 'get').toLowerCase()
  if (!['get', 'head', 'options'].includes(method)) {
    const match = document.cookie.match(/(?:^|;\s*)tfd_admin_csrf=([^;]+)/)
    if (match) {
      ;(config.headers as any)['X-CSRF-Token'] = decodeURIComponent(match[1])
    }
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401 && window.location.hash !== '#/login') {
      window.location.hash = '#/login'
    }
    return Promise.reject(error)
  },
)

export async function ensureCsrf(): Promise<void> {
  await api.get('/auth/csrf')
}

export function errorText(error: any): string {
  return error?.response?.data?.detail || error?.message || '请求失败'
}
