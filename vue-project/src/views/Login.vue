<template>
  <div class="login-bg">
    <div class="overlay"></div>
    <div class="login-wrapper">
      <div class="login-card">
        <img :src="logoUrl" alt="Logo" class="login-logo" />
        <h2 class="title">欢迎登录</h2>

        <form @submit.prevent="login">
          <div class="form-item">
            <label for="username">用户名</label>
            <input
              id="username"
              v-model.trim="username"
              type="text"
              placeholder="请输入用户名"
              @input="validateUsernameInput"
              required
            />
          </div>

          <div class="form-item">
            <label for="password">密码</label>
            <div class="password-field">
              <input
                id="password"
                v-model.trim="password"
                :type="showPassword ? 'text' : 'password'"
                placeholder="请输入密码"
                @input="validatePasswordInput"
                required
              />
              <button class="password-toggle" type="button" @click="togglePassword">
                <svg v-if="showPassword" viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6z"></path>
                  <circle cx="12" cy="12" r="3"></circle>
                </svg>
                <svg v-else viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M3 3l18 18"></path>
                  <path d="M2 12s3.5-6 10-6c2.1 0 3.9.6 5.4 1.5"></path>
                  <path d="M22 12s-3.5 6-10 6c-2.1 0-3.9-.6-5.4-1.5"></path>
                  <path d="M9.9 9.9A3 3 0 0 0 14.1 14.1"></path>
                </svg>
              </button>
            </div>
          </div>

          <div class="form-item captcha-row">
            <div class="flex-1">
              <input
                id="captcha"
                v-model.trim="captcha"
                type="text"
                maxlength="4"
                placeholder="请输入验证码"
                required
              />
            </div>
            <div class="captcha-img" @click="loadCaptcha" title="点击更换验证码">
              <img v-if="captchaImg" :src="captchaImg" alt="验证码" />
              <span v-else>加载中…</span>
            </div>
          </div>

          <button class="btn-primary" type="submit" :disabled="loading">
            <span v-if="loading" class="spinner" />
            登录
          </button>
        </form>

        <p class="footer-text">
          没有账号？
          <span class="link" @click="$router.push('/register')">去注册</span>
        </p>
        <p class="footer-text">
          忘记密码？
          <span class="link" @click="$router.push('/forgot-password')">去修改</span>
        </p>

        <p v-if="errorMsg" class="error">{{ errorMsg }}</p>
      </div>
    </div>
  </div>
</template>

<script src="../js/login-page.js"></script>
