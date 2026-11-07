import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true }, // the API's CORS allows this origin
  test: { environment: "node" },
});
