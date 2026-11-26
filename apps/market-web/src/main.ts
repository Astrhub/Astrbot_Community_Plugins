import { createApp } from "vue";
import { createPinia } from "pinia";
import { createHead } from "@unhead/vue/client";
import { create, NConfigProvider } from "naive-ui";
import App from "./App.vue";
import "./assets/theme.css";
import router from "./router";
import { handleChunkLoadFailure, isChunkLoadError } from "./utils/bootRecovery";

window.addEventListener("vite:preloadError", (event) => {
  event.preventDefault();
  handleChunkLoadFailure((event as Event & { payload?: unknown }).payload ?? event);
});

router.onError((error) => {
  if (isChunkLoadError(error)) {
    handleChunkLoadFailure(error);
  }
});

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
    if (isChunkLoadError(error)) {
      handleChunkLoadFailure(error);
      return;
    }
  }

  try {
    app.mount("#app");
  } catch (error) {
    console.error("Application mount failed", error);
    window.__showBootError?.();
  }
}

void bootstrap();
