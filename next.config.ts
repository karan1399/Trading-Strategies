import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // The repo carries TWO lockfiles (bun.lock + package-lock.json), which makes
  // Turbopack infer the workspace root one directory above the app. That nests
  // the standalone output under .next/standalone/<repo>/<app>/ and breaks the
  // static-asset copy in `npm run build` (JS chunks then 404 in production).
  // Pinning the root to this directory keeps .next/standalone/server.js where
  // scripts/start-server.mjs expects it.
  turbopack: {
    root: __dirname,
  },
  /* type errors fail the build — non-app dirs (examples/scripts/tests)
     are excluded in tsconfig.json */
  typescript: {
    ignoreBuildErrors: false,
  },
  reactStrictMode: false,
  // Live preview is served from https://{port}-{sandboxId}.e2b.app. Next.js dev
  // blocks cross-origin dev resources (CSS/JS) by default, which leaves the
  // preview unstyled. Dev-only setting; it has no effect on production builds.
  allowedDevOrigins: ["*.e2b.app"],
};

export default nextConfig;
