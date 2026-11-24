<template>
  <main class="not-found-page">
    <n-result status="404" description="这个页面不存在或已被移除。">
      <template #header>
        <h1 class="not-found-heading">页面不存在</h1>
      </template>
      <template #footer>
        <router-link v-slot="{ href, navigate }" to="/" custom>
          <n-button tag="a" :href="href" type="primary" @click="navigate">
            <template #icon>
              <n-icon><home-outline /></n-icon>
            </template>
            返回首页
          </n-button>
        </router-link>
      </template>
    </n-result>
  </main>
</template>

<script setup lang="ts">
import { NButton, NIcon, NResult } from "naive-ui";
import { HomeOutline } from "@vicons/ionicons5";
import { useSeo } from "../composables/useSeo";

useSeo({
  title: "页面不存在",
  description: "请求的页面不存在。",
  path: "/404",
  robots: "noindex,nofollow",
});
</script>

<style scoped>
.not-found-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background: var(--body-color);
}

.not-found-heading {
  margin: 0;
  font-size: 28px;
  line-height: 1.3;
  color: var(--text-primary);
}
</style>
