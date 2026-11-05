import path from "node:path";
import type { NextConfig } from "next";

// Env is loaded from the repo-root .env by scripts/next.mjs, not here.
const nextConfig: NextConfig = {
  experimental: {
    agentUpgrade: "security",
  },
  cacheComponents: true,
  partialPrefetching: true,
  // Workspace packages ship TypeScript source.
  transpilePackages: ["@sc/client", "@sc/core", "@sc/db", "@sc/schemas"],
  turbopack: {
    root: path.join(__dirname, "../.."),
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
