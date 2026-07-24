import { createApp } from "vue";
import { createPinia } from "pinia";
import { createHead } from "@unhead/vue/client";
import { create, NConfigProvider } from "naive-ui";
import App from "./App.vue";
import "./assets/theme.css";
import router from "./router";
import "./plugins/highlight";

const naive = create({
  components: [NConfigProvider],
});
const app = createApp(App);
const head = createHead();

app.use(head);
app.use(naive);
app.use(createPinia());
app.use(router);

async function bootstrap(): Promise<void> {
  try {
    await router.isReady();
  } catch (error) {
    console.error("Initial route resolution failed", error);
  }

  app.mount("#app");
  document.documentElement.classList.remove("route-snapshot-mismatch");
}

void bootstrap();
