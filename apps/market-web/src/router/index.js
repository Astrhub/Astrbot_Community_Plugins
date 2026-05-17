import { createRouter, createWebHistory } from 'vue-router'
import Home from '../views/Home.vue'

const SubmitPlugin = () => import('../views/SubmitPlugin.vue')
const Setup = () => import('../views/Setup.vue')
const Settings = () => import('../views/Settings.vue')
const PersonalSettings = () => import('../views/PersonalSettings.vue')
const Notifications = () => import('../views/Notifications.vue')
const AdminPlugins = () => import('../views/AdminPlugins.vue')
const AdminLogin = () => import('../views/AdminLogin.vue')
const NotFound = () => import('../views/NotFound.vue')
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
    redirect: '/admin/settings'
  },
  {
    path: '/settings/personal',
    name: 'PersonalSettings',
    component: PersonalSettings
  },
  {
    path: '/notifications',
    name: 'Notifications',
    component: Notifications
  },
  {
    path: '/admin',
    name: 'AdminLogin',
    component: AdminLogin
  },
  {
    path: '/admin/settings',
    name: 'AdminSettings',
    component: Settings
  },
  {
    path: '/admin/plugins',
    name: 'AdminPlugins',
    component: AdminPlugins
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: NotFound
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

export default router
