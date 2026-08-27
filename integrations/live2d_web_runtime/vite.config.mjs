import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const sdkRoot = process.env.ARIA_CUBISM_SDK_ROOT;

if (!sdkRoot) throw new Error("ARIA_CUBISM_SDK_ROOT is required");

export default {
  resolve: {
    extensions: [".ts", ".js"],
    alias: {
      "@framework": path.resolve(sdkRoot, "Framework/src"),
    },
  },
  build: {
    target: "es2020",
    emptyOutDir: true,
    outDir: path.resolve(here, "dist"),
    lib: {
      entry: path.resolve(here, "src/main.ts"),
      name: "AriaCubismRuntimeBundle",
      formats: ["iife"],
      fileName: () => "adapter.js",
    },
  },
};
