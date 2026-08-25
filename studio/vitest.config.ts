import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
    environment: "node",
    coverage: {
      reporter: ["text", "json-summary"],
      include: ["src/core/**/*.ts", "src/main/runtime/**/*.ts"],
    },
  },
});
