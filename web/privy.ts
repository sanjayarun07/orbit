import Privy, {
  LocalStorage,
  getEntropyDetailsFromUser,
  getUserEmbeddedEthereumWallet,
  getUserEmbeddedSolanaWallet,
  type EIP1193Provider,
  type PrivyEmbeddedSolanaWalletProvider
} from "@privy-io/js-sdk-core";
import {Connection, VersionedTransaction} from "@solana/web3.js";

type PublicConfig = {
  privy: {
    enabled: boolean;
    app_id: string | null;
    client_id: string | null;
    delegated_signing: boolean;
  };
};

type WalletState = {
  configured: boolean;
  authenticated: boolean;
  userId?: string;
  evmAddress?: string;
  solanaAddress?: string;
};

let client: Privy | null = null;
type PrivyUser = Awaited<ReturnType<Privy["user"]["get"]>>["user"];

let currentUser: PrivyUser | null = null;
let secureFrame: HTMLIFrameElement | null = null;
let evmProvider: EIP1193Provider | null = null;
let solanaProvider: PrivyEmbeddedSolanaWalletProvider | null = null;
let initialization: Promise<WalletState> | null = null;

function walletState(): WalletState {
  const evm = getUserEmbeddedEthereumWallet(currentUser);
  const solana = getUserEmbeddedSolanaWallet(currentUser);
  return {
    configured: Boolean(client),
    authenticated: Boolean(currentUser),
    userId: currentUser?.id,
    evmAddress: evm?.address,
    solanaAddress: solana?.address
  };
}

function emitState() {
  window.dispatchEvent(new CustomEvent("orbit:privy-state", { detail: walletState() }));
}

function mountSecureContext(privy: Privy) {
  if (secureFrame) return;
  secureFrame = document.createElement("iframe");
  secureFrame.src = privy.embeddedWallet.getURL();
  secureFrame.hidden = true;
  secureFrame.title = "Privy secure wallet context";
  document.body.appendChild(secureFrame);
  privy.setMessagePoster(secureFrame.contentWindow! as unknown as Parameters<Privy["setMessagePoster"]>[0]);
  window.addEventListener("message", (event) => {
    if (event.source !== secureFrame?.contentWindow) return;
    try {
      const data = typeof event.data === "string" ? JSON.parse(event.data) : event.data;
      privy.embeddedWallet.onMessage(data);
    } catch {
      // Ignore unrelated or malformed frame messages.
    }
  });
}

async function loadProviders() {
  evmProvider = null;
  solanaProvider = null;
  if (!client || !currentUser) return;
  const entropy = getEntropyDetailsFromUser(currentUser);
  if (!entropy) throw new Error("Privy wallet entropy is unavailable for this session.");
  const evm = getUserEmbeddedEthereumWallet(currentUser);
  const solana = getUserEmbeddedSolanaWallet(currentUser);
  if (evm) {
    evmProvider = await client.embeddedWallet.getEthereumProvider({
      wallet: evm,
      entropyId: entropy.entropyId,
      entropyIdVerifier: entropy.entropyIdVerifier
    });
  }
  if (solana) {
    solanaProvider = await client.embeddedWallet.getSolanaProvider(
      solana,
      entropy.entropyId,
      entropy.entropyIdVerifier
    );
  }
}

async function ensureWallets() {
  if (!client || !currentUser) throw new Error("Sign in with Privy first.");
  let evm = getUserEmbeddedEthereumWallet(currentUser);
  let solana = getUserEmbeddedSolanaWallet(currentUser);
  if (!evm) {
    currentUser = (await client.embeddedWallet.create({solanaAccount: solana || undefined})).user;
    evm = getUserEmbeddedEthereumWallet(currentUser);
  }
  solana = getUserEmbeddedSolanaWallet(currentUser);
  if (!solana) {
    currentUser = (await client.embeddedWallet.createSolana({ethereumAccount: evm || undefined})).user;
  }
  await loadProviders();
  emitState();
  return walletState();
}

async function initialize() {
  if (initialization) return initialization;
  initialization = (async () => {
    const response = await fetch("/config/public", {headers: {Accept: "application/json"}});
    if (!response.ok) throw new Error("Wallet options could not load. Please try again.");
    const config = await response.json() as PublicConfig;
    if (!config.privy.enabled || !config.privy.app_id || !config.privy.client_id) {
      emitState();
      return walletState();
    }
    client = new Privy({
      appId: config.privy.app_id,
      clientId: config.privy.client_id,
      storage: new LocalStorage()
    });
    await client.initialize();
    mountSecureContext(client);
    try {
      ({user: currentUser} = await client.user.get());
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (!message.toLowerCase().includes("no tokens found in storage")) throw error;
      currentUser = null;
    }
    if (currentUser) await loadProviders();
    emitState();
    return walletState();
  })().catch((error) => {
    initialization = null;
    throw error;
  });
  return initialization;
}

async function sendEmailCode(email: string) {
  await initialize();
  if (!client) throw new Error("Email wallets are currently unavailable. Choose another wallet to continue.");
  const normalized = email.trim().toLowerCase();
  if (!/^\S+@\S+\.\S+$/.test(normalized)) throw new Error("Enter a valid email address.");
  await client.auth.email.sendCode(normalized);
}

async function verifyEmailCode(email: string, code: string) {
  await initialize();
  if (!client) throw new Error("Email wallets are currently unavailable. Choose another wallet to continue.");
  currentUser = (await client.auth.email.loginWithCode(email.trim().toLowerCase(), code.trim())).user;
  return ensureWallets();
}

async function logout() {
  if (client && currentUser) await client.auth.logout({userId: currentUser.id});
  currentUser = null;
  evmProvider = null;
  solanaProvider = null;
  emitState();
}

async function getEvmWallet() {
  await initialize();
  if (!evmProvider) await loadProviders();
  const address = getUserEmbeddedEthereumWallet(currentUser)?.address;
  if (!evmProvider || !address) throw new Error("No Privy Ethereum wallet is connected.");
  return {provider: evmProvider, address};
}

async function getSolanaWallet() {
  await initialize();
  if (!solanaProvider) await loadProviders();
  const address = getUserEmbeddedSolanaWallet(currentUser)?.address;
  if (!solanaProvider || !address) throw new Error("No Privy Solana wallet is connected.");
  return {provider: solanaProvider, address};
}

function decodeBase64(value: string): Uint8Array {
  const binary = atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function encodeBase64(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function deserializeSolanaTransaction(value: string) {
  return VersionedTransaction.deserialize(decodeBase64(value));
}

function serializeSolanaTransaction(transaction: VersionedTransaction) {
  return encodeBase64(transaction.serialize());
}

async function signSolanaTransaction(value: string) {
  const {provider} = await getSolanaWallet();
  const transaction = deserializeSolanaTransaction(value);
  const result = await provider.request({method: "signTransaction", params: {transaction}});
  return serializeSolanaTransaction(result.signedTransaction as VersionedTransaction);
}

async function signAndSendSolanaTransaction(value: string) {
  const {provider} = await getSolanaWallet();
  const transaction = deserializeSolanaTransaction(value);
  const connection = new Connection(`${window.location.origin}/rpc/solana`, "confirmed");
  const result = await provider.request({
    method: "signAndSendTransaction",
    params: {transaction, connection, options: {skipPreflight: false, maxRetries: 2}}
  });
  return result.signature;
}

const bridge = {
  initialize,
  getState: walletState,
  sendEmailCode,
  verifyEmailCode,
  logout,
  getEvmWallet,
  getSolanaWallet,
  deserializeSolanaTransaction,
  serializeSolanaTransaction,
  signSolanaTransaction,
  signAndSendSolanaTransaction
};

declare global {
  interface Window {
    OrbitPrivy: typeof bridge;
  }
}

window.OrbitPrivy = bridge;
window.dispatchEvent(new CustomEvent("orbit:privy-ready"));
void initialize().catch((error) => {
  window.dispatchEvent(new CustomEvent("orbit:privy-error", {detail: {message: error instanceof Error ? error.message : String(error)}}));
});
