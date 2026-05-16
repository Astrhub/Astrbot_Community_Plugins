// 评论模块预留。

<template>
  <div class="plugin-comment">
    <n-empty description="评论组件由市场 API 提供，当前仅保留接口位置。" />
  </div>
</template>

<script setup>
import { NEmpty } from 'naive-ui'
</script>

<style scoped>
.plugin-comment {
  margin: var(--modal-padding) calc(-1 * var(--modal-padding));
  padding: var(--modal-padding);
  border-top: 1px solid var(--n-border-color);
  background-color: var(--n-color);
}
</style>
