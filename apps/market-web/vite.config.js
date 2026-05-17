import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { execSync } from 'child_process'
import { resolve } from 'path'
import sitemap from 'vite-plugin-sitemap'
import externalSitemaps from './sitemaps.config.js'

const baseUrl = process.env.VITE_BASE_URL
const communityRepoUrl = process.env.VITE_COMMUNITY_REPO_URL || readGitRemoteUrl()
const plugins = [vue()]

if (baseUrl) {
  plugins.push(
    sitemap({
      hostname: baseUrl,
      dynamicRoutes: ['/submit'],
      externalSitemaps,
      generateRobotsTxt: true,
      readable: true
    })
  )
}

export default defineConfig({
  plugins,
  define: {
    'import.meta.env.VITE_COMMUNITY_REPO_URL': JSON.stringify(communityRepoUrl)
  },
  base: '/',
  assetsInclude: ['**/*.md'],
  resolve: {
    alias: {
      '@': resolve(__dirname, './src')
    }
  },
  build: {
    chunkSizeWarningLimit: 1000
  },
  server: {
    host: '0.0.0.0',
    port: 3000
  }
})

function readGitRemoteUrl() {
  try {
    return normalizeGitRemoteUrl(
      execSync('git config --get remote.origin.url', {
        cwd: resolve(__dirname, '../..'),
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore']
      })
    )
  } catch {
    return 'https://github.com/Astrhub/Astrbot_Community_Plugins'
  }
}

function normalizeGitRemoteUrl(value) {
  const remoteUrl = String(value || '').trim().replace(/\.git$/, '')
  const sshMatch = remoteUrl.match(/^git@github\.com:(.+)$/)
  if (sshMatch) return `https://github.com/${sshMatch[1]}`
  return remoteUrl || 'https://github.com/Astrhub/Astrbot_Community_Plugins'
}
