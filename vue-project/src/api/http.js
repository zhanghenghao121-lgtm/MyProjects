import axios from 'axios'

const http = axios.create({
    // baseURL: 'http://1.12.230.174:8000/api',
    baseURL: import.meta.env.VITE_API_BASE_URL,
    timeout: 5000,
    withCredentials: true,
    xsrfCookieName: 'csrftoken',
    xsrfHeaderName: 'X-CSRFToken'
})

http.interceptors.request.use(config => {
    const token = localStorage.getItem('token')
    if (token) {
        config.headers.Authorization = `Bearer ${token}`
    }
    return config
})

export default http
export const ensureCsrf = async () => {
  const res = await http.get('/csrf/')
  const token = res?.data?.csrfToken
  if (token) {
    http.defaults.headers.common['X-CSRFToken'] = token
  }
  return res
}
