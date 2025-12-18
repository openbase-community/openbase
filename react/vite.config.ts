import react from "@vitejs/plugin-react-swc";
// import { componentTagger } from "lovable-tagger";
import path from "path";
import { defineConfig } from "vite";

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => ({
  server: {
    host: "::",
    port: 8080,
  },
  plugins: [react()].filter(Boolean),
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  define: {
    // Make VITE_IS_CLOUD available as a boolean in the frontend
    __IS_CLOUD__: JSON.stringify(process.env.VITE_IS_CLOUD === "true"),
  },
}));
