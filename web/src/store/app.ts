import { defineStore } from "pinia";

import type { SourceType } from "@/types";

export const useAppStore = defineStore("app", {
  state: () => ({
    activeSourceType: "news_feed" as SourceType,
  }),
  actions: {
    setSourceType(sourceType: SourceType) {
      this.activeSourceType = sourceType;
    },
  },
});
