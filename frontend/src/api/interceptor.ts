import axios from 'axios'

const ACCESS_TOKEN_KEY = 'ate-access-token'
const REFRESH_TOKEN_KEY = 'ate-refresh-token'

const http = axios.create({
  baseURL: '/api/v1',
  timeout: 10000,
  headers: {
    'Content-Type': 'application/json',
  },
})

let isRefreshing = false
let refreshSubscribers: Array<(token: string | null) => void> = []

function onTokenRefreshed(token: string | null): void {
  refreshSubscribers.forEach((cb) => cb(token))
  refreshSubscribers = []
}

function addRefreshSubscriber(cb: (token: string | null) => void): void {
  refreshSubscribers.push(cb)
}

function clearAuthStorage(): void {
  localStorage.removeItem(ACCESS_TOKEN_KEY)
  localStorage.removeItem(REFRESH_TOKEN_KEY)
}

function redirectToLogin(): void {
  clearAuthStorage()
  if (window.location.pathname !== '/login') {
    const redirect = window.location.pathname + window.location.search
    window.location.href = `/login?redirect=${encodeURIComponent(redirect)}`
  }
}

http.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem(ACCESS_TOKEN_KEY)
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error),
)

http.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config

    if (error.response?.status === 401 && !originalRequest._retry) {
      const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY)

      if (!refreshToken) {
        redirectToLogin()
        return Promise.reject(error)
      }

      if (isRefreshing) {
        return new Promise((resolve, reject) => {
          addRefreshSubscriber((token) => {
            if (token) {
              originalRequest.headers.Authorization = `Bearer ${token}`
              resolve(http(originalRequest))
            } else {
              reject(error)
            }
          })
        })
      }

      originalRequest._retry = true
      isRefreshing = true

      try {
        const res = await axios.post('/api/v1/auth/refresh', {
          refresh_token: refreshToken,
        })
        const { access_token, refresh_token: newRefreshToken } = res.data
        localStorage.setItem(ACCESS_TOKEN_KEY, access_token)
        localStorage.setItem(REFRESH_TOKEN_KEY, newRefreshToken)
        onTokenRefreshed(access_token)
        originalRequest.headers.Authorization = `Bearer ${access_token}`
        return http(originalRequest)
      } catch {
        onTokenRefreshed(null)
        redirectToLogin()
        return Promise.reject(error)
      } finally {
        isRefreshing = false
      }
    }

    return Promise.reject(error)
  },
)

export { ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, clearAuthStorage, redirectToLogin }
export default http
