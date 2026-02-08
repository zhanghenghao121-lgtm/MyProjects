import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'

// 导入本地 Bootstrap
import './assets/bootstrap/bootstrap.min.css'
import './assets/bootstrap/bootstrap.bundle.min.js'

// 自定义 CSS
import './assets/css/custom.css'

const app = createApp(App)

app.use(createPinia())
app.use(router)

app.mount('#app')
