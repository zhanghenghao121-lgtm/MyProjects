<template>
  <div>
    <h2>登录</h2>
    <input v-model="username" placeholder="用户名" />
    <input v-model="password" type="password" placeholder="密码" />
    <button @click="login">登录</button>
    <p @click="$router.push('/register')">去注册</p>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import http from '../api/http'
import { useUserStore } from '../stores/user'

const router = useRouter()
const userStore = useUserStore()

const username = ref('')
const password = ref('')

const login = async () => {
  const res = await http.post('/login/', {
    username: username.value,
    password: password.value
  })

  userStore.setToken(res.data.token)
  router.push('/home')
}
</script>