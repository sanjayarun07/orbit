// x402 payments for the chat API (protocol v2). When the server answers
// POST /chat with 402, this signs an EIP-3009 USDC authorization with the
// connected EVM wallet and retries with the payment header. Nothing is sent
// on-chain by the browser; the facilitator settles the signed authorization.
import { createPublicClient, createWalletClient, custom, http, type EIP1193Provider, type Address } from "viem";
import { base, baseSepolia } from "viem/chains";
import { wrapFetchWithPayment, x402Client } from "@x402/fetch";
import { ExactEvmScheme, toClientEvmSigner } from "@x402/evm";

type EvmWallet = { provider: EIP1193Provider; address: string };
type WalletBridge = { getEvmWallet: () => Promise<EvmWallet> };

// The wallet bridges declare their own window globals in their bundles; read
// them through a narrow view here so the whole web/ tree type-checks as one.
const bridges = window as unknown as {
  OrbitWalletContext?: { provider: "privy" | "reown" | "coinbase" | "phantom" | "metamask" | null };
  OrbitPrivy?: WalletBridge;
  OrbitReown?: WalletBridge;
  OrbitCoinbase?: WalletBridge;
  ethereum?: EIP1193Provider;
  OrbitX402?: {
    fetchWithPayment: (network: string, input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
    hasEvmWallet: () => Promise<boolean>;
  };
};

const CHAINS = { 8453: base, 84532: baseSepolia } as const;

async function evmWallet(): Promise<EvmWallet> {
  const which = bridges.OrbitWalletContext?.provider;
  if (which === "privy" && bridges.OrbitPrivy) return bridges.OrbitPrivy.getEvmWallet();
  if (which === "reown" && bridges.OrbitReown) return bridges.OrbitReown.getEvmWallet();
  if (which === "coinbase" && bridges.OrbitCoinbase) return bridges.OrbitCoinbase.getEvmWallet();
  const injected = bridges.ethereum;
  if (!injected) throw new Error("Connect Coinbase Wallet, Privy, or WalletConnect (an EVM wallet) to pay for messages.");
  const accounts = (await injected.request({ method: "eth_requestAccounts" })) as string[];
  if (!accounts?.[0]) throw new Error("The EVM wallet did not return an account.");
  return { provider: injected, address: accounts[0] };
}

function chainFor(network: string) {
  const id = Number(network.split(":")[1]);
  const chain = CHAINS[id as keyof typeof CHAINS];
  if (!chain) throw new Error(`Unsupported x402 network ${network}; expected Base (eip155:8453) or Base Sepolia (eip155:84532).`);
  return chain;
}

async function ensureChain(provider: EIP1193Provider, chainId: number) {
  const hex = `0x${chainId.toString(16)}`;
  const current = (await provider.request({ method: "eth_chainId" })) as string;
  if (current?.toLowerCase() === hex) return;
  // Wallets refuse EIP-712 signatures whose domain chainId differs from the
  // active chain, so switch first; a wallet that cannot switch surfaces its own error.
  await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: hex }] });
}

async function fetchWithPayment(network: string, input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const chain = chainFor(network);
  const { provider, address } = await evmWallet();
  await ensureChain(provider, chain.id);
  const account = address as Address;
  const wallet = createWalletClient({ account, chain, transport: custom(provider) });
  const publicClient = createPublicClient({ chain, transport: http() });
  const signer = toClientEvmSigner(
    {
      address: account,
      // viem's generic typed-data signature cannot be inferred from x402's
      // loosely typed message; the wallet validates the payload itself.
      signTypedData: (message) => wallet.signTypedData({
        account,
        domain: message.domain,
        types: message.types,
        primaryType: message.primaryType,
        message: message.message,
      } as never),
    },
    publicClient,
  );
  const client = new x402Client().register(network as never, new ExactEvmScheme(signer));
  return wrapFetchWithPayment(fetch, client)(input, init);
}

bridges.OrbitX402 = {
  fetchWithPayment,
  hasEvmWallet: async () => Boolean(await evmWallet().catch(() => null)),
};
