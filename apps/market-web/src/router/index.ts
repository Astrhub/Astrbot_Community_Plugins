import type { RouteRecordRaw } from "vue-router";
import { createRouter, createWebHistory } from "vue-router";
import Home from "../views/Home.vue";

const SubmitPlugin = () => import("../views/SubmitPlugin.vue");
const Setup = () => import("../views/Setup.vue");
const Settings = () => import("../views/Settings.vue");
const PersonalSettings = () => import("../views/PersonalSettings.vue");
const Notifications = () => import("../views/Notifications.vue");
const AdminPlugins = () => import("../views/AdminPlugins.vue");
const AdminLogin = () => import("../views/AdminLogin.vue");
const RestDocs = () => import("../views/RestDocs.vue");
const PluginDetailsPage = () => import("../views/PluginDetailsPage.vue");
const NotFound = () => import("../views/NotFound.vue");

const routes: RouteRecordRaw[] = [
  {
    path: "/plugin/:name",
    name: "PluginDetails",
    component: PluginDetailsPage,
    props: true,
  },
  {
    path: "/",
    name: "Home",
    component: Home,
  },
  {
    path: "/setup",
    name: "Setup",
    component: Setup,
    meta: { noindex: true },
  },
  {
    path: "/submit",
    name: "SubmitPlugin",
    component: SubmitPlugin,
  },
  {
    path: "/settings",
    redirect: "/admin/settings",
    meta: { noindex: true },
  },
  {
    path: "/settings/personal",
    name: "PersonalSettings",
    component: PersonalSettings,
    meta: { noindex: true },
  },
  {
    path: "/notifications",
    name: "Notifications",
    component: Notifications,
    meta: { noindex: true },
  },
  {
    path: "/admin",
    name: "AdminLogin",
    component: AdminLogin,
    meta: { noindex: true },
  },
  {
    path: "/admin/settings",
    name: "AdminSettings",
    component: Settings,
    meta: { noindex: true },
  },
  {
    path: "/admin/plugins",
    name: "AdminPlugins",
    component: AdminPlugins,
    meta: { noindex: true },
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
    meta: { noindex: true },
  },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

export default router;
