<template>
  <n-dropdown
    :options="themeMenuOptions"
    trigger="click"
    @select="setThemeMode"
  >
    <n-button
      quaternary
      :circle="circle"
      :class="$attrs.class"
      :aria-label="`主题：${themeModeLabel}`"
    >
      <template #icon>
        <n-icon><component :is="themeModeIcon" /></n-icon>
      </template>
      <template v-if="!circle">{{ themeModeLabel }}</template>
    </n-button>
  </n-dropdown>
</template>

<script setup>
import { computed, h } from 'vue'
import { storeToRefs } from 'pinia'
import { NButton, NDropdown, NIcon } from 'naive-ui'
import { DesktopOutline, Moon, Sunny } from '@vicons/ionicons5'
import { usePluginStore } from '../stores/plugins'

defineOptions({ inheritAttrs: false })

defineProps({
  circle: {
    type: Boolean,
    default: false
  }
})

const store = usePluginStore()
const { themeMode } = storeToRefs(store)
const { setThemeMode } = store

const themeModes = {
  system: {
    label: '自动',
    icon: DesktopOutline
  },
  light: {
    label: '浅色',
    icon: Sunny
  },
  dark: {
    label: '深色',
    icon: Moon
  }
}

const themeModeLabel = computed(() => themeModes[themeMode.value]?.label || themeModes.system.label)
const themeModeIcon = computed(() => themeModes[themeMode.value]?.icon || themeModes.system.icon)
const themeMenuOptions = computed(() =>
  Object.entries(themeModes).map(([key, item]) => ({
    key,
    label: item.label,
    icon: renderIcon(item.icon)
  }))
)

function renderIcon(icon) {
  return () => h(NIcon, null, { default: () => h(icon) })
}
</script>
