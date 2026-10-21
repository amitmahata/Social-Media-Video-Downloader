// Builds the minified browser files served from app/static/.
// Edit the readable sources in frontend/, then run `npm run build`.
import { build } from "esbuild";

const shared = {
  bundle: true,
  minify: true,
  sourcemap: false, // no source maps: the readable source isn't published
  legalComments: "none",
  logLevel: "info",
};

await build({
  ...shared,
  entryPoints: ["frontend/main.js"],
  outfile: "app/static/app.js",
  format: "iife",
  target: ["es2019", "safari13", "chrome80", "firefox78"],
});

await build({
  ...shared,
  entryPoints: ["frontend/style.css"],
  outfile: "app/static/style.css",
  target: ["safari13", "chrome80", "firefox78"],
});
