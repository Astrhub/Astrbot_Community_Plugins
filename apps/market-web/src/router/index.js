import { createRouter, createWebHistory } from 'vue-router'
import Home from '../views/Home.vue'

const SubmitPlugin = () => import('../views/SubmitPlugin.vue')
const Setup = () => import('../views/Setup.vue')
const Settings = () => import('../views/Settings.vue')
const PersonalSettings = () => import('../views/PersonalSettings.vue')
const AdminPlugins = () => import('../views/AdminPlugins.vue')
const routes = [
  {
    path: '/',
    name: 'Home',
    component: Home
  },
  {
    path: '/setup',
    name: 'Setup',
    component: Setup
  },
  {
    path: '/submit',
    name: 'SubmitPlugin',
    component: SubmitPlugin
  },
  {
    path: '/settings',
    name: 'Settings',
    component: Settings
  },
  {
    path: '/settings/personal',
    name: 'PersonalSettings',
    component: PersonalSettings
  },
  {
    path: '/admin/plugins',
    name: 'AdminPlugins',
    component: AdminPlugins
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

export default router
