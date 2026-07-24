<script setup lang="ts">
import { computed } from "vue";
import { storeToRefs } from "pinia";
import { usePluginStore } from "../stores/plugins";

const store = usePluginStore();
const { siteConfig } = storeToRefs(store);
const currentYear = new Date().getFullYear();
const siteName = computed(() => siteConfig.value.name || "Astrhub Plugins Market");
const communityRepoUrl = computed(() => store.communityRepoUrl);
</script>

<template>
  <footer class="app-footer">
    <div class="footer-content">
      <section class="footer-brand" aria-label="市场说明">
        <strong>&gt; 把你的 astrbot_plugin_idea 挂到这面墙上</strong>
        <p>{{ siteName }} · Community Plugin Market</p>
        <p>© {{ currentYear }} · API /v1</p>
      </section>

      <nav class="footer-group" aria-label="相关链接">
        <h2>相关链接</h2>
        <a href="https://docs.astrbot.app/" target="_blank" rel="noopener noreferrer"
          >AstrBot 文档 ↗</a
        >
        <a href="https://github.com/AstrBotDevs/AstrBot" target="_blank" rel="noopener noreferrer">
          AstrBot 本体 ↗
        </a>
      </nav>

      <nav class="footer-group" aria-label="开发相关">
        <h2>开发相关</h2>
        <router-link to="/docs/rest">REST 接口文档</router-link>
        <a :href="communityRepoUrl" target="_blank" rel="noopener noreferrer">市场仓库 ↗</a>
      </nav>
    </div>
  </footer>
</template>

<style scoped>
.app-footer {
  width: min(1824px, calc(100% - 96px));
  margin: 56px auto 0;
  padding: 42px 28px 34px;
  color: var(--text-secondary);
  background: transparent;
  border-top: 1px solid var(--border-base);
  box-sizing: border-box;
}

.footer-content {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(180px, 1fr) minmax(180px, 1fr);
  gap: 48px;
}

.footer-brand strong {
  display: block;
  color: var(--text-primary);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 15px;
  overflow-wrap: anywhere;
}

.footer-brand p {
  margin: 14px 0 0;
  color: var(--text-tertiary);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 10px;
  text-transform: uppercase;
}

.footer-brand p + p {
  margin-top: 5px;
}

.footer-group {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
}

.footer-group h2 {
  margin: 0 0 8px;
  color: var(--text-tertiary);
  font-size: 11px;
  font-weight: 650;
}

.footer-group a {
  color: var(--text-secondary);
  font-size: 12px;
  text-decoration: none;
}

.footer-group a:hover,
.footer-group a:focus-visible {
  color: var(--primary-color);
  outline: 0;
}

@media (max-width: 820px) {
  .app-footer {
    width: min(100% - 32px, 1824px);
    margin-top: 36px;
    padding: 30px 0;
  }

  .footer-content {
    grid-template-columns: 1fr 1fr;
    gap: 28px;
  }

  .footer-brand {
    grid-column: 1 / -1;
  }
}

@media (max-width: 520px) {
  .footer-content {
    grid-template-columns: 1fr;
  }

  .footer-brand {
    grid-column: auto;
  }
}
</style>
