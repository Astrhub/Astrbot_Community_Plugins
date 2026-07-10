import type { RouteRecordRaw } from "vue-router";
import { createRouter, createWebHistory } from "vue-router";
import Home from "../views/Home.vue";
import { usePluginStore } from "../stores/plugins";

const SubmitPlugin = () => import("../views/SubmitPlugin.vue");
const Setup = () => import("../views/Setup.vue");
const Settings = () => import("../views/Settings.vue");
const PersonalSettings = () => import("../views/PersonalSettings.vue");
const Notifications = () => import("../views/Notifications.vue");
const AdminPlugins = () => import("../views/AdminPlugins.vue");
const AdminLogin = () => import("../views/AdminLogin.vue");
const RestDocs = () => import("../views/RestDocs.vue");
const PluginWorkbench = () => import("../views/PluginWorkbench.vue");
const NotFound = () => import("../views/NotFound.vue");

const routes: RouteRecordRaw[] = [
  {
    path: "/",
    name: "Home",
    component: Home,
  },
  {
    path: "/setup",
    name: "Setup",
    component: Setup,
  },
  {
    path: "/submit",
    name: "SubmitPlugin",
    component: SubmitPlugin,
  },
  {
    path: "/settings",
    redirect: "/admin/settings",
  },
  {
    path: "/settings/personal",
    name: "PersonalSettings",
    component: PersonalSettings,
  },
  {
    path: "/notifications",
    name: "Notifications",
    component: Notifications,
  },
  {
    path: "/plugin-workbench",
    name: "PluginWorkbench",
    component: PluginWorkbench,
    meta: { requiresAuth: true },
  },
  {
    path: "/admin",
    name: "AdminLogin",
    component: AdminLogin,
  },
  {
    path: "/admin/settings",
    name: "AdminSettings",
    component: Settings,
  },
  {
    path: "/admin/plugins",
    name: "AdminPlugins",
    component: AdminPlugins,
  },
  {
    path: "/docs/rest",
    name: "RestDocs",
    component: RestDocs,
  },
  {
    path: "/:pathMatch(.*)*",
    name: "NotFound",
    component: NotFound,
  },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.beforeEach(async (to) => {
  if (!to.meta.requiresAuth) return true;
  const store = usePluginStore();
  await store.loadCurrentUser();
  if (store.currentUser) return true;
  return { name: "Home", query: { login: "required", redirect: to.fullPath } };
});

export default router;
