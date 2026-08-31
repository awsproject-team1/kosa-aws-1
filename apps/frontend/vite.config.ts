import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  const target = env.VITE_API_PROXY_TARGET;
  return {
    plugins: [react()],
    server: target ? { proxy: { "/assessments": { target, changeOrigin: true } } } : undefined,
  };
});
