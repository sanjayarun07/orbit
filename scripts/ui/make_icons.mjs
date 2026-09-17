// Render the Orbit mark to the PNG icons a PWA needs (Chrome via Playwright; no image library needed).
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const outDir = process.argv[2];
mkdirSync(outDir, { recursive: true });
const browser = await chromium.launch({ channel: "chrome" });

// The sidebar mark (stroke icon) on the app's dark ground; `pad` is the safe
// margin as a fraction of the tile -- maskable icons keep the mark inside the
// central 80% so any platform mask leaves it whole.
function svg(size, { radius, pad }) {
  const inner = size * (1 - 2 * pad);
  const off = size * pad;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1c1e3a"/><stop offset="1" stop-color="#111214"/></linearGradient></defs>
  <rect width="${size}" height="${size}" rx="${radius}" fill="url(#g)"/>
  <g transform="translate(${off} ${off}) scale(${inner / 24})" fill="none" stroke="#a5b4fc" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
    <path d="M5 15.5 9.5 11l3 3L19 7.5"/><path d="M19 12v7H5V5h8"/>
  </g></svg>`;
}

async function render(name, size, opts) {
  const page = await browser.newPage({ viewport: { width: size, height: size }, deviceScaleFactor: 1 });
  await page.setContent(`<style>html,body{margin:0;background:transparent}</style>${svg(size, opts)}`);
  await page.screenshot({ path: `${outDir}/${name}`, omitBackground: true, clip: { x: 0, y: 0, width: size, height: size } });
  await page.close();
  console.log("wrote", name);
}

await render("icon-192.png", 192, { radius: 40, pad: 0.18 });
await render("icon-512.png", 512, { radius: 108, pad: 0.18 });
await render("icon-maskable-512.png", 512, { radius: 0, pad: 0.26 });
await render("apple-touch-icon.png", 180, { radius: 0, pad: 0.2 });   // iOS applies its own corner mask
await browser.close();
