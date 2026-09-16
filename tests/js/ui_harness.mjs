/**
 * Runs the real inline script from app/static/index.html against a stub DOM so
 * its functions can be called and asserted on.
 *
 * This exists because two browser defects in a row reached review while 900+
 * Python tests passed: a quote path that referenced an undeclared variable and
 * so threw before ever calling Relay, and a dialog that disabled its own button
 * on a failure path and never re-enabled it. Neither is reachable from Python,
 * and `node --check` only proves the file parses.
 *
 * Deliberately a stub, not a real DOM: the point is to exercise the script's
 * own logic and the values it passes outward, not to render anything. Anything
 * the script touches that is not modelled here returns an inert element rather
 * than throwing, so page-load side effects do not have to be enumerated.
 *
 * Usage: node ui_harness.mjs <case>   -- prints one JSON result object.
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
const INDEX = resolve(HERE, "../../app/static/index.html");

function inlineScript() {
  const html = readFileSync(INDEX, "utf8");
  const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
  // The largest block is the application script; the small one is the
  // pre-paint theme setter.
  return blocks.sort((a, b) => b.length - a.length)[0];
}

/** Every element the script asks for, remembered by selector so a test can
 *  read back what the script did to it. */
function makeDom() {
  const elements = new Map();
  const make = (key) => {
    const listeners = {};
    const el = {
      __key: key, value: "", textContent: "", innerHTML: "", className: "",
      disabled: false, hidden: false, checked: false, dataset: {},
      style: { setProperty() {}, removeProperty() {}, getPropertyValue: () => "" },
      children: [], isConnected: true,
      classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
      addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
      removeEventListener() {},
      dispatch(type, event = {}) { (listeners[type] || []).forEach(fn => fn({ preventDefault() {}, stopPropagation() {}, ...event })); },
      append() {}, appendChild(child) { el.children.push(child); return child; },
      replaceWith() {}, remove() {}, closest: () => null, focus() {}, blur() {}, scrollTo() {},
      setAttribute(name, v) { el[name] = v; }, getAttribute: (name) => el[name] ?? null,
      removeAttribute() {}, insertAdjacentHTML() {}, querySelector: () => query("descendant"),
      querySelectorAll: () => [], getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
    };
    return el;
  };
  const query = (selector) => {
    if (!elements.has(selector)) elements.set(selector, make(selector));
    return elements.get(selector);
  };
  const document = {
    getElementById: (id) => query(`#${id}`),
    querySelector: query,
    querySelectorAll: () => [],
    createElement: () => make("created"),
    createTextNode: () => make("text"),
    addEventListener() {}, removeEventListener() {},
    documentElement: make("html"), body: make("body"), head: make("head"),
    cookie: "", title: "", hidden: false, activeElement: null,
    visibilityState: "visible",
  };
  return { document, elements, query };
}

function makeSandbox(dom, overrides = {}) {
  const storage = () => {
    const store = new Map();
    return { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k), clear: () => store.clear() };
  };
  const recorded = { relayQuoteArgs: null, fetches: [] };
  const sandbox = {
    document: dom.document,
    console,
    recorded,
    addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
    alert() {}, scrollTo() {}, getComputedStyle: () => ({ getPropertyValue: () => "" }),
    setTimeout: (fn) => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: () => 0, cancelAnimationFrame() {},
    localStorage: storage(), sessionStorage: storage(),
    location: { pathname: "/ui/", search: "", href: "http://localhost/ui/", hash: "" },
    history: { replaceState() {}, pushState() {} },
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    navigator: { userAgent: "harness", clipboard: { writeText: async () => {} }, language: "en" },
    crypto: { randomUUID: () => "00000000-0000-4000-8000-000000000000", getRandomValues: a => a },
    URLSearchParams, URL, Intl, Date, Math, JSON, Promise, Number, String, Boolean, Array, Object,
    fetch: async (url) => { recorded.fetches.push(String(url)); return { ok: false, status: 503, json: async () => ({}), text: async () => "" }; },
    EventSource: class { constructor() {} addEventListener() {} close() {} },
    MutationObserver: class { observe() {} disconnect() {} takeRecords() { return []; } },
    IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
    Event: class { constructor(type) { this.type = type; } },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    FormData: class { append() {} }, Blob: class {}, File: class {},
    TextEncoder, TextDecoder, btoa, atob, structuredClone,
    AbortController: class { constructor() { this.signal = {}; } abort() {} },
    ...overrides,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.window.OrbitRelay = {
    constants: { SOLANA_CHAIN_ID: 792703809 },
    // The assertion target for the quote case: what the page actually sends.
    getFreshQuote: async (args) => { recorded.relayQuoteArgs = args; return { quote: { details: {}, fees: {}, steps: [] }, wallet: {}, createdAt: Date.now() }; },
    resolveCurrency: async (_chain, token) => token,
    recipientFor: async () => "recipient",
    chains: async () => [],
    supportedChains: async () => [
      { id: 792703809, name: "Solana", nativeSymbol: "SOL", vmType: "svm" },
      { id: 8453, name: "Base", nativeSymbol: "ETH", vmType: "evm" },
    ],
  };
  sandbox.window.OrbitExecutors = { relay: { status: async () => ({ status: "pending" }) }, executeRelay: async () => {} };
  return sandbox;
}

function load(overrides = {}) {
  const dom = makeDom();
  const sandbox = makeSandbox(dom, overrides);
  const context = vm.createContext(sandbox);
  // Page-load side effects are not what is under test; a failure to wire some
  // unmodelled widget must not hide the function we came to call.
  try {
    new vm.Script(inlineScript(), { filename: "index.inline.js" }).runInContext(context);
  } catch (error) {
    sandbox.__loadError = String(error && error.message);
  }
  // Top-level `let`/`const` in the script land in the context's lexical scope,
  // not on the sandbox object, so they can only be read or written by running
  // code inside the context. Assigning sandbox.relayChains would silently do
  // nothing, which is its own way to write a test that proves nothing.
  const evalIn = (code) => vm.runInContext(code, context);
  const setScriptVar = (name, value) => {
    sandbox.__harnessValue = value;
    evalIn(`${name} = __harnessValue;`);
  };
  const getScriptVar = (name) => evalIn(name);
  return { dom, sandbox, context, evalIn, setScriptVar, getScriptVar };
}

const SOLANA = { id: 792703809, name: "Solana", nativeSymbol: "SOL", vmType: "svm" };
const BASE = { id: 8453, name: "Base", nativeSymbol: "ETH", vmType: "evm" };

const CASES = {
  /** A standalone dialog quote with valid inputs must reach Relay. */
  async quote_reaches_relay() {
    const { dom, sandbox, setScriptVar } = load();
    setScriptVar("relayChains", [SOLANA, BASE]);
    dom.query("#relayFromChain").value = "792703809";
    dom.query("#relayToChain").value = "8453";
    dom.query("#relayFromToken").value = "SOL";
    dom.query("#relayToToken").value = "USDC";
    dom.query("#relayAmount").value = "0.01";
    dom.query("#relayRecipient").value = "0x1111111111111111111111111111111111111111";
    dom.query("#relaySlippage").value = "50";
    await sandbox.requestRelayQuote();
    return {
      loadError: sandbox.__loadError ?? null,
      quoteArgs: sandbox.recorded.relayQuoteArgs,
      status: dom.query("#relayStatus").textContent,
    };
  },

  /** Blank slippage is valid and means Relay's automatic slippage. */
  async quote_allows_blank_slippage() {
    const { dom, sandbox, setScriptVar } = load();
    setScriptVar("relayChains", [BASE]);
    dom.query("#relayFromChain").value = "8453";
    dom.query("#relayToChain").value = "8453";
    dom.query("#relayFromToken").value = "ETH";
    dom.query("#relayToToken").value = "USDC";
    dom.query("#relayAmount").value = "1";
    dom.query("#relaySlippage").value = "";
    await sandbox.requestRelayQuote();
    return { quoteArgs: sandbox.recorded.relayQuoteArgs, status: dom.query("#relayStatus").textContent };
  },

  /** Out-of-range slippage is refused before any network call. */
  async quote_rejects_impossible_slippage() {
    const { dom, sandbox, setScriptVar } = load();
    setScriptVar("relayChains", [BASE]);
    dom.query("#relayFromChain").value = "8453";
    dom.query("#relayToChain").value = "8453";
    dom.query("#relayFromToken").value = "ETH";
    dom.query("#relayToToken").value = "USDC";
    dom.query("#relayAmount").value = "1";
    dom.query("#relaySlippage").value = "99999";
    await sandbox.requestRelayQuote();
    return { quoteArgs: sandbox.recorded.relayQuoteArgs, status: dom.query("#relayStatus").textContent };
  },

  /** A quote still in flight when the inputs change must be discarded.
   *
   * The reviewed sequence: ask for 50 bps, change the field to 1 while the
   * response is outstanding, then press sign. The response used to be assigned
   * straight to relayQuoteState after the await, restoring the superseded
   * quote over inputs the user had already changed -- so the handler signed a
   * 50 bps quote for someone looking at a form that said 1.
   */
  async quote_in_flight_is_discarded_when_inputs_change() {
    const { dom, sandbox, setScriptVar, getScriptVar } = load();
    setScriptVar("relayChains", [SOLANA, BASE]);
    dom.query("#relayFromChain").value = "792703809";
    dom.query("#relayToChain").value = "8453";
    dom.query("#relayFromToken").value = "SOL";
    dom.query("#relayToToken").value = "USDC";
    dom.query("#relayAmount").value = "0.01";
    dom.query("#relayRecipient").value = "0x1111111111111111111111111111111111111111";
    dom.query("#relaySlippage").value = "50";

    // Hold the first response open until the inputs have changed under it.
    let release;
    const held = new Promise(resolve => { release = resolve; });
    sandbox.window.OrbitRelay.getFreshQuote = async (args) => {
      sandbox.recorded.relayQuoteArgs = args;
      await held;
      return { quote: { details: {}, fees: {}, steps: [], slippageBps: args.slippageBps }, wallet: {}, createdAt: Date.now() };
    };

    const pending = sandbox.requestRelayQuote();
    // The user edits the field; this is what the dialog's input listener calls.
    dom.query("#relaySlippage").value = "1";
    sandbox.invalidateRelayQuote();
    release();
    await pending;

    const signed = [];
    sandbox.window.OrbitExecutors.executeRelay = async (quote) => { signed.push(quote); };
    await sandbox.executeRelayQuote();

    return {
      requestedSlippage: sandbox.recorded.relayQuoteArgs?.slippageBps,
      quoteRetained: Boolean(getScriptVar("relayQuoteState")),
      signedQuotes: signed.length,
      status: dom.query("#relayStatus").textContent,
    };
  },

  /** Inputs changed AFTER a quote was reviewed must also block signing. */
  async reviewed_quote_cannot_be_signed_after_inputs_change() {
    const { dom, sandbox, setScriptVar, getScriptVar } = load();
    setScriptVar("relayChains", [SOLANA, BASE]);
    dom.query("#relayFromChain").value = "792703809";
    dom.query("#relayToChain").value = "8453";
    dom.query("#relayFromToken").value = "SOL";
    dom.query("#relayToToken").value = "USDC";
    dom.query("#relayAmount").value = "0.01";
    dom.query("#relayRecipient").value = "0x1111111111111111111111111111111111111111";
    dom.query("#relaySlippage").value = "50";

    await sandbox.requestRelayQuote();
    const reviewed = Boolean(getScriptVar("relayQuoteState"));

    // Quote on screen, then the user edits an input before pressing sign.
    dom.query("#relayAmount").value = "10";
    sandbox.invalidateRelayQuote();

    const signed = [];
    sandbox.window.OrbitExecutors.executeRelay = async (quote) => { signed.push(quote); };
    await sandbox.executeRelayQuote();
    return { reviewed, signedQuotes: signed.length, status: dom.query("#relayStatus").textContent };
  },

  /** The execute-side revision check on its own.
   *
   * Today invalidateRelayQuote both bumps the revision and clears the quote,
   * so executeRelayQuote returns at its null check before reaching the
   * revision check. That makes the second check defence in depth rather than
   * the active guard -- it catches the easy future mistake of bumping the
   * revision without clearing the quote. This case creates exactly that state
   * so the guard is actually exercised instead of being dead code nobody has
   * ever seen run.
   */
  async execution_refuses_a_quote_from_a_superseded_revision() {
    const { dom, sandbox, setScriptVar, evalIn } = load();
    setScriptVar("relayChains", [SOLANA, BASE]);
    dom.query("#relayFromChain").value = "792703809";
    dom.query("#relayToChain").value = "8453";
    dom.query("#relayFromToken").value = "SOL";
    dom.query("#relayToToken").value = "USDC";
    dom.query("#relayAmount").value = "0.01";
    dom.query("#relayRecipient").value = "0x1111111111111111111111111111111111111111";
    dom.query("#relaySlippage").value = "50";
    await sandbox.requestRelayQuote();

    // Revision moves on while the reviewed quote is still held.
    evalIn("relayInputRevision++;");

    const signed = [];
    sandbox.window.OrbitExecutors.executeRelay = async (quote) => { signed.push(quote); };
    await sandbox.executeRelayQuote();
    return { signedQuotes: signed.length, status: dom.query("#relayStatus").textContent };
  },

  /** An unchanged quote must still be signable -- the guard must not be a
   *  blanket refusal that quietly breaks swapping altogether. */
  async an_unchanged_reviewed_quote_still_signs() {
    const { dom, sandbox, setScriptVar } = load();
    setScriptVar("relayChains", [SOLANA, BASE]);
    dom.query("#relayFromChain").value = "792703809";
    dom.query("#relayToChain").value = "8453";
    dom.query("#relayFromToken").value = "SOL";
    dom.query("#relayToToken").value = "USDC";
    dom.query("#relayAmount").value = "0.01";
    dom.query("#relayRecipient").value = "0x1111111111111111111111111111111111111111";
    dom.query("#relaySlippage").value = "50";

    await sandbox.requestRelayQuote();
    const signed = [];
    sandbox.window.OrbitExecutors.executeRelay = async (quote) => { signed.push(quote); };
    await sandbox.executeRelayQuote();
    return { signedQuotes: signed.length, status: dom.query("#relayStatus").textContent };
  },

  /** The dialog must recover once a configuration fetch finally succeeds. */
  async dialog_recovers_after_config_failure() {
    let failNext = true;
    const { dom, sandbox, getScriptVar, setScriptVar } = load();
    sandbox.fetch = async (url) => {
      if (String(url).includes("/config/public")) {
        if (failNext) throw new Error("simulated failure");
        return { ok: true, status: 200, json: async () => ({ deployment: { execution_enabled: true }, x402: null }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    };
    setScriptVar("executionConfigLoaded", false);
    setScriptVar("executionEnabled", false);

    await sandbox.openRelayDialog();
    const afterFailure = {
      quoteDisabled: dom.query("#relayQuoteBtn").disabled,
      status: dom.query("#relayStatus").textContent,
    };

    failNext = false;
    await sandbox.openRelayDialog();
    const afterRecovery = {
      quoteDisabled: dom.query("#relayQuoteBtn").disabled,
      quoteLabel: dom.query("#relayQuoteBtn").textContent,
      status: dom.query("#relayStatus").textContent,
      executionEnabled: getScriptVar("executionEnabled"),
    };
    return { afterFailure, afterRecovery };
  },

  /** Research mode says research mode, not "could not confirm". */
  async dialog_reports_research_mode_distinctly() {
    const { dom, sandbox, getScriptVar, setScriptVar } = load();
    sandbox.fetch = async (url) => String(url).includes("/config/public")
      ? { ok: true, status: 200, json: async () => ({ deployment: { execution_enabled: false }, x402: null }) }
      : { ok: true, status: 200, json: async () => ({}) };
    setScriptVar("executionConfigLoaded", false);
    await sandbox.openRelayDialog();
    return {
      quoteDisabled: dom.query("#relayQuoteBtn").disabled,
      status: dom.query("#relayStatus").textContent,
      configLoaded: getScriptVar("executionConfigLoaded"),
    };
  },
};

const name = process.argv[2];
const runner = CASES[name];
if (!runner) {
  console.log(JSON.stringify({ error: `unknown case ${name}`, available: Object.keys(CASES) }));
  process.exit(2);
}
runner().then(
  result => console.log(JSON.stringify(result)),
  error => { console.log(JSON.stringify({ error: String(error && error.stack || error) })); process.exit(1); },
);
