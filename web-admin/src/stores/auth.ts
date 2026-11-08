import { defineStore } from 'pinia'
import { api, ensureCsrf } from '../api'

export interface AdminInfo {
  id: number
  username: string
  role: string
}

export const useAuthStore = defineStore('auth', {
  state: () => ({
    admin: null as AdminInfo | null,
    loading: false,
  }),
  actions: {
    async login(username: string, password: string) {
      await ensureCsrf()
      const { data } = await api.post('/auth/login', { username, password })
      this.admin = { id: 0, username: data.username, role: data.role }
      await this.fetchMe()
    },
    async fetchMe(): Promise<boolean> {
      if (this.admin) return true
      try {
        const { data } = await api.get('/auth/me')
        this.admin = data
        return true
      } catch {
        this.admin = null
        return false
      }
    },
    async logout() {
      try {
        await api.post('/auth/logout')
      } finally {
        this.admin = null
        window.location.hash = '#/login'
      }
    },
  },
})
