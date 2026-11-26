import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

export default defineConfig({
  plugins: [viteSingleFile()],
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    cssCodeSplit: false,
    assetsInlineLimit: 100000000,
    rollupOptions: {
      input: "index.html",
      output: {
        inlineDynamicImports: true,
      },
    },
  },
});
