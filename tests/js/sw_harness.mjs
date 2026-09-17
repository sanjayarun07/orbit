// Runs app/static/sw.js in a node vm with a fake ServiceWorkerGlobalScope and
// reports what the fetch handler does with a request: whether it answered it
// (respondWith called) or left it to the network. Usage: node sw_harness.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "..", "app", "static", "sw.js"), "utf8");

function load() {
  const listeners = {};
  const cacheStore = new Map();
  const cache = {
    addAll: async (urls) => { for (const u of urls) cacheStore.set(u, `cached:${u}`); },
    match: async (req) => cacheStore.get(typeof req === "string" ? req : req.url) ?? undefined,
    put: async (req, res) => { cacheStore.set(typeof req === "string" ? req : req.url, res); },
  };
  const self = {
    location: { origin: "https://orbit.example" },
    addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
    skipWaiting: async () => {}, clients: { claim: async () => {} },
  };
  const sandbox = { self, caches: { open: async () => cache, match: (k) => cache.match(k), keys: async () => ["orbit-shell-v0", "orbit-shell-v1"], delete: async () => true }, URL, fetch: async (req) => ({ ok: true, clone: () => "net", url: req.url }), console };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return { listeners, cacheStore };
}

function fetchEvent(url, { method = "GET", mode = "cors" } = {}) {
  const event = { request: { url, method, mode }, responded: null, waited: null,
    respondWith(p) { this.responded = p; }, waitUntil(p) { this.waited = p; } };
  return event;
}

const cases = {
  async api_routes_are_never_intercepted() {
    const { listeners } = load();
    const out = {};
    for (const [name, url, opts] of [
      ["chat", "https://orbit.example/chat", { method: "POST" }],
      ["chat_stream", "https://orbit.example/chat/stream", { method: "POST" }],
      ["me", "https://orbit.example/me", {}],
      ["billing", "https://orbit.example/billing/plans", {}],
      ["readyz", "https://orbit.example/readyz", {}],
      ["cross_origin", "https://fonts.googleapis.com/css2?family=Inter", {}],
    ]) {
      const e = fetchEvent(url, opts);
      for (const fn of listeners.fetch) fn(e);
      out[name] = e.responded !== null;
    }
    return out;
  },
  async shell_files_are_answered_from_the_cache_and_refreshed() {
    const { listeners, cacheStore } = load();
    cacheStore.set("https://orbit.example/ui/chat.css?v=3", "cached:css");
    const e = fetchEvent("https://orbit.example/ui/chat.css?v=3");
    for (const fn of listeners.fetch) fn(e);
    const answered = await e.responded;
    return { answered, refreshed: cacheStore.get("https://orbit.example/ui/chat.css?v=3") };
  },
  async a_navigation_prefers_the_network_and_falls_back_to_the_shell() {
    const { listeners, cacheStore } = load();
    cacheStore.set("/ui/", "cached:shell");
    const e = fetchEvent("https://orbit.example/ui/?source=pwa", { mode: "navigate" });
    for (const fn of listeners.fetch) fn(e);
    const online = await e.responded;
    // offline: fetch rejects
    const { listeners: l2, cacheStore: c2 } = load();
    c2.set("/ui/", "cached:shell");
    const e2 = fetchEvent("https://orbit.example/ui/", { mode: "navigate" });
    const sandboxFetchFail = () => Promise.reject(new Error("offline"));
    // rebind fetch inside the vm by re-running with a failing fetch
    const ctx = vm.createContext({ self: { location: { origin: "https://orbit.example" }, addEventListener: (t, fn) => { (l2[t + "_off"] ||= []).push(fn); }, skipWaiting: async () => {}, clients: { claim: async () => {} } },
      caches: { open: async () => ({ match: async (k) => c2.get(typeof k === "string" ? k : k.url), put: async () => {}, addAll: async () => {} }), match: async (k) => c2.get(typeof k === "string" ? k : k.url), keys: async () => [], delete: async () => true }, URL, fetch: sandboxFetchFail, console });
    vm.runInContext(source, ctx);
    for (const fn of l2.fetch_off) fn(e2);
    const offline = await e2.responded;
    return { online: online && online.url, offline };
  },
  async install_precaches_the_shell_and_activate_drops_old_caches() {
    const { listeners, cacheStore } = load();
    const install = { waitUntil(p) { this.p = p; } };
    for (const fn of listeners.install) fn(install);
    await install.p;
    const activate = { waitUntil(p) { this.p = p; } };
    for (const fn of listeners.activate) fn(activate);
    await activate.p;
    return { precached: [...cacheStore.keys()] };
  },
};

const name = process.argv[2];
if (!cases[name]) { console.log(JSON.stringify({ error: `unknown case ${name}`, available: Object.keys(cases) })); process.exit(1); }
cases[name]().then((r) => console.log(JSON.stringify(r))).catch((e) => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(1); });
