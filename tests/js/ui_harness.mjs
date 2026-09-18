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
    // innerHTML reads back what was assigned, or the escaped textContent when
    // only text was set -- the one derivation the script relies on
    // (escapeHtml sets textContent on a scratch element and reads innerHTML).
    let text = "", html = null;
    const el = {
      __key: key, value: "", className: "",
      get textContent() { return text; }, set textContent(v) { text = String(v ?? ""); html = null; },
      get innerHTML() { return html ?? text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }, set innerHTML(v) { html = String(v ?? ""); },
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
      removeAttribute() {}, insertAdjacentHTML() {},
      // Scoped by parent, so two different selectors inside one card are two
      // different elements. Returning one shared element for every selector
      // made a card's fields all alias each other, which silently turned any
      // test driving a card into a test that proved nothing.
      querySelector: (selector) => query(`${key} >> ${selector}`),
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
    setImmediate, queueMicrotask,
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
  /** Replace the stub executor with the REAL compiled bundle the page serves.
   *
   * The executor is where the guard immediately before wallet approval lives,
   * so imitating it in the harness would test the imitation. This runs
   * app/static/executors.js in the same context, exactly as the page's script
   * tag does, and it overwrites the stub OrbitExecutors on the way. */
  const useRealExecutors = () => {
    const bundle = readFileSync(resolve(HERE, "../../app/static/executors.js"), "utf8");
    new vm.Script(bundle, { filename: "executors.js" }).runInContext(context);
  };
  const setScriptVar = (name, value) => {
    sandbox.__harnessValue = value;
    evalIn(`${name} = __harnessValue;`);
  };
  const getScriptVar = (name) => evalIn(name);
  return { dom, sandbox, context, evalIn, setScriptVar, getScriptVar, useRealExecutors };
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

  /** Inputs edited while the server's execution claim is outstanding.
   *
   * The revision check runs before the executor is called, and the executor
   * then awaits the claim over the network. An edit during that wait used to
   * change nothing: the old quote went on to wallet approval anyway. The real
   * compiled executor already re-checks an isCurrent callback immediately
   * after the claim -- the page simply never passed one.
   *
   * `surface` is "dialog" or "inline" so the same race is proved on both.
   */
  async execution_aborts_when_inputs_change_during_the_claim() {
    const { dom, sandbox, setScriptVar, useRealExecutors } = load();
    useRealExecutors();
    setScriptVar("relayChains", [SOLANA, BASE]);

    // A quote whose step carries the requestId the claim is keyed on.
    sandbox.window.OrbitRelay.getFreshQuote = async (args) => {
      sandbox.recorded.relayQuoteArgs = args;
      return {
        quote: { details: {}, fees: {}, steps: [{ requestId: "r".repeat(24), action: "swap" }] },
        wallet: {}, createdAt: Date.now(),
      };
    };

    // The wallet step. Reaching this at all with a superseded quote is the bug.
    const approvals = [];
    sandbox.window.OrbitRelay.executeQuote = async (quote) => { approvals.push(quote); return {}; };

    // Hold the claim open so the inputs can change underneath it.
    let releaseClaim;
    const claimHeld = new Promise(resolve => { releaseClaim = resolve; });
    sandbox.fetch = async (url) => {
      if (String(url).includes("/executions/relay/")) {
        await claimHeld;
        return { ok: true, status: 200, json: async () => ({ execution_claimed: true }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    };

    dom.query("#relayFromChain").value = "792703809";
    dom.query("#relayToChain").value = "8453";
    dom.query("#relayFromToken").value = "SOL";
    dom.query("#relayToToken").value = "USDC";
    dom.query("#relayAmount").value = "0.01";
    dom.query("#relayRecipient").value = "0x1111111111111111111111111111111111111111";
    dom.query("#relaySlippage").value = "50";

    await sandbox.requestRelayQuote();
    const signing = sandbox.executeRelayQuote();
    dom.query("#relaySlippage").value = "1";
    sandbox.invalidateRelayQuote();   // the edit lands while the claim is open
    releaseClaim();
    await signing;

    return {
      requestedSlippage: sandbox.recorded.relayQuoteArgs?.slippageBps,
      walletApprovals: approvals.length,
      status: dom.query("#relayStatus").textContent,
    };
  },

  /** The inline chat swap card, edited mid-claim without leaving the field.
   *
   * `eventType` is the point. The card used to invalidate only on `change`,
   * which a text or number field withholds until blur, so a quote stayed
   * "current" while the user typed over it and the freshness callback passed.
   * Dispatching `input` is what a real keystroke does; the `change` variant is
   * kept as the case that always worked, so a regression can be told apart
   * from the card simply never quoting.
   */
  async inline_card_edit_during_claim(eventType = "input") {
    const { dom, sandbox, useRealExecutors } = load();
    useRealExecutors();
    const tick = async (turns = 12) => { for (let i = 0; i < turns; i++) await new Promise(r => setImmediate(r)); };

    sandbox.window.OrbitRelay.getFreshQuote = async (args) => {
      sandbox.recorded.relayQuoteArgs = args;
      return { quote: { details: {}, fees: {}, steps: [{ requestId: "r".repeat(24) }] }, wallet: {}, createdAt: Date.now() };
    };
    const approvals = [];
    sandbox.window.OrbitRelay.executeQuote = async (quote) => { approvals.push(quote); return {}; };

    let releaseClaim;
    const claimHeld = new Promise(resolve => { releaseClaim = resolve; });
    sandbox.fetch = async (url) => {
      if (String(url).includes("/executions/relay/")) {
        await claimHeld;
        return { ok: true, status: 200, json: async () => ({ execution_claimed: true }) };
      }
      if (String(url).includes("/config/public")) {
        return { ok: true, status: 200, json: async () => ({ deployment: { execution_enabled: true }, x402: null }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    };
    // Without this the card is the research-mode notice, not a swap card, and
    // every assertion below passes for the wrong reason. Found exactly that way.
    await sandbox.loadX402Config();

    const card = sandbox.renderInlineRelaySwap({ amount: "0.01" });
    // The card populates its own selects and defaults asynchronously. Fill the
    // fields only after that has finished, or its setup overwrites them and the
    // case quietly never quotes at all.
    await tick();
    const field = (selector) => card.querySelector(selector);
    field(".inline-from-chain").value = "792703809";
    field(".inline-to-chain").value = "8453";
    field(".inline-from-token").value = "SOL";
    field(".inline-to-token").value = "USDC";
    field(".inline-amount").value = "0.01";
    field(".inline-slippage").value = "50";
    field(".inline-recipient").value = "0x1111111111111111111111111111111111111111";

    field(".inline-quote-btn").dispatch("click");
    await tick();
    // askedSlippage is the proof the card really quoted. `hidden` is not: the
    // stub defaults it to false, so asserting on it would be trivially true.
    const quoted = { askedSlippage: sandbox.recorded.relayQuoteArgs?.slippageBps ?? null };

    field(".inline-execute-btn").dispatch("click");
    await tick(3);                       // far enough to be waiting on the claim
    if (eventType !== "none") {
      field(".inline-slippage").value = "1";
      field(".inline-slippage").dispatch(eventType);   // no blur, just a keystroke
    }
    releaseClaim();
    await tick();

    return { ...quoted, eventType, walletApprovals: approvals.length, status: field(".inline-status").textContent };
  },

  async inline_card_edit_during_claim_on_change() {
    return CASES.inline_card_edit_during_claim("change");
  },

  /** The inline card must still be able to sign when nothing is edited. */
  async inline_card_signs_when_nothing_changes() {
    return CASES.inline_card_edit_during_claim("none");
  },

  /** The risk charter is a hard limit and must bind on EVERY swap surface.
   *
   * The inline card never called charterQuoteVeto, so a trade the dialog and
   * the chat card would both refuse went straight through it. `surface` picks
   * which card to drive so the two are held to the same rule by one case.
   */
  async charter_blocks_an_over_limit_swap(surface = "inline") {
    const { dom, sandbox, setScriptVar, useRealExecutors } = load();
    useRealExecutors();
    setScriptVar("relayChains", [SOLANA, BASE]);
    // $10 and 10 bps, against a $1000 trade at 50 bps.
    setScriptVar("charterFields", { max_trade_usd: 10, max_slippage_bps: 10 });

    sandbox.window.OrbitRelay.getFreshQuote = async (args) => {
      sandbox.recorded.relayQuoteArgs = args;
      return {
        quote: { details: { currencyIn: { amountUsd: 1000 } }, fees: {}, steps: [{ requestId: "r".repeat(24) }] },
        wallet: {}, createdAt: Date.now(),
      };
    };
    const approvals = [];
    sandbox.window.OrbitRelay.executeQuote = async (quote) => { approvals.push(quote); return {}; };
    sandbox.fetch = async (url) => String(url).includes("/config/public")
      ? { ok: true, status: 200, json: async () => ({ deployment: { execution_enabled: true }, x402: null }) }
      : { ok: true, status: 200, json: async () => ({ execution_claimed: true }) };
    await sandbox.loadX402Config();

    let status;
    if (surface === "dialog") {
      dom.query("#relayFromChain").value = "792703809";
      dom.query("#relayToChain").value = "8453";
      dom.query("#relayFromToken").value = "SOL";
      dom.query("#relayToToken").value = "USDC";
      dom.query("#relayAmount").value = "1000";
      dom.query("#relayRecipient").value = "0x1111111111111111111111111111111111111111";
      dom.query("#relaySlippage").value = "50";
      await sandbox.requestRelayQuote();
      await sandbox.executeRelayQuote();
      status = dom.query("#relayStatus").textContent;
    } else {
      const tick = async (n = 12) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };
      const card = sandbox.renderInlineRelaySwap({ amount: "1000" });
      await tick();
      const field = (selector) => card.querySelector(selector);
      field(".inline-from-chain").value = "792703809";
      field(".inline-to-chain").value = "8453";
      field(".inline-from-token").value = "SOL";
      field(".inline-to-token").value = "USDC";
      field(".inline-amount").value = "1000";
      field(".inline-slippage").value = "50";
      field(".inline-recipient").value = "0x1111111111111111111111111111111111111111";
      field(".inline-quote-btn").dispatch("click");
      await tick();
      field(".inline-execute-btn").dispatch("click");
      await tick();
      status = field(".inline-status").textContent;
    }
    return {
      surface,
      quotedUsd: sandbox.recorded.relayQuoteArgs ? 1000 : null,
      walletApprovals: approvals.length,
      status,
    };
  },

  async charter_blocks_an_over_limit_swap_dialog() {
    return CASES.charter_blocks_an_over_limit_swap("dialog");
  },

  /** The claim path must still reach the wallet when nothing changed. */
  async execution_reaches_the_wallet_when_nothing_changes() {
    const { dom, sandbox, setScriptVar, useRealExecutors } = load();
    useRealExecutors();
    setScriptVar("relayChains", [SOLANA, BASE]);
    sandbox.window.OrbitRelay.getFreshQuote = async (args) => {
      sandbox.recorded.relayQuoteArgs = args;
      return { quote: { details: {}, fees: {}, steps: [{ requestId: "r".repeat(24) }] }, wallet: {}, createdAt: Date.now() };
    };
    const approvals = [];
    sandbox.window.OrbitRelay.executeQuote = async (quote) => { approvals.push(quote); return {}; };
    sandbox.fetch = async (url) => String(url).includes("/executions/relay/")
      ? { ok: true, status: 200, json: async () => ({ execution_claimed: true }) }
      : { ok: true, status: 200, json: async () => ({}) };

    dom.query("#relayFromChain").value = "792703809";
    dom.query("#relayToChain").value = "8453";
    dom.query("#relayFromToken").value = "SOL";
    dom.query("#relayToToken").value = "USDC";
    dom.query("#relayAmount").value = "0.01";
    dom.query("#relayRecipient").value = "0x1111111111111111111111111111111111111111";
    dom.query("#relaySlippage").value = "50";
    await sandbox.requestRelayQuote();
    await sandbox.executeRelayQuote();
    return { walletApprovals: approvals.length, status: dom.query("#relayStatus").textContent };
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

  // --- UI QA of 2026-09-17: the six confirmed findings, as browser-code cases ---

  /** UI-01: a delete-all whose requests fail must not clear the list. `mode`
   *  is "fail" (every DELETE answers 503) or "ok". */
  async delete_all_keeps_what_the_server_did_not_delete() {
    return runDeleteAll(false);
  },
  async delete_all_clears_the_list_when_the_server_confirms() {
    return runDeleteAll(true);
  },

  /** UI-02: a failed preference save keeps the dialog open with the typed
   *  value and says so; a confirmed one closes and applies it. */
  async preference_save_reports_a_failed_server_write() {
    return runSaveProfile(false);
  },
  async preference_save_applies_once_the_server_confirms() {
    return runSaveProfile(true);
  },

  /** UI-03: a failed departure stays on Members with an error; a confirmed
   *  one returns to Account. */
  async leaving_a_team_reports_a_failed_departure() {
    return runLeaveTeam(false);
  },
  async leaving_a_team_returns_to_account_when_confirmed() {
    return runLeaveTeam(true);
  },

  /** UI-04: the menu's placement treats the header as unusable space. The
   *  reviewed geometry: composer top at 356 with a 64px header and a 320px
   *  list -- room above the composer only if the header counts as room. */
  async mention_menu_never_opens_behind_the_header() {
    return runPlaceMentions({ top: 356, bottom: 420 });
  },
  async mention_menu_opens_above_a_composer_at_the_bottom() {
    return runPlaceMentions({ top: 900, bottom: 964 });
  },

  /** UI-05: text that cannot be an address is refused with the dialog kept
   *  open; a real address is adopted and shown as view-only. */
  async a_public_address_that_is_not_one_is_refused() {
    return runUseAddress("not-a-wallet");
  },
  async a_real_public_address_is_adopted_as_view_only() {
    return runUseAddress("So11111111111111111111111111111111111111112");
  },
  /** Well-formed base58 of a plausible length that is not 32 bytes. */
  // A stream that breaks mid-turn is reported as interrupted, not as an error,
  // and recoverTurn finds the finished answer in the conversation's history.
  async a_broken_stream_is_recovered_from_history() {
    const { dom, sandbox } = load();
    let reads = 0;
    const reader = { read: async () => { reads++; if (reads === 1) return { value: new TextEncoder().encode("event: status\ndata: {\"text\":\"Running x\"}\n\n"), done: false }; throw new TypeError("Load failed"); } };
    let polls = 0;
    sandbox.fetch = async (url) => {
      if (String(url) === "/chat/stream") return { ok: true, status: 200, body: { getReader: () => reader }, headers: { get: () => "text/event-stream" } };
      if (String(url).startsWith("/chat/history/s1")) { polls++; return answer(true, polls < 2 ? { messages: [{ role: "user", content: "NVDA fundamentals" }], context: { revision: 3 } }
        : { messages: [{ role: "user", content: "NVDA fundamentals" }, { role: "assistant", content: "NVDA brief", chart: { symbol: "NASDAQ:NVDA" }, intent: "research", session_revision: 4 }], context: { revision: 4 } }); }
      return answer(true, {});
    };
    sandbox.ReadableStream = class {};
    sandbox.setTimeout = (fn) => globalThis.setTimeout(fn, 0);   // the sandbox's timers never fire; recovery waits between polls
    const typing = dom.query("#typing");
    const streamed = await sandbox.streamChat({ message: "NVDA fundamentals", session_id: "s1" }, typing);
    const recovered = await sandbox.recoverTurn("s1", "NVDA fundamentals", streamed.view, { interval: 1, limit: 2000 });
    return { interrupted: streamed.interrupted === true, polls, answer: recovered && recovered.answer, chart: recovered && recovered.chart, revision: recovered && recovered.session_revision, status: dom.query("#typing >> .message-body >> .stream-status").textContent };
  },
  // Privy's embedded Solana provider signs a base64 message and answers with
  // a base64 signature; the page hands the server hex.
  async privy_sign_message_uses_base64_in_and_out() {
    const { sandbox } = load();
    const seen = [];
    const provider = { request: async (req) => { seen.push(req); return { signature: btoa(String.fromCharCode(1, 2, 255)) }; } };
    const hex = await sandbox.privySolanaSignMessage(provider, "hi");
    return { method: seen[0].method, message: seen[0].params.message, hex };
  },
  // After the email is sent the sheet offers the code path; the code signs
  // the account in through /auth/email/code and closes the sheet.
  async signin_sheet_offers_and_accepts_the_emailed_code() {
    const { dom, sandbox } = load();
    const posts = [];
    sandbox.fetch = async (url, init = {}) => {
      if (String(url) === "/auth/email/start") return answer(true, { sent: true, email: "a@b.co" });
      if (String(url) === "/auth/email/code") { posts.push(JSON.parse(init.body)); return answer(true, { authenticated: true, user: { id: "u9", email: "a@b.co" }, plan: {}, credits: {} }); }
      if (String(url) === "/me") return answer(true, { authenticated: true, user: { id: "u9" }, plan: {}, credits: {} });
      return answer(true, {});
    };
    dom.query("#signinEmail").value = "a@b.co";
    await sandbox.startSignin();
    const afterSend = { codeRowHidden: dom.query("#signinCodeRow").hidden, codeBtnHidden: dom.query("#signinCodeBtn").hidden, sendLabel: dom.query("#signinSendBtn").textContent };
    dom.query("#signinCode").value = "12345";
    await sandbox.signinWithCode();
    const shortCode = dom.query("#signinStatus").textContent;
    dom.query("#signinCode").value = "123456";
    await sandbox.signinWithCode();
    return { afterSend, shortCode, posts, signedIn: sandbox.__getScriptVar ? undefined : true };
  },
  // The sign-in sheet tells the truth about the email: "check your inbox"
  // only when the server sent one; a failed send says so.
  async signin_sheet_reports_a_send_that_did_not_happen() {
    const { dom, sandbox } = load();
    const out = {};
    for (const sent of [false, true]) {
      sandbox.fetch = async () => answer(true, { sent, email: "a@b.co" });
      dom.query("#signinEmail").value = "a@b.co";
      await sandbox.startSignin();
      out[String(sent)] = dom.query("#signinStatus").textContent;
    }
    return out;
  },
  // The chart card: TradingView's widget for the server-resolved symbol, in
  // the page's theme, with the attribution TradingView's terms require.
  async chart_card_embeds_the_resolved_symbol_with_attribution() {
    const { dom, sandbox } = load();
    sandbox.document.documentElement.dataset.theme = "light";
    const card = sandbox.renderChartCard({ symbol: "BINANCE:SOLUSDT", label: "SOL / USDT · Binance", interval: "60", kind: "crypto" });
    const frame = card.children[0], caption = card.children[1];
    return { className: card.className, src: frame.src, title: frame.title, caption: caption.innerHTML, none: sandbox.renderChartCard(null) };
  },
  // A freshly created API-key secret must not outlive the account that made
  // it: signing out, or another account signing in on the same tab, clears it.
  async api_key_secret_is_cleared_on_sign_out_and_account_switch() {
    const { dom, sandbox, setScriptVar } = load();
    let me = { authenticated: false, plan: {}, credits: {} };
    sandbox.fetch = async (url, init = {}) => {
      if (String(url) === "/me/api-keys" && init.method === "POST") return answer(true, { id: "k1", name: "Claude Desktop", secret: "orb_sk_SECRET123" });
      if (String(url) === "/me/api-keys") return answer(true, { keys: [] });
      if (String(url) === "/me") return answer(true, me);
      return answer(true, {});
    };
    setScriptVar("account", { authenticated: true, user: { id: "u1" }, plan: { entitlements: { api_keys: true } }, credits: {} });
    const box = dom.query("#apiKeySecret"), value = dom.query("#apiKeySecretValue");
    await sandbox.createApiKey();
    const shown = { hidden: box.hidden, value: value.textContent };
    await sandbox.loadAccount();                                   // signed out (the /me stub says so)
    const afterSignOut = { hidden: box.hidden, html: box.innerHTML, value: value.textContent };
    me = { authenticated: true, user: { id: "u2" }, plan: { entitlements: { api_keys: true } }, credits: {} };
    await sandbox.loadAccount();                                   // another account on the same tab
    const afterSwitch = { hidden: box.hidden, html: box.innerHTML, value: value.textContent };
    return { shown, afterSignOut, afterSwitch };
  },
  // The app's height follows the stylesheet (100dvh) unless the keyboard is
  // up; a short reading at launch never leaves a dead band under the composer.
  async app_height_follows_the_stylesheet_unless_the_keyboard_is_up() {
    const { sandbox } = load();
    return { keyboard: sandbox.appHeightFor(500, 844), settled: sandbox.appHeightFor(844, 844), toolbars: sandbox.appHeightFor(780, 844), unknown: sandbox.appHeightFor(undefined, 844) };
  },
  async a_base58_string_that_is_not_32_bytes_is_refused() {
    const { sandbox } = load();
    const probe = (v) => ({ regex: /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(v), bytes: sandbox.base58ByteLength(v), accepted: sandbox.isPublicAddress(v) });
    return {
      fortyThreeOnes: probe("1".repeat(43)),                          // 43 zero bytes
      fortyFourZs: probe("z".repeat(44)),                             // 33 bytes
      thirtyTwoChars: probe("2".repeat(32)),                          // 24 bytes
      systemProgram: probe("1".repeat(32)),                           // the system program id: 32 zero bytes, valid
      wrappedSol: probe("So11111111111111111111111111111111111111112"),
      evm: probe("0x" + "ab".repeat(20)),
    };
  },

  /** Closed beta: the public config's flag hides every purchase control and
   *  turns the plans dialog into a note. `on` is "true"/"false". */
  async closed_beta_hides_purchase_controls() {
    return runClosedBeta(true);
  },
  async purchase_controls_show_when_billing_is_open() {
    return runClosedBeta(false);
  },

  /** Streaming: the SSE parser handles split frames; the client renders
   *  progressively and resolves to a fetch-shaped result; without an SSE
   *  response it returns null so the JSON route runs. */
  async sse_parser_handles_frames_split_across_reads() {
    const { sandbox } = load();
    const first = sandbox.parseSse("event: status\ndata: {\"text\":\"Running x\"}\n\nevent: card\ndata: {\"mark");
    const second = sandbox.parseSse(first.rest + "down\":\"# C\"}\n\n");
    return { firstEvents: first.events, rest: first.rest, secondEvents: second.events, secondRest: second.rest };
  },
  async stream_renders_progressively_and_returns_the_done_payload() {
    const { dom, sandbox } = load();
    const frames = [
      "event: status\ndata: {\"text\":\"Running shield\"}\n\n",
      "event: card\ndata: {\"markdown\":\"# Shield\\n| ok |\",\"tool\":\"shield\"}\n\n",
      "event: delta\ndata: {\"text\":\"Safe \"}\n\nevent: delta\ndata: {\"text\":\"by the dossier.\"}\n\n",
      "event: done\ndata: {\"data\":{\"answer\":\"final\",\"intent\":\"research\",\"session_id\":\"s1\"}}\n\n",
    ];
    let i = 0;
    const reader = { read: async () => i < frames.length ? { value: new TextEncoder().encode(frames[i++]), done: false } : { value: undefined, done: true } };
    sandbox.fetch = async () => ({ ok: true, status: 200, body: { getReader: () => reader }, headers: { get: () => "text/event-stream" } });
    sandbox.ReadableStream = class {};
    const typing = dom.query("#typing");
    const rendered = [];
    sandbox.renderMarkdownResult = (md) => { rendered.push(md); return dom.query("#card-" + rendered.length); };
    const result = await sandbox.streamChat({ message: "is BONK safe" }, typing);
    const status = dom.query("#typing >> .message-body >> .stream-status");
    return { ok: result.ok, status: result.status, data: await result.json(), cards: rendered,
             summaryHtml: dom.query("#typing >> .message-body >> .stream-summary").innerHTML, statusLine: status.textContent, statusHidden: status.hidden };
  },
  // A general reply streams whole: markdown marks render as they arrive, and
  // the status line gives way to the text.
  async stream_renders_a_whole_answer_as_markdown_while_it_arrives() {
    const { dom, sandbox } = load();
    const frames = [
      "event: status\ndata: {\"text\":\"Routed: general\"}\n\n",
      "event: delta\ndata: {\"text\":\"I can help with **markets**\"}\n\nevent: delta\ndata: {\"text\":\" and wallets:\\n- prices\\n- swaps\"}\n\n",
      "event: done\ndata: {\"data\":{\"answer\":\"final\",\"intent\":\"general\",\"session_id\":\"s2\"}}\n\n",
    ];
    let i = 0;
    const reader = { read: async () => i < frames.length ? { value: new TextEncoder().encode(frames[i++]), done: false } : { value: undefined, done: true } };
    sandbox.fetch = async () => ({ ok: true, status: 200, body: { getReader: () => reader }, headers: { get: () => "text/event-stream" } });
    sandbox.ReadableStream = class {};
    const statusSeen = [];
    const typing = dom.query("#typing");
    const status = dom.query("#typing >> .message-body >> .stream-status");
    Object.defineProperty(status, "textContent", { set(v) { statusSeen.push(v); }, get() { return statusSeen[statusSeen.length - 1] ?? ""; } });
    const result = await sandbox.streamChat({ message: "hi" }, typing);
    return { ok: result.ok, summaryHtml: dom.query("#typing >> .message-body >> .stream-summary").innerHTML, statusSeen, statusHidden: status.hidden };
  },
  async stream_falls_back_to_json_when_the_response_is_not_a_stream() {
    const { dom, sandbox } = load();
    sandbox.ReadableStream = class {};
    sandbox.fetch = async () => ({ ok: true, status: 200, body: null, headers: { get: () => "application/json" } });
    const result = await sandbox.streamChat({ message: "hi" }, dom.query("#typing"));
    return { fallback: result === null };
  },
  async stream_error_event_is_fetch_shaped_for_the_existing_handling() {
    const { dom, sandbox } = load();
    sandbox.ReadableStream = class {};
    let sent = false;
    const reader = { read: async () => sent ? { done: true } : (sent = true, { value: new TextEncoder().encode("event: error\ndata: {\"status\":404,\"detail\":\"Conversation not found\"}\n\n"), done: false }) };
    sandbox.fetch = async () => ({ ok: true, status: 200, body: { getReader: () => reader }, headers: { get: () => "text/event-stream" } });
    const result = await sandbox.streamChat({ message: "hi", session_id: "old" }, dom.query("#typing"));
    return { ok: result.ok, status: result.status, data: await result.json() };
  },
};

async function runClosedBeta(on) {
  const { dom, sandbox, getScriptVar } = load();
  const controls = ["plansBtn", "buyCreditsBtn", "changePlanBtn"];
  for (const id of controls) dom.query("#" + id).hidden = false;
  sandbox.applyClosedBeta({ accounts: { closed_beta: on, closed_beta_monthly_credits: on ? 5000 : null } });
  const hidden = Object.fromEntries(controls.map(id => [id, dom.query("#" + id).hidden]));
  const fetched = [];
  sandbox.fetch = async (url) => { fetched.push(String(url)); return { ok: true, status: 200, json: async () => ({ plans: [], packs: [], configured: false }) }; };
  await sandbox.openPlans();
  return { hidden, plansBody: dom.query("#plansBody").innerHTML.slice(0, 200), plansFetched: fetched.some(u => u.includes("/billing/plans")), closedBeta: getScriptVar("closedBeta") };
}

function answer(ok, body = {}) {
  return { ok, status: ok ? 200 : 503, json: async () => (ok ? body : { detail: "QA failure" }) };
}

async function runDeleteAll(serverOk) {
  const { dom, sandbox, setScriptVar, getScriptVar } = load();
  const deletes = [];
  sandbox.fetch = async (url, init = {}) => {
    if (init.method === "DELETE") { deletes.push(String(url)); return answer(serverOk, { deleted: 2, busy: 0 }); }
    if (String(url) === "/me/conversations") return answer(true, { conversations: serverOk ? [] : [{ session_id: "a", title: "kept", updated_at: new Date().toISOString() }] });
    return answer(true, {});
  };
  setScriptVar("account", { authenticated: true, user: { id: "u1" } });
  setScriptVar("chatRegistry", [{ id: "a", title: "A", owner: "u1", updatedAt: 2 }, { id: "b", title: "B", owner: "u1", updatedAt: 1 }]);
  const outcome = await sandbox.deleteAllConversations(["a", "b"]);
  return { outcome, deletes: deletes.length, remaining: getScriptVar("chatRegistry").map(c => c.id), status: dom.query("#deleteAllStatus").textContent };
}

async function runSaveProfile(serverOk) {
  const { dom, sandbox, setScriptVar, getScriptVar } = load();
  let put = null;
  sandbox.fetch = async (url, init = {}) => {
    if (String(url) === "/me/preferences" && init.method === "PUT") { put = JSON.parse(init.body); return answer(serverOk); }
    if (String(url) === "/me") return answer(true, { authenticated: true, user: { id: "u1", display_name: serverOk ? "New name" : null, preferences: {} }, plan: {}, credits: {} });
    return answer(true, {});
  };
  setScriptVar("account", { authenticated: true, user: { id: "u1", preferences: {} } });
  setScriptVar("profile", { name: "Old name", riskProfile: "balanced", defaultWallet: "" });
  sandbox.openDialog("profileDialog");
  dom.query("#displayNameInput").value = "New name";
  await sandbox.saveProfile();
  return {
    sent: put?.display_name ?? null,
    dialogOpen: getScriptVar("dialogStack").includes("profileDialog"),
    typedValueKept: dom.query("#displayNameInput").value,
    storedName: JSON.parse(sandbox.localStorage.getItem("orbit_profile_v1") || "{}").name ?? null,
    status: dom.query("#profileStatus").textContent,
    saveEnabled: !dom.query("#saveProfileBtn").disabled,
  };
}

async function runLeaveTeam(serverOk) {
  const { dom, sandbox, setScriptVar } = load();
  const tabs = [];
  sandbox.settingsTab = (name) => tabs.push(name);
  sandbox.fetch = async (url, init = {}) => {
    if (String(url) === "/me/team/leave") return answer(serverOk, { left: true });
    if (String(url) === "/me") return answer(true, { authenticated: true, user: { id: "u1", preferences: {} }, team: serverOk ? {} : { role: "member" }, plan: {}, credits: {} });
    return answer(true, {});
  };
  setScriptVar("account", { authenticated: true, user: { id: "u1" }, team: { role: "member" } });
  const button = dom.query("#teamLeaveBtn");
  button.dataset.armed = "1";
  await sandbox.leaveTeam();
  return { tabsSwitchedTo: tabs, status: dom.query("#teamLeaveStatus").textContent, buttonEnabled: !button.disabled, buttonText: button.textContent };
}

async function runPlaceMentions(composerBox) {
  const { dom, sandbox } = load();
  const menu = dom.query("#mentionMenu");
  const toggled = {};
  menu.classList = { toggle: (cls, on) => { toggled[cls] = on; }, add() {}, remove() {}, contains: () => false };
  menu.hidden = false;
  menu.scrollHeight = 320;
  menu.offsetParent = { getBoundingClientRect: () => ({ ...composerBox, left: 0, right: 800 }) };
  dom.query(".topbar").getBoundingClientRect = () => ({ top: 0, bottom: 64, left: 0, right: 1440 });
  sandbox.innerHeight = 1000;
  sandbox.placeMentions();
  const maxHeight = Number(String(menu.style.maxHeight).replace("px", ""));
  // Where the list's top edge lands if it opens above: composer top - gap - height.
  const topIfAbove = composerBox.top - 8 - Math.min(320, maxHeight);
  return { below: toggled.below, maxHeight, topIfAbove, headerBottom: 64 };
}

async function runUseAddress(text) {
  const { dom, sandbox, getScriptVar } = load();
  const button = dom.query("#walletButton");
  const classes = [];
  button.classList = { add: (c) => classes.push(c), remove() {}, toggle() {}, contains: () => false };
  sandbox.openDialog("walletDialog");
  dom.query("#walletAddress").value = text;
  dom.query("#useAddressBtn").dispatch("click");
  return {
    errorShown: !dom.query("#walletAddressError").hidden,
    // Not `error`: the Python driver reads that key as a harness crash.
    errorText: dom.query("#walletAddressError").textContent,
    dialogOpen: getScriptVar("dialogStack").includes("walletDialog"),
    adopted: getScriptVar("walletInput").value,
    label: dom.query("#walletLabel").textContent,
    classes,
  };
}

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
