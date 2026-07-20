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

app.mount("#app");
