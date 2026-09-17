// Phone-width audit of every screen: iPhone 13 emulation, screenshots into ./mobile/<name>.png
// Usage: node mobile_audit.mjs [outDir] [baseUrl]
import { chromium, devices } from "playwright";
import { mkdirSync } from "node:fs";

const out = process.argv[2] || "mobile";
const base = process.argv[3] || "http://localhost:8000";
mkdirSync(out, { recursive: true });
const device = { ...devices["iPhone 13"], deviceScaleFactor: 2 };

const browser = await chromium.launch({ channel: "chrome" });
const findings = [];

async function overflow(page, name) {
  const r = await page.evaluate(() => {
    const doc = document.documentElement;
    const wide = [];
    for (const el of document.querySelectorAll("body *")) {
      const b = el.getBoundingClientRect();
      if (b.width > 0 && (b.right > window.innerWidth + 1 || b.left < -1) && getComputedStyle(el).visibility !== "hidden" && el.offsetParent !== null) {
        wide.push(`${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}${el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).slice(0, 2).join(".") : ""} right=${Math.round(b.right)} left=${Math.round(b.left)}`);
      }
    }
    const small = [];
    for (const el of document.querySelectorAll("button, a, input, select, textarea, [role=button]")) {
      const b = el.getBoundingClientRect();
      if (b.width > 0 && b.height > 0 && el.offsetParent !== null && (b.height < 32 || b.width < 32)) small.push(`${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}.${(typeof el.className === "string" ? el.className : "").trim().split(/\s+/)[0]} ${Math.round(b.width)}x${Math.round(b.height)}`);
    }
    const fonts = [];
    for (const el of document.querySelectorAll("input, textarea, select")) {
      if (el.offsetParent !== null && parseFloat(getComputedStyle(el).fontSize) < 16) fonts.push(`${el.tagName.toLowerCase()}#${el.id} ${getComputedStyle(el).fontSize}`);
    }
    return { scrollW: doc.scrollWidth, innerW: window.innerWidth, wide: wide.slice(0, 12), small: small.slice(0, 20), fonts: fonts.slice(0, 20) };
  });
  findings.push({ name, ...r });
}

async function shot(page, name, full = false) {
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${out}/${name}.png`, fullPage: full });
  await overflow(page, name);
}

// --- signed in ---------------------------------------------------------------
const ctx = await browser.newContext({ ...device, colorScheme: "dark" });
const start = await ctx.request.post(`${base}/auth/email/start`, { data: { email: "mobile-audit@example.com" } });
const link = (await start.json()).dev_link || "";
await ctx.request.post(`${base}/auth/email/verify`, { data: { token: link.split("signin=")[1] || "" } });
const page = await ctx.newPage();
page.on("pageerror", (e) => findings.push({ name: "pageerror", error: String(e) }));
await page.goto(`${base}/ui/`);
await page.waitForTimeout(1200);
await shot(page, "01-home");
await page.click("#mobileMenu");
await shot(page, "02-drawer");
await page.click("#navLibrary");
await shot(page, "03-history");
await page.keyboard.press("Escape"); await page.waitForTimeout(300);
await page.click("#mobileMenu");
await page.click("#navTasks");
await shot(page, "04-tasks");
await page.keyboard.press("Escape"); await page.waitForTimeout(300);
await page.click("#mobileMenu");
await page.click("#navSettings");
await shot(page, "05-settings-account");
for (const tab of ["billing", "keys", "preferences", "tasks", "notifications", "data"]) {
  const t = page.locator(`#profileDialog [data-tab="${tab}"]`).first();
  if (await t.count()) { await t.click(); await shot(page, `05-settings-${tab}`); }
}
await page.keyboard.press("Escape");
await page.evaluate(() => typeof openPlans === "function" && openPlans());
await shot(page, "06-plans");
await page.keyboard.press("Escape");
await page.click("#walletButton");
await shot(page, "07-wallet");
await page.keyboard.press("Escape");
// a chat turn with cards
await page.keyboard.press("Escape"); await page.waitForTimeout(300);
await page.click("#mobileMenu");
await page.click("#navHome");
await page.keyboard.press("Escape").catch(() => {});
await page.click("#sidebarBackdrop").catch(() => {});
await page.fill("#messageInput", "what is the price of SOL");
await page.keyboard.press("Enter");
await page.waitForTimeout(2500);
await shot(page, "08-chat-streaming");
await page.waitForSelector(".message.assistant .rendered-result table, .message.assistant .answer-result", { timeout: 60000 }).catch(() => {});
await page.waitForTimeout(1500);
await shot(page, "09-chat-answer");
await shot(page, "09-chat-answer-full", true);
await page.fill("#messageInput", "swap 0.01 sol to usdc on solana");
await page.keyboard.press("Enter");
await page.waitForTimeout(12000);
await shot(page, "10-swap-card");
await shot(page, "10-swap-card-full", true);
// light theme home
await page.emulateMedia({ colorScheme: "light" });
await page.click("#themeToggle").catch(() => {});
await page.click("#mobileMenu");
await page.click("#newChatBtn");
await shot(page, "11-home-light");
await ctx.close();

// --- signed out: sign-in dialog -------------------------------------------------
const ctx2 = await browser.newContext({ ...device, colorScheme: "dark" });
const p2 = await ctx2.newPage();
await p2.goto(`${base}/ui/`);
await p2.waitForTimeout(1000);
await p2.evaluate(() => typeof openSignin === "function" && openSignin());
await shot(p2, "12-signin");
await p2.goto(`${base}/ui/admin.html`);
await p2.waitForTimeout(800);
await shot(p2, "13-admin");
await shot(p2, "13-admin-full", true);
await ctx2.close();
await browser.close();
console.log(JSON.stringify(findings, null, 1));
