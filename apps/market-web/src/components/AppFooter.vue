<template>
  <footer class="app-footer">
    <div class="footer-content">
      <div class="footer-section">
        <img src="/logo.webp" alt="AstrBot Community Plugins" class="footer-logo" width="40" height="40" decoding="async" fetchpriority="low">
        <p class="footer-description">
          AstrBot Community Plugins 是 AstrBot 的社区插件市场与第三方源入口。
        </p>
      </div>
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
          <a href="https://github.com/AstrBotDevs/Astrbot_Community_Plugins" target="_blank" class="footer-link">
            <n-icon><document-outline /></n-icon>
            社区源仓库
          </a>
        </div>
        <div class="links-group">
          <h4>开发相关</h4>
          <a href="https://docs.astrbot.app/dev/star/plugin.html" target="_blank" class="footer-link">
            <n-icon><document-text /></n-icon>
            插件开发文档
          </a>
          <a href="/submit" class="footer-link">
            <n-icon><git-branch /></n-icon>
            站内提交
          </a>
          <a href="https://github.com/AstrBotDevs/Astrbot_Community_Plugins" target="_blank" class="footer-link">
            <n-icon><code-slash /></n-icon>
            市场仓库
          </a>
        </div>
      </div>
    </div>
    <div class="footer-bottom">
      <p class="copyright">
        © {{ currentYear }} AstrBot Community Plugins
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
import { 
  LogoGithub, 
  GitBranch, 
  DocumentText, 
  CodeSlash,
  Heart,
  StarOutline,
  DocumentOutline
} from '@vicons/ionicons5'

const currentYear = computed(() => new Date().getFullYear())
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
  display: grid;
  grid-template-columns: 1.5fr 1fr;
  gap: 48px;
}

.footer-section {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.footer-logo {
  width: 40px;
  height: 40px;
  object-fit: contain;
}

.footer-description {
  color: var(--text-secondary);
  line-height: 1.6;
  font-size: 0.95em;
  margin: 0;
}

.footer-links {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 32px;
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
    padding: 32px 0 20px;
    margin-top: 40px;
  }

  .footer-content {
    grid-template-columns: 1fr;
    gap: 32px;
    padding: 0 16px;
  }

  .footer-section {
    text-align: left;
    gap: 12px;
  }

  .footer-description {
    font-size: 0.9em;
    line-height: 1.5;
  }

  .footer-links {
    grid-template-columns: 1fr;
    gap: 24px;
  }

  .links-group {
    text-align: left;
  }

  .links-group h4 {
    margin-bottom: 12px;
    font-size: 0.95em;
  }

  .footer-link {
    justify-content: flex-start;
    margin-bottom: 10px;
    padding: 8px 0;
  }

  .footer-link:hover {
    transform: none;
    color: var(--primary-color);
  }

  .footer-bottom {
    margin-top: 28px;
    padding-top: 20px;
  }

  .copyright {
    font-size: 0.85em;
    line-height: 1.4;
    flex-direction: column;
    gap: 8px;
  }
}

/* 小屏手机适配 */
@media (max-width: 480px) {
  .app-footer {
    padding: 24px 0 16px;
    margin-top: 32px;
  }

  .footer-content {
    padding: 0 12px;
    gap: 24px;
  }

  .footer-logo {
    width: 36px;
    height: 36px;
  }

  .footer-description {
    font-size: 0.85em;
  }

  .footer-links {
    gap: 20px;
  }

  .links-group h4 {
    font-size: 0.9em;
    margin-bottom: 10px;
  }

  .footer-link {
    font-size: 0.9em;
    margin-bottom: 8px;
    padding: 6px 0;
  }

  .footer-link .n-icon {
    font-size: 16px;
  }

  .footer-bottom {
    margin-top: 20px;
    padding-top: 16px;
  }

  .copyright {
    font-size: 0.8em;
  }

  .heart-icon {
    font-size: 14px;
  }
}
</style>
