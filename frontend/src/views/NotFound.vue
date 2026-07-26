<script setup lang="ts">
import { useRouter } from 'vue-router'
import { ElButton } from 'element-plus'

const router = useRouter()

const goHome = () => {
  router.push('/')
}
</script>

<template>
  <div class="not-found">
    <div class="content">
      <h1>404</h1>
      <p>Page not found</p>
      <ElButton type="primary" @click="goHome">
        Go Home
      </ElButton>
    </div>
  </div>
</template>

<style scoped>
.not-found {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  text-align: center;
}

.content h1 {
  font-size: 4rem;
  font-weight: 700;
  color: var(--color-primary);
  margin-bottom: var(--spacing-sm);
}

.content p {
  font-size: 1.25rem;
  color: var(--color-text-secondary);
  margin-bottom: var(--spacing-lg);
}
</style>