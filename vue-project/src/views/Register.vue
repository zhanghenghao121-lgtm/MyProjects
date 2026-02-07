<template>
  <div>
    <h2>注册</h2>
    <input v-model="username" placeholder="用户名" />
    <input v-model="password" type="password" placeholder="密码" />
    <button @click="register">注册</button>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import http from '../api/http'

const router = useRouter()
const username = ref('')
const password = ref('')

const register = async () => {
  await http.post('/register/', {
    username: username.value,
    password: password.value
  })

  router.push('/login')
}
</script>