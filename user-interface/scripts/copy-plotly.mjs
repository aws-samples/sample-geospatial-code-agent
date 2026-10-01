// Copies the prebuilt plotly.js bundle into public/ so it can be loaded via a
// <script> tag at runtime (see src/components/InteractiveChart.tsx for why).
// This keeps the ~5MB plotly bundle out of Vite's dep optimizer / es-module-lexer,
// which cannot handle a file that large on Vite 8 (Rolldown).
import { copyFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const src = resolve(root, 'node_modules/plotly.js/dist/plotly.min.js');
const destDir = resolve(root, 'public');
const dest = resolve(destDir, 'plotly.min.js');

if (!existsSync(src)) {
  console.error(`[copy-plotly] source not found: ${src}\nRun "npm install" first.`);
  process.exit(1);
}

mkdirSync(destDir, { recursive: true });
copyFileSync(src, dest);
console.log(`[copy-plotly] copied plotly.min.js -> public/plotly.min.js`);
