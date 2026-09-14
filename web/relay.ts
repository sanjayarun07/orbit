import {
  MAINNET_RELAY_API,
  adaptViemWallet,
  createClient,
  type AdaptedWallet,
  type Execute,
  type ProgressData,
  type RelayChain
} from "@relayprotocol/relay-sdk";
import { configureDynamicChains } from "@relayprotocol/relay-sdk/chain-utils";
import { adaptSolanaWallet } from "@relayprotocol/relay-svm-wallet-adapter";
import { Connection } from "@solana/web3.js";
import {
  createPublicClient,
  createWalletClient,
  custom,
  formatUnits,
  http,
  parseUnits,
  type Address,
  type EIP1193Provider
} from "viem";

const SOLANA_CHAIN_ID = 792703809;
const SOLANA_RPC_PROXY = `${window.location.origin}/rpc/solana`;
const EVM_NATIVE = "0x0000000000000000000000000000000000000000";
const SOLANA_NATIVE = "11111111111111111111111111111111";
const ERC20_DECIMALS_ABI = [{
  type: "function",
  name: "decimals",
  stateMutability: "view",
  inputs: [],
  outputs: [{ type: "uint8" }]
}] as const;
const ERC20_BALANCE_ABI = [{
  type: "function",
  name: "balanceOf",
  stateMutability: "view",
  inputs: [{ name: "account", type: "address" }],
  outputs: [{ type: "uint256" }]
}] as const;

type PhantomProvider = {
  publicKey?: { toString(): string };
  connect(options?: { onlyIfTrusted?: boolean }): Promise<{ publicKey: { toString(): string } }>;
  signAndSendTransaction(transaction: unknown): Promise<{ signature: string }>;
};

type ConnectedSolanaWallet = {
  address: string;
  signAndSendTransaction(transaction: unknown): Promise<{ signature: string }>;
};

declare global {
  interface Window {
    solana?: PhantomProvider & { isPhantom?: boolean };
    OrbitWalletContext?: { provider: "privy" | "reown" | "coinbase" | "phantom" | "metamask" | null };
    OrbitSolanaWallet?: ConnectedSolanaWallet;
    OrbitRelay: typeof relayBridge;
  }
}

let chainsPromise: Promise<RelayChain[]> | null = null;
let relayClient: ReturnType<typeof createClient> | null = null;

function injectedEvmProvider(): EIP1193Provider | undefined {
  return (window as unknown as {ethereum?: EIP1193Provider}).ethereum;
}

async function initialize() {
  if (!chainsPromise) {
    const hostname = window.location.hostname;
    const isLocalDevelopment = hostname === "127.0.0.1" || hostname === "localhost";
    relayClient = createClient({
      baseApiUrl: MAINNET_RELAY_API,
      ...(isLocalDevelopment ? {} : { source: hostname })
    });
    // The SDK derives a browser hostname when source is omitted. Relay rejects
    // localhost/127.0.0.1 as an unregistered referrer, so remove it in local
    // development. Production domains should be registered with Relay.
    if (isLocalDevelopment) relayClient.source = undefined;
    chainsPromise = configureDynamicChains().then((chains) => {
      if (relayClient) relayClient.chains = chains;
      return chains;
    });
  }
  return chainsPromise;
}

async function supportedChains() {
  const chains = await initialize();
  return chains
    .filter((chain) => chain.depositEnabled !== false && (chain.vmType === "evm" || chain.vmType === "svm"))
    .map((chain) => ({
      id: chain.id,
      name: chain.displayName || chain.name,
      vmType: chain.vmType,
      nativeCurrency: chain.currency?.address || (chain.vmType === "svm" ? SOLANA_NATIVE : EVM_NATIVE),
      nativeSymbol: chain.currency?.symbol || "Native",
      nativeDecimals: chain.currency?.decimals ?? (chain.vmType === "svm" ? 9 : 18),
      explorerUrl: chain.explorerUrl || chain.viemChain?.blockExplorers?.default?.url || "",
      currencies: [chain.currency, ...(chain.featuredTokens || []), ...(chain.erc20Currencies || []), ...(chain.solverCurrencies || [])]
        .filter((token): token is NonNullable<typeof token> => Boolean(token?.address))
        .map((token) => ({ address: token.address!, symbol: token.symbol || "", name: token.name || "", decimals: token.decimals }))
        .filter((token, index, items) => items.findIndex((item) => item.address.toLowerCase() === token.address.toLowerCase()) === index)
    }))
    .sort((a, b) => {
      if (a.id === SOLANA_CHAIN_ID) return -1;
      if (b.id === SOLANA_CHAIN_ID) return 1;
      return a.name.localeCompare(b.name);
    });
}

async function resolveCurrency(chainId: number, query: string) {
  const chain = (await supportedChains()).find((item) => item.id === chainId);
  if (!chain) throw new Error("That chain is not currently supported by Relay.");
  const value = query.trim();
  if (/^0x[0-9a-fA-F]{40}$/.test(value) || /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(value)) return value;
  const candidates = chain.currencies.filter((token) => token.symbol.toLowerCase() === value.toLowerCase());
  if (candidates.length === 1) return candidates[0].address;
  if (candidates.length > 1) {
    throw new Error(`${value} is ambiguous on ${chain.name}. Paste the exact contract: ${candidates.map((item) => item.address).join(", ")}`);
  }
  throw new Error(`${value} was not found in Relay's ${chain.name} currency list. Paste its exact contract or mint.`);
}

async function chainById(chainId: number) {
  const chains = await initialize();
  const chain = chains.find((item) => item.id === chainId);
  if (!chain) throw new Error("That chain is not currently supported by Relay.");
  return chain;
}

async function evmWallet(chain: RelayChain): Promise<{ wallet: AdaptedWallet; address: string }> {
  const privyWallet = window.OrbitWalletContext?.provider === "privy"
    ? await window.OrbitPrivy?.getEvmWallet()
    : undefined;
  const reownWallet = window.OrbitWalletContext?.provider === "reown"
    ? await window.OrbitReown?.getEvmWallet()
    : undefined;
  const coinbaseWallet = window.OrbitWalletContext?.provider === "coinbase"
    ? await window.OrbitCoinbase?.getEvmWallet()
    : undefined;
  const provider = privyWallet?.provider || reownWallet?.provider || coinbaseWallet?.provider || injectedEvmProvider();
  if (!provider) throw new Error("Connect Coinbase Wallet, Privy, MetaMask, or another EVM wallet for this source chain.");
  if (!chain.viemChain) throw new Error(`${chain.displayName} does not have an EVM wallet configuration.`);
  const accounts = privyWallet ? [privyWallet.address] : reownWallet ? [reownWallet.address] : coinbaseWallet ? [coinbaseWallet.address] : await provider.request({ method: "eth_requestAccounts" }) as string[];
  const address = accounts?.[0] as Address | undefined;
  if (!address) throw new Error("The EVM wallet did not return an account.");

  const currentHex = await provider.request({ method: "eth_chainId" }) as string;
  if (Number.parseInt(currentHex, 16) !== chain.id) {
    try {
      await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: `0x${chain.id.toString(16)}` }] });
    } catch (error: unknown) {
      const code = typeof error === "object" && error && "code" in error ? Number((error as { code: unknown }).code) : 0;
      if (code !== 4902) throw error;
      await provider.request({
        method: "wallet_addEthereumChain",
        params: [{
          chainId: `0x${chain.id.toString(16)}`,
          chainName: chain.displayName,
          nativeCurrency: chain.viemChain.nativeCurrency,
          rpcUrls: chain.viemChain.rpcUrls.default.http,
          blockExplorerUrls: chain.viemChain.blockExplorers?.default?.url ? [chain.viemChain.blockExplorers.default.url] : undefined
        }]
      });
    }
  }

  const client = createWalletClient({ account: address, chain: chain.viemChain, transport: custom(provider) });
  return { wallet: adaptViemWallet(client), address };
}

async function solanaWallet(chain: RelayChain): Promise<{ wallet: AdaptedWallet; address: string }> {
  if (window.OrbitWalletContext?.provider === "privy") {
    const privy = await window.OrbitPrivy?.getSolanaWallet();
    if (!privy) throw new Error("No Privy Solana wallet is connected.");
    const connection = new Connection(SOLANA_RPC_PROXY, "confirmed");
    return {
      address: privy.address,
      wallet: adaptSolanaWallet(
        privy.address,
        chain.id,
        connection,
        async (transaction) => privy.provider.request({
          method: "signAndSendTransaction",
          params: {transaction, connection}
        })
      )
    };
  }
  const connectedWallet = window.OrbitSolanaWallet;
  if (connectedWallet?.address) {
    const connection = new Connection(SOLANA_RPC_PROXY, "confirmed");
    return {
      address: connectedWallet.address,
      wallet: adaptSolanaWallet(
        connectedWallet.address,
        chain.id,
        connection,
        async (transaction) => connectedWallet.signAndSendTransaction(transaction)
      )
    };
  }
  const provider = window.solana?.isPhantom ? window.solana : undefined;
  if (!provider) throw new Error("Connect Privy, MetaMask, or Phantom to quote a Solana source swap.");
  const connected = provider.publicKey ? { publicKey: provider.publicKey } : await provider.connect();
  const address = connected.publicKey.toString();
  const connection = new Connection(SOLANA_RPC_PROXY, "confirmed");
  return {
    address,
    wallet: adaptSolanaWallet(
      address,
      chain.id,
      connection,
      async (transaction) => provider.signAndSendTransaction(transaction)
    )
  };
}

async function walletFor(chain: RelayChain) {
  return chain.vmType === "svm" ? solanaWallet(chain) : evmWallet(chain);
}

async function recipientFor(chainId: number) {
  const chain = await chainById(chainId);
  if (chain.vmType === "svm") {
    if (window.OrbitWalletContext?.provider === "privy") {
      const wallet = await window.OrbitPrivy?.getSolanaWallet();
      if (wallet?.address) return wallet.address;
    }
    if (window.OrbitSolanaWallet?.address) return window.OrbitSolanaWallet.address;
    const provider = window.solana?.isPhantom ? window.solana : undefined;
    if (!provider) throw new Error("Connect a Solana wallet or include a Solana destination address.");
    const connected = provider.publicKey ? { publicKey: provider.publicKey } : await provider.connect();
    return connected.publicKey.toString();
  }
  if (window.OrbitWalletContext?.provider === "privy") {
    const wallet = await window.OrbitPrivy?.getEvmWallet();
    if (wallet?.address) return wallet.address;
  }
  if (window.OrbitWalletContext?.provider === "reown") {
    const wallet = await window.OrbitReown?.getEvmWallet();
    if (wallet?.address) return wallet.address;
  }
  if (window.OrbitWalletContext?.provider === "coinbase") {
    const wallet = await window.OrbitCoinbase?.getEvmWallet();
    if (wallet?.address) return wallet.address;
  }
  const accounts = await injectedEvmProvider()?.request({ method: "eth_requestAccounts" }) as string[] | undefined;
  if (!accounts?.[0]) throw new Error("Connect an EVM wallet or include an EVM destination address.");
  return accounts[0];
}

async function currencyDecimals(chain: RelayChain, currency: string) {
  if (currency.toLowerCase() === (chain.currency?.address || "").toLowerCase()) return chain.currency?.decimals ?? 18;
  if (chain.vmType === "svm") {
    const connection = new Connection(SOLANA_RPC_PROXY, "confirmed");
    const parsed = await connection.getParsedAccountInfo(new (await import("@solana/web3.js")).PublicKey(currency));
    const decimals = (parsed.value?.data as { parsed?: { info?: { decimals?: number } } })?.parsed?.info?.decimals;
    if (typeof decimals !== "number") throw new Error("Could not read this Solana mint's decimals.");
    return decimals;
  }
  if (!chain.viemChain) throw new Error("Missing EVM chain configuration.");
  const publicClient = createPublicClient({ chain: chain.viemChain, transport: http(chain.httpRpcUrl) });
  return Number(await publicClient.readContract({ address: currency as Address, abi: ERC20_DECIMALS_ABI, functionName: "decimals" }));
}

async function sourceBalance(chain: RelayChain, currency: string, address: string): Promise<bigint> {
  if (chain.vmType === "svm") {
    const { PublicKey } = await import("@solana/web3.js");
    const connection = new Connection(SOLANA_RPC_PROXY, "confirmed");
    if (currency.toLowerCase() === SOLANA_NATIVE.toLowerCase()) {
      return BigInt(await connection.getBalance(new PublicKey(address)));
    }
    const accounts = await connection.getParsedTokenAccountsByOwner(new PublicKey(address), { mint: new PublicKey(currency) });
    const raw = accounts.value[0]?.account.data.parsed?.info?.tokenAmount?.amount;
    return raw ? BigInt(raw) : 0n;
  }
  if (!chain.viemChain) throw new Error("Missing EVM chain configuration.");
  const publicClient = createPublicClient({ chain: chain.viemChain, transport: http(chain.httpRpcUrl) });
  if (currency.toLowerCase() === EVM_NATIVE) {
    return publicClient.getBalance({ address: address as Address });
  }
  return publicClient.readContract({ address: currency as Address, abi: ERC20_BALANCE_ABI, functionName: "balanceOf", args: [address as Address] });
}

async function symbolFor(chainId: number, currency: string) {
  const chain = (await supportedChains()).find((item) => item.id === chainId);
  const match = chain?.currencies.find((token) => token.address.toLowerCase() === currency.toLowerCase());
  return match?.symbol || `${currency.slice(0, 6)}…${currency.slice(-4)}`;
}

type QuoteInput = {
  chainId: number;
  toChainId: number;
  currency: string;
  toCurrency: string;
  amount: string;
  recipient?: string;
  slippageBps?: number;
};

async function getFreshQuote(input: QuoteInput) {
  await initialize();
  if (!relayClient) throw new Error("Relay client is unavailable.");
  const sourceChain = await chainById(input.chainId);
  const { wallet, address } = await walletFor(sourceChain);
  const decimals = await currencyDecimals(sourceChain, input.currency);
  const amount = parseUnits(input.amount, decimals).toString();
  // Relay's quote API prices a hypothetical swap regardless of whether the
  // connected wallet actually holds the funds -- it will happily return a
  // route for a wallet with a zero balance, and the shortfall only surfaces
  // when the wallet is asked to sign. Checking the balance here fails fast,
  // before the user is walked through a quote card and a sign prompt that
  // was always going to be rejected.
  try {
    const available = await sourceBalance(sourceChain, input.currency, address);
    const required = BigInt(amount);
    if (available < required) {
      const symbol = await symbolFor(input.chainId, input.currency);
      const have = formatUnits(available, decimals);
      const chainName = sourceChain.displayName || sourceChain.name;
      throw new Error(
        `Insufficient balance: this wallet holds ${have} ${symbol} on ${chainName}, but the swap needs `
        + `${input.amount} ${symbol}. Fund the wallet or reduce the amount, then request a fresh quote.`
      );
    }
  } catch (error) {
    // A balance-read failure (unsupported RPC method, flaky endpoint, etc.)
    // should not block quoting -- only a confirmed, read shortfall should.
    // Re-throw only the insufficient-balance error we raised above.
    if (error instanceof Error && error.message.startsWith("Insufficient balance:")) throw error;
  }
  const quote = await relayClient.actions.getQuote({
    chainId: input.chainId,
    toChainId: input.toChainId,
    currency: input.currency,
    toCurrency: input.toCurrency,
    amount,
    tradeType: "EXACT_INPUT",
    user: address,
    recipient: input.recipient || address,
    options: input.slippageBps == null ? undefined : {slippageTolerance: String(input.slippageBps)},
    wallet
  });
  return { quote, wallet, address, createdAt: Date.now() };
}

async function executeQuote(quote: Execute, wallet: AdaptedWallet, onProgress: (progress: ProgressData) => void) {
  await initialize();
  if (!relayClient) throw new Error("Relay client is unavailable.");
  return relayClient.actions.execute({ quote, wallet, onProgress });
}

const relayBridge = {
  supportedChains,
  resolveCurrency,
  recipientFor,
  getFreshQuote,
  executeQuote,
  constants: { SOLANA_CHAIN_ID, EVM_NATIVE, SOLANA_NATIVE }
};

window.OrbitRelay = relayBridge;
window.dispatchEvent(new CustomEvent("orbit-relay-ready"));
