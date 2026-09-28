// Browser UAT of the news/stocks/crypto/meme journeys (docs/engineering/uat-news-stocks-crypto-memes.md):
// the real UI, a signed-in session, Home card taps, streamed progress, follow-ups, screenshots per turn.
// Usage: node scripts/ui/uat_journeys.mjs <outDir> <sessionTokenFile> [desktop|mobile] [baseUrl] [journeyIds,comma]
import { chromium, devices } from "playwright";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";

const out = process.argv[2] || "reports/uat-browser";
const token = readFileSync(process.argv[3], "utf8").trim();
const mode = process.argv[4] || "desktop";
const base = process.argv[5] || "http://localhost:8000";
const only = (process.argv[6] || "").split(",").filter(Boolean);
mkdirSync(out, { recursive: true });

const WALLET = "3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS";
const JOURNEYS = [
  { id: "news_event", tap: "news", turns: ["What happened, and what does it mean for the market?", "Which part is confirmed, and which is your interpretation?"] },
  { id: "news_freshness", turns: ["What is actually new in crypto today?", "Is this old news recirculating?"] },
  { id: "news_calendar", turns: ["What events could move markets this week?", { followup: true, fallback: "Which date and timezone is the next high-impact event scheduled for?" }] },
  { id: "stock_identity", turns: ["Why is BP moving today?", "Is that BP plc or a tokenized BP market?"] },
  { id: "stock_fundamentals", turns: ["How did AAPL's latest reported quarter compare with expectations?", "What came from the filing versus analyst estimates?"] },
  { id: "stock_venue", turns: ["Which tokenized stocks are moving on Hyperliquid?", "Only the 24-hour quote-volume field."] },
  { id: "crypto_live", turns: ["Why is SOL moving today?", "What is the live price versus the reported catalyst?"] },
  { id: "crypto_event", turns: ["When exactly is Solana Alpenglow scheduled to activate on mainnet?"] },
  { id: "crypto_derivatives", turns: ["How are NEAR funding and open interest on Hyperliquid now?"] },
  { id: "meme_identity", turns: ["Check SPX.", "I mean SPX6900 the meme token, not the index. What did it do today?"] },
  { id: "meme_holders", turns: ["Who holds BONK? Are the largest wallets exchanges, pools, or unknown?", "Is the largest account the deployer or funded by it?"] },
  { id: "meme_launch", turns: ["Deep dive this Solana meme token: deployer, early buyers, bundle/sniping, liquidity and rug risk. Mint DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 (BONK)."] },
  { id: "meme_exit", turns: [`For wallet ${WALLET}, can I exit my ANSEM position? Read-only, do not prepare or submit anything.`, `Watch my exit on ANSEM.`] },
];

const device = mode === "mobile" ? { ...devices["iPhone 13"], deviceScaleFactor: 2 } : { viewport: { width: 1280, height: 900 } };
const browser = await chromium.launch({ channel: "chrome" });
const ctx = await browser.newContext({ ...device, colorScheme: "dark" });
await ctx.addCookies([{ name: "orbit_user", value: token, url: base }]);
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(String(e).slice(0, 300)));
page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text().slice(0, 300)); });

const records = [];
const shots = [];
let shotNo = 0;

async function shot(name) {
  shotNo += 1;
  const file = `${out}/${String(shotNo).padStart(2, "0")}-${name}.png`;
  await page.screenshot({ path: file, fullPage: false });
  shots.push(file);
  return file;
}

async function overflow() {
  return page.evaluate(() => {
    const doc = document.documentElement;
    const wide = [];
    for (const el of document.querySelectorAll("body *")) {
      const b = el.getBoundingClientRect();
      if (b.width > 0 && (b.right > window.innerWidth + 1 || b.left < -1) && getComputedStyle(el).visibility !== "hidden" && el.offsetParent !== null) {
        wide.push(`${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}.${(typeof el.className === "string" ? el.className : "").trim().split(/\s+/).slice(0, 2).join(".")} right=${Math.round(b.right)}`);
      }
    }
    return { scrollW: doc.scrollWidth, innerW: window.innerWidth, wide: wide.slice(0, 8) };
  });
}

async function newChat() {
  if (mode === "mobile") { await page.click("#mobileMenu").catch(() => {}); await page.waitForTimeout(300); }
  await page.click("#newChatBtn");
  await page.keyboard.press("Escape").catch(() => {});
  await page.click("#sidebarBackdrop").catch(() => {});
  await page.waitForTimeout(800);
}

async function assistantCount() { return page.locator(".message.assistant").count(); }
async function lastAssistantText() {
  return page.evaluate(() => { const all = document.querySelectorAll(".message.assistant"); const el = all[all.length - 1]; return el ? (el.querySelector(".message-body") || el).innerText : ""; });
}

async function waitAnswer(before, timeoutMs = 300000) {
  const t0 = Date.now();
  const progress = [];
  let last = "", stableSince = 0;
  while (Date.now() - t0 < timeoutMs) {
    await page.waitForTimeout(500);
    const state = await page.evaluate(() => {
      const all = document.querySelectorAll(".message.assistant"); const el = all[all.length - 1];
      const status = el?.querySelector(".stream-status")?.textContent?.trim() || "";
      const activity = el?.querySelector(".work-progress-activity")?.innerText?.trim() || "";
      const stages = [...(el?.querySelectorAll(".work-progress-steps span") || [])].filter(s => s.className.includes("done") || s.className.includes("active")).map(s => s.textContent.trim());
      const streaming = !!el?.querySelector(".answer.streaming");
      return { status, activity, stages, streaming, n: all.length };
    });
    for (const line of [state.status, ...state.activity.split("\n")].map(s => s.trim()).filter(Boolean)) if (progress[progress.length - 1] !== line) progress.push(line);
    if (state.n > before) {
      const text = await lastAssistantText();
      if (text && text === last && !state.streaming) { if (!stableSince) stableSince = Date.now(); else if (Date.now() - stableSince > 3000) return { text, progress: progress.slice(0, 40), ms: Date.now() - t0, stages: state.stages }; }
      else { last = text; stableSince = 0; }
    }
  }
  return { text: await lastAssistantText(), progress: progress.slice(0, 40), ms: Date.now() - t0, timedOut: true };
}

async function send(text) {
  const before = await assistantCount();
  await page.fill("#messageInput", text);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(1500);
  const streamingShot = await shot("streaming");
  const result = await waitAnswer(before);
  const followups = await page.evaluate(() => [...document.querySelectorAll(".message.assistant:last-of-type .followup-action, .message.assistant:last-of-type .related-item")].map(b => b.getAttribute("aria-label") || b.textContent.trim()));
  return { ...result, followups, streamingShot };
}

await page.goto(`${base}/ui/`);
await page.waitForTimeout(1500);
const signedIn = await page.evaluate(async () => { try { const r = await fetch("/me"); return r.ok; } catch { return false; } });
records.push({ note: "signed_in", signedIn, mode });
await shot("home");

for (const j of JOURNEYS) {
  if (only.length && !only.includes(j.id)) continue;
  await newChat();
  let turnNo = 0;
  if (j.tap === "news") {
    turnNo += 1;
    const card = page.locator('#homePrompts .prompt-card[data-msg^="What does this mean for the market"]').first();
    await card.waitFor({ timeout: 10000 }).catch(() => {});      // the Home highlights load after the chat resets
    let present = await card.count();
    if (!present) { await page.waitForTimeout(2500); present = await card.count(); console.log(`tap: ${present} card(s) after retry; grid hidden=${await page.evaluate(() => document.querySelector("#homePrompts")?.hidden)}`); }
    if (!present) { records.push({ journey: j.id, turn: turnNo, prompt: "(Home news tap)", error: "no news card on Home" }); }
    else {
      const title = await card.locator("strong").innerText();
      const msg = await card.getAttribute("data-msg");
      const before = await assistantCount();
      await card.click();
      await page.waitForTimeout(1500);
      const streamingShot = await shot(`${j.id}-tap-streaming`);
      const result = await waitAnswer(before);
      const followups = await page.evaluate(() => [...document.querySelectorAll(".message.assistant:last-of-type .followup-action, .message.assistant:last-of-type .related-item")].map(b => b.getAttribute("aria-label") || b.textContent.trim()));
      const file = await shot(`${j.id}-tap-answer`);
      records.push({ journey: j.id, turn: turnNo, prompt: msg, card_title: title, ...result, followups, streamingShot, screenshot: file, overflow: mode === "mobile" ? await overflow() : undefined });
    }
  }
  for (const t of j.turns) {
    turnNo += 1;
    let prompt = typeof t === "string" ? t : null;
    let usedFollowup = false;
    if (t && typeof t === "object" && t.followup) {
      const chip = page.locator(".message.assistant:last-of-type .followup-action, .message.assistant:last-of-type .related-item").first();
      if (await chip.count()) { prompt = await chip.getAttribute("aria-label"); usedFollowup = true; }
      else prompt = t.fallback;
    }
    let result;
    if (usedFollowup) {
      const before = await assistantCount();
      await page.locator(".message.assistant:last-of-type .followup-action, .message.assistant:last-of-type .related-item").first().click();
      await page.waitForTimeout(1500);
      const streamingShot = await shot(`${j.id}-${turnNo}-streaming`);
      result = { ...(await waitAnswer(before)), streamingShot, followups: [] };
    } else {
      result = await send(prompt);
    }
    const file = await shot(`${j.id}-${turnNo}-answer`);
    records.push({ journey: j.id, turn: turnNo, prompt, usedFollowup, ...result, screenshot: file, overflow: mode === "mobile" ? await overflow() : undefined });
    console.log(`${j.id}.${turnNo} ${Math.round(result.ms / 1000)}s ${result.timedOut ? "TIMEOUT " : ""}${(result.text || "").slice(0, 90).replace(/\n/g, " ")}`);
  }
}

if (mode === "mobile") {
  await page.click("#mobileMenu").catch(() => {});
  await page.waitForTimeout(400);
  records.push({ note: "drawer", screenshot: await shot("drawer"), overflow: await overflow() });
  await page.keyboard.press("Escape").catch(() => {});
}
await ctx.close();
await browser.close();
writeFileSync(`${out}/turns.json`, JSON.stringify({ mode, base, records, errors: errors.slice(0, 40), shots }, null, 1));
console.log("done", records.length, "records,", errors.length, "page errors");
