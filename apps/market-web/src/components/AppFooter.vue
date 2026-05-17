<template>
  <footer class="app-footer">
    <div class="footer-content">
      <div class="footer-links">
        <div class="links-group">
          <h4>相关链接</h4>
          <a href="https://docs.astrbot.app/" target="_blank" class="footer-link">
            <n-icon><star-outline /></n-icon>
            AstrBot 文档
          </a>
          <a href="https://github.com/AstrBotDevs/AstrBot" target="_blank" class="footer-link">
            <n-icon><logo-github /></n-icon>
            AstrBot 本体
          </a>
        </div>
        <div class="links-group">
          <h4>开发相关</h4>
          <a href="https://docs.astrbot.app/dev/star/plugin-new.html" target="_blank" class="footer-link">
            <n-icon><document-text /></n-icon>
            插件开发文档
          </a>
          <a :href="communityRepoUrl" target="_blank" class="footer-link">
            <n-icon><code-slash /></n-icon>
            市场仓库
          </a>
        </div>
      </div>
    </div>
    <div class="footer-bottom">
      <p class="copyright">
        © {{ currentYear }} {{ siteName }}
        <span class="made-with">
          Built for AstrBot <n-icon class="heart-icon"><heart /></n-icon>
        </span>
      </p>
    </div>
  </footer>
</template>

<script setup>
import { computed } from 'vue'
import { NIcon } from 'naive-ui'
import { storeToRefs } from 'pinia'
import { 
  LogoGithub, 
  DocumentText, 
  CodeSlash,
  Heart,
  StarOutline
} from '@vicons/ionicons5'
import { usePluginStore } from '../stores/plugins'

const store = usePluginStore()
const { siteConfig } = storeToRefs(store)
const currentYear = computed(() => new Date().getFullYear())
const siteName = computed(() => siteConfig.value.name)
const communityRepoUrl = computed(() => store.communityRepoUrl)
</script>

<style scoped>
.app-footer {
  background: linear-gradient(180deg, var(--bg-card) 0%, var(--bg-base) 100%);
  border-top: 1px solid var(--border-base);
  padding: 48px 0 24px;
  margin-top: 60px;
}

.footer-content {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 24px;
}

.footer-links {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 32px;
  max-width: 620px;
  margin: 0 auto;
}

.links-group h4 {
  color: var(--primary-color);
  margin: 0 0 16px;
  font-size: 1em;
  font-weight: 700;
}

.footer-link {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-secondary);
  text-decoration: none;
  margin-bottom: 12px;
  transition: all 0.3s ease;
  padding: 4px 8px;
  border-radius: 4px;
}

.footer-link:hover {
  color: #ffffff;
  background-color: var(--primary-color);
  transform: translateX(4px);
  box-shadow: 0 0 10px rgba(37, 99, 235, 0.22);
}

.footer-link .n-icon {
  font-size: 18px;
}

.footer-bottom {
  border-top: 1px solid var(--border-base);
  margin-top: 40px;
  padding-top: 24px;
  text-align: center;
}

.copyright {
  color: var(--text-tertiary);
  font-size: 0.9em;
  margin: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.made-with {
  display: flex;
  align-items: center;
  gap: 6px;
}

.heart-icon {
  color: var(--primary-color);
  font-size: 16px;
  animation: heartbeat 1.5s ease-in-out infinite alternate;
}

@keyframes heartbeat {
  from {
    transform: scale(1);
  }
  to {
    transform: scale(1.2);
  }
}

/* 平板端适配 */
@media (max-width: 1024px) {
  .footer-content {
    padding: 0 20px;
    gap: 36px;
  }
  
  .footer-links {
    gap: 28px;
  }
}

/* 移动端适配 */
@media (max-width: 768px) {
  .app-footer {
    padding: 18px 0 14px;
    margin-top: 24px;
  }

  .footer-content {
    grid-template-columns: 1fr;
    gap: 12px;
    padding: 0 16px;
  }

  .footer-links {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 8px 10px;
  }

  .links-group {
    display: contents;
  }

  .links-group h4 {
    display: none;
  }

  .footer-link {
    justify-content: center;
    margin-bottom: 0;
    padding: 6px 8px;
    font-size: 0.85em;
    line-height: 1.2;
  }

  .footer-link:hover {
    transform: none;
    color: var(--primary-color);
  }

  .footer-bottom {
    margin-top: 12px;
    padding-top: 12px;
  }

  .copyright {
    font-size: 0.78em;
    line-height: 1.4;
    flex-wrap: wrap;
    gap: 4px 8px;
  }

  .made-with {
    display: none;
  }
}

/* 小屏手机适配 */
@media (max-width: 480px) {
  .app-footer {
    padding: 14px 0 12px;
    margin-top: 18px;
  }

  .footer-content {
    padding: 0 10px;
    gap: 10px;
  }

  .footer-links {
    gap: 6px;
  }

  .footer-link {
    font-size: 0.82em;
    padding: 5px 7px;
  }

  .footer-link .n-icon {
    font-size: 16px;
  }

  .footer-bottom {
    margin-top: 10px;
    padding-top: 10px;
  }

  .copyright {
    font-size: 0.76em;
  }
}
</style>
