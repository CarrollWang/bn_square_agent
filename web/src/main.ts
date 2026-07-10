import "element-plus/es/components/message-box/style/css";
import "element-plus/es/components/message/style/css";
import "./styles.css";

import { createPinia } from "pinia";
import { createApp } from "vue";

import App from "./App.vue";
import { router } from "./router";

createApp(App)
  .use(createPinia())
  .use(router)
  .mount("#app");
