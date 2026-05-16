<template>
  <div class="submit-button-container" @mouseenter="showTipText = true" @mouseleave="showTipText = false">
    <div class="text-container">
      <div class="tip-text" :class="{ 'tip-text--show': showTipText }">
        来提交你的插件吧！
      </div>
    </div>
    <div class="float-button submit-plugin">
      <div 
        class="float-button__inner submit-plugin__inner"
        @click="navigateToSubmit"
        role="button"
        aria-label="提交插件"
      >
        <n-icon size="22" class="float-button__icon">
          <add-circle />
        </n-icon>
        <div class="float-button__ripple"></div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { NIcon } from 'naive-ui'
import { AddCircle } from '@vicons/ionicons5'
import { useRouter } from 'vue-router'

const router = useRouter()
const showTipText = ref(false)

const navigateToSubmit = () => {
  router.push('/submit')
}
</script>

<style scoped>
.float-button {
  width: 56px;
  height: 56px;
  cursor: pointer;
  user-select: none;
}

.float-button__inner {
  position: relative;
  width: 100%;
  height: 100%;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
  overflow: hidden;
}

.float-button:hover .float-button__inner {
  box-shadow: var(--shadow-lg);
}

.float-button:active .float-button__inner {
  transform: scale(0.95);
  transition-duration: 0.1s;
}

.float-button__icon {
  position: relative;
  z-index: 2;
}

.float-button__ripple {
  position: absolute;
  top: 50%;
  left: 50%;
  width: 0;
  height: 0;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.3);
  transform: translate(-50%, -50%);
  pointer-events: none;
}

.submit-button-container {
  display: flex;
  align-items: center;
}

.text-container {
  height: 56px;
  overflow: hidden;
  display: flex;
  align-items: center;
  padding: 0 8px;
  margin: 0 -8px;
}

.tip-text {
  background: var(--bg-card);
  padding: 0 12px;
  border: 2px solid transparent;
  border-radius: 28px;
  font-size: 14px;
  height: 40px;
  line-height: 40px;
  color: var(--text-color);
  white-space: nowrap;
  margin-right: -56px;
  transform: translateX(100%);
  opacity: 0;
  transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
  box-shadow: var(--shadow-sm);
  background-clip: padding-box;
  position: relative;
}

.tip-text::before {
  content: '';
  position: absolute;
  top: -2px;
  right: -2px;
  bottom: -2px;
  left: -2px;
  background: var(--header-gradient);
  border-radius: inherit;
  z-index: -1;
  transition: opacity 0.4s ease;
  opacity: 0.6;
}

.tip-text--show {
  transform: translateX(0);
  opacity: 1;
  margin-right: 16px;
}

.submit-plugin__inner {
  background: var(--primary-color);
}

.submit-plugin:hover .submit-plugin__inner {
  background: var(--primary-hover);
}

.submit-plugin:active .submit-plugin__ripple {
  animation: ripple 0.6s ease-out;
}

@media (max-width: 768px) {
  .text-container {
    height: 48px;
  }
  .tip-text {
    height: 36px;
    line-height: 36px;
    font-size: 13px;
  }
  .float-button {
    width: 48px;
    height: 48px;
  }
  
  .float-button__icon {
    font-size: 18px;
  }
}

@media (max-width: 480px) {
  .text-container {
    height: 44px;
  }
  .tip-text {
    height: 32px;
    line-height: 32px;
    font-size: 12px;
  }
  .float-button {
    width: 44px;
    height: 44px;
  }
}

@keyframes ripple {
  0% {
    width: 0;
    height: 0;
    opacity: 1;
  }
  100% {
    width: 60px;
    height: 60px;
    opacity: 0;
  }
}

.modal-content {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.scroll-container {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding-right: 4px;
}

.submit-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--border-color);
  flex-shrink: 0;
}

.json-section {
  margin-top: 24px;
}

.json-tip {
  margin-bottom: 12px;
}

.json-textarea {
  margin-bottom: 12px;
}

.json-actions {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 16px;
}

:deep(.n-input) {
  width: 100%;
}

:deep(.n-form-item) {
  margin-bottom: 24px;
}

:deep(.n-card-header) {
  padding-bottom: 16px;
  flex-shrink: 0;
}

:deep(.n-card__content) {
  flex: 1;
  padding-right: -8px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

@media (max-width: 768px) {
  .submit-modal {
    max-width: 90vw;
  }
}
</style>