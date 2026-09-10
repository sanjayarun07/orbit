import {createCoinbaseWalletSDK, type ProviderInterface} from "@coinbase/wallet-sdk";
import type {EIP1193Provider} from "viem";

type State = {configured: true; connected: boolean; authenticated: boolean; address?: string; chainId?: number};

const sdk = createCoinbaseWalletSDK({
  appName: "Orbit",
  appChainIds: [1, 8453, 42161, 10, 137, 43114, 56],
  preference: {options: "all", attribution: {auto: true}},
});
const provider = sdk.getProvider();
let address: string | undefined;
let chainId: number | undefined;
let authenticated = false;

function state(): State {
  return {configured: true, connected: Boolean(address), authenticated, address, chainId};
}

function emit() {
  window.dispatchEvent(new CustomEvent("orbit:coinbase-state", {detail: state()}));
}

function errorMessage(payload: unknown, fallback: string): string {
  if (typeof payload === "string" && payload.trim()) return payload;
  if (Array.isArray(payload)) {
    const messages = payload.map((item) => errorMessage(item, "")).filter(Boolean);
    if (messages.length) return messages.join(" ");
  }
  if (payload && typeof payload === "object") {
    const value = payload as {detail?: unknown; message?: unknown; error?: unknown; msg?: unknown};
    for (const candidate of [value.detail, value.message, value.error, value.msg]) {
      const message = errorMessage(candidate, "");
      if (message) return message;
    }
  }
  return fallback;
}

function signatureHex(value: unknown): string {
  if (typeof value === "string" && value.startsWith("0x")) return value;
  if (value && typeof value === "object") {
    const nested = (value as {signature?: unknown; result?: unknown}).signature
      ?? (value as {signature?: unknown; result?: unknown}).result;
    if (typeof nested === "string" && nested.startsWith("0x")) return nested;
  }
  throw new Error(errorMessage(value, "Coinbase Wallet returned an unsupported signature format."));
}

async function refresh(): Promise<State> {
  const accounts = await provider.request({method: "eth_accounts"}) as string[];
  address = accounts?.[0];
  if (address) {
    const current = await provider.request({method: "eth_chainId"}) as string;
    chainId = Number.parseInt(current, 16);
  } else {
    chainId = undefined;
  }
  const response = await fetch("/auth/session", {headers: {Accept: "application/json"}});
  const session = response.ok ? await response.json() as {authenticated?: boolean; address?: string} : {};
  authenticated = Boolean(address && session.authenticated && session.address?.toLowerCase() === address.toLowerCase());
  emit();
  return state();
}

async function authenticate(account: string, activeChainId: number): Promise<void> {
  const challengeResponse = await fetch("/auth/coinbase/challenge", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({address: account, chain_id: activeChainId}),
  });
  const challenge = await challengeResponse.json() as {nonce?: string; message?: string; detail?: unknown};
  if (!challengeResponse.ok || !challenge.nonce || !challenge.message) {
    throw new Error(errorMessage(challenge, "Could not create a Coinbase Wallet login challenge."));
  }
  const signature = signatureHex(
    await provider.request({method: "personal_sign", params: [challenge.message, account]}),
  );
  const verifyResponse = await fetch("/auth/coinbase/verify", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({address: account, nonce: challenge.nonce, signature}),
  });
  const verified = await verifyResponse.json() as {authenticated?: boolean; detail?: unknown};
  if (!verifyResponse.ok || !verified.authenticated) {
    throw new Error(errorMessage(verified, "Coinbase Wallet login verification failed."));
  }
  authenticated = true;
}

async function connect(): Promise<State> {
  const accounts = await provider.request({method: "eth_requestAccounts"}) as string[];
  if (!accounts?.[0]) throw new Error("Coinbase Wallet did not return an account.");
  address = accounts[0];
  const current = await provider.request({method: "eth_chainId"}) as string;
  chainId = Number.parseInt(current, 16);
  await authenticate(address, chainId);
  emit();
  return state();
}

async function disconnect(): Promise<void> {
  await fetch("/auth/logout", {method: "POST"}).catch(() => undefined);
  await provider.disconnect();
  address = undefined;
  chainId = undefined;
  authenticated = false;
  emit();
}

async function getEvmWallet(): Promise<{provider: EIP1193Provider; address: string}> {
  if (!address) await refresh();
  if (!address) throw new Error("Coinbase Wallet is not connected.");
  return {provider: provider as unknown as EIP1193Provider, address};
}

provider.on("accountsChanged", (accounts) => {
  if (address && accounts?.[0]?.toLowerCase() !== address.toLowerCase()) {
    authenticated = false;
    void fetch("/auth/logout", {method: "POST"});
  }
  address = accounts?.[0];
  emit();
});
provider.on("chainChanged", (value) => {
  chainId = Number.parseInt(value, 16);
  emit();
});
provider.on("disconnect", () => {
  address = undefined;
  chainId = undefined;
  authenticated = false;
  emit();
});

const bridge = {initialize: refresh, connect, disconnect, getState: state, getEvmWallet};
declare global { interface Window { OrbitCoinbase: typeof bridge; } }
window.OrbitCoinbase = bridge;
window.dispatchEvent(new CustomEvent("orbit:coinbase-ready"));
void refresh().catch(() => emit());
