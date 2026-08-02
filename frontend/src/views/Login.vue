<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { User, Lock } from '@element-plus/icons-vue'
import { useAuth } from '@/composables/useAuth'
import { register } from '@/api/auth'
import { ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY } from '@/api/interceptor'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const { login } = useAuth()

const loading = ref(false)
const isRegister = ref(false)
const form = reactive({
  username: '',
  password: '',
  confirmPassword: '',
})

function toggleMode(): void {
  isRegister.value = !isRegister.value
  form.username = ''
  form.password = ''
  form.confirmPassword = ''
}

async function handleLogin(): Promise<void> {
  if (!form.username || !form.password) {
    ElMessage.error(t('auth.invalidCredentials'))
    return
  }

  loading.value = true
  try {
    await login(form.username, form.password)
    const redirect = (route.query.redirect as string) || '/'
    router.push(redirect)
  } catch {
    ElMessage.error(t('auth.loginFailed'))
  } finally {
    loading.value = false
  }
}

async function handleRegister(): Promise<void> {
  if (!form.username || !form.password) {
    ElMessage.error(t('auth.invalidCredentials'))
    return
  }
  if (form.password.length < 8) {
    ElMessage.error(t('auth.passwordTooShort'))
    return
  }
  if (form.password !== form.confirmPassword) {
    ElMessage.error(t('auth.passwordMismatch'))
    return
  }

  loading.value = true
  try {
    const tokenResp = await register(form.username, form.password)
    localStorage.setItem(ACCESS_TOKEN_KEY, tokenResp.access_token)
    localStorage.setItem(REFRESH_TOKEN_KEY, tokenResp.refresh_token)
    const { init } = useAuth()
    await init()
    ElMessage.success(t('auth.registerSuccess'))
    const redirect = (route.query.redirect as string) || '/'
    router.push(redirect)
  } catch {
    ElMessage.error(t('auth.registerFailed'))
  } finally {
    loading.value = false
  }
}

function handleSubmit(): void {
  if (isRegister.value) {
    handleRegister()
  } else {
    handleLogin()
  }
}
</script>

<template>
  <div class="login-container">
    <div class="login-card">
      <div class="login-header">
        <h1 class="login-title">{{ isRegister ? t('auth.register') : t('auth.loginTitle') }}</h1>
        <p class="login-subtitle">ATE Studio</p>
      </div>

      <el-form class="login-form" @submit.prevent="handleSubmit">
        <el-form-item>
          <el-input
            v-model="form.username"
            :placeholder="t('auth.username')"
            size="large"
            :prefix-icon="User"
          />
        </el-form-item>

        <el-form-item>
          <el-input
            v-model="form.password"
            type="password"
            :placeholder="t('auth.password')"
            size="large"
            :prefix-icon="Lock"
            show-password
          />
        </el-form-item>

        <el-form-item v-if="isRegister">
          <el-input
            v-model="form.confirmPassword"
            type="password"
            :placeholder="t('auth.confirmPassword')"
            size="large"
            :prefix-icon="Lock"
            show-password
            @keyup.enter="handleSubmit"
          />
        </el-form-item>

        <el-button
          type="primary"
          size="large"
          class="login-button"
          :loading="loading"
          @click="handleSubmit"
        >
          {{ isRegister ? t('auth.register') : t('auth.loginButton') }}
        </el-button>
      </el-form>

      <div class="toggle-mode">
        <span v-if="!isRegister">{{ t('auth.noAccount') }}</span>
        <span v-else>{{ t('auth.haveAccount') }}</span>
        <el-button text type="primary" @click="toggleMode">
          {{ isRegister ? t('auth.backToLogin') : t('auth.signUp') }}
        </el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-container {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #409eff 0%, #337ecc 100%);
}

.login-card {
  width: 400px;
  max-width: 90vw;
  background: var(--color-bg-elevated);
  border-radius: var(--radius-2xl);
  padding: 40px 32px;
  box-shadow: var(--shadow-lg);
}

.login-header {
  text-align: center;
  margin-bottom: 32px;
}

.login-title {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--color-text-primary);
  margin: 0 0 8px 0;
}

.login-subtitle {
  font-size: 0.875rem;
  color: var(--color-text-secondary);
  margin: 0;
}

.login-form {
  margin-top: 8px;
}

.login-button {
  width: 100%;
  margin-top: 8px;
}

.toggle-mode {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  margin-top: 16px;
  font-size: 0.875rem;
  color: var(--color-text-secondary);
}
</style>
