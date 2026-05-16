<template>
  <div class="float-buttons">
    <!-- 返回顶部按钮 -->
    <transition name="back-to-top-fade">
      <div 
        v-show="show" 
        class="float-button back-to-top"
        @click="scrollToTop"
        role="button"
        aria-label="返回顶部"
      >
        <div class="float-button__inner back-to-top__inner">
          <n-icon size="22" class="float-button__icon">
            <chevron-up />
          </n-icon>
          <div class="float-button__ripple"></div>
        </div>
      </div>
    </transition>
    <submit-plugin-button />
    <help-button />
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { NIcon } from 'naive-ui'
import { ChevronUp } from '@vicons/ionicons5'
import HelpButton from './HelpButton.vue'
import SubmitPluginButton from './SubmitPluginButton.vue'

const show = ref(false)
const scrollThreshold = 300

const checkScroll = () => {
  show.value = window.pageYOffset > scrollThreshold
}

const scrollToTop = () => {
  const currentPosition = window.pageYOffset
  const duration = 500 
  const startTime = performance.now()

  function animation(currentTime) {
    const timeElapsed = currentTime - startTime
    const progress = Math.min(timeElapsed / duration, 1)
    const easing = progress => progress < 0.5
      ? 2 * progress * progress
      : 1 - Math.pow(-2 * progress + 2, 2) / 2

    window.scrollTo(0, currentPosition * (1 - easing(progress)))

    if (progress < 1) {
      requestAnimationFrame(animation)
    }
  }

  requestAnimationFrame(animation)
}

onMounted(() => {
  window.addEventListener('scroll', checkScroll, { passive: true })
  checkScroll() 
})

onUnmounted(() => {
  window.removeEventListener('scroll', checkScroll)
})
</script>

<style scoped>
.float-buttons {
  position: fixed;
  right: 24px;
  bottom: 24px;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 16px;
  z-index: 1000;
}

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
  box-shadow: var(--shadow-md);
  transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
  overflow: hidden;
}

.back-to-top__inner {
  background: var(--primary-color);
}

.float-button:hover .float-button__inner {
  box-shadow: var(--shadow-lg);
}

.back-to-top:hover .back-to-top__inner {
  background: var(--primary-hover);
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

.back-to-top:active .back-to-top__ripple {
  animation: ripple 0.6s ease-out;
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

@keyframes float {
  0%, 100% {
    transform: translateY(0px);
  }
  50% {
    transform: translateY(-2px);
  }
}

.back-to-top-fade-enter-active {
  transition: all 0.5s cubic-bezier(0.68, -0.55, 0.265, 1.55);
}

.back-to-top-fade-leave-active {
  transition: all 0.3s cubic-bezier(0.4, 0, 1, 1);
}

.back-to-top-fade-enter-from {
  opacity: 0;
  transform: translateY(20px) scale(0.8);
}

.back-to-top-fade-leave-to {
  opacity: 0;
  transform: translateY(10px) scale(0.9);
}

.help-modal {
  max-width: 90vw;
}

.help-modal__title {
  margin: 0;
  color: var(--text-primary);
}

.help-section {
  padding: 8px 0;
}

.help-section :deep(h3) {
  margin: 0 0 8px 0;
  color: var(--text-primary);
}

.help-section :deep(.n-text) {
  color: var(--text-secondary);
  line-height: 1.6;
}

/* 响应式 */
@media (max-width: 768px) {
  .float-buttons {
    right: 20px;
    bottom: 20px;
    gap: 12px;
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
  .float-buttons {
    right: 16px;
    bottom: 16px;
    gap: 10px;
  }

  .float-button {
    width: 44px;
    height: 44px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .back-to-top__inner,
  .back-to-top__icon,
  .progress-ring__circle,
  .back-to-top-fade-enter-active,
  .back-to-top-fade-leave-active {
    transition-duration: 0.1s;
  }
  
  .back-to-top__ripple {
    display: none;
  }
  
  @keyframes float {
    0%, 100% {
      transform: none;
    }
  }
}
</style>