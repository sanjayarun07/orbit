import {createAppKit} from "@reown/appkit";
import {EthersAdapter} from "@reown/appkit-adapter-ethers";
import {mainnet, arbitrum, base, optimism, polygon, avalanche, bsc, type AppKitNetwork} from "@reown/appkit/networks";
import type {EIP1193Provider} from "viem";

type PublicConfig = {reown?: {enabled: boolean; project_id: string | null}};
type State = {configured: boolean; connected: boolean; address?: string};
let modal: ReturnType<typeof createAppKit> | null = null;
let initialization: Promise<State> | null = null;

function state(): State {
  return {configured: Boolean(modal), connected: Boolean(modal?.getIsConnectedState()), address: modal?.getAddress("eip155")};
}
function emit() {
  window.dispatchEvent(new CustomEvent("orbit:reown-state", {detail: state()}));
}
async function initialize() {
  if (initialization) return initialization;
  initialization = (async () => {
    const response = await fetch("/config/public", {headers: {Accept: "application/json"}});
    if (!response.ok) throw new Error("WalletConnect could not load. Please try again.");
    const config = await response.json() as PublicConfig;
    if (!config.reown?.enabled || !config.reown.project_id) { emit(); return state(); }
    const networks = [mainnet, arbitrum, base, optimism, polygon, avalanche, bsc] as [AppKitNetwork, ...AppKitNetwork[]];
    modal = createAppKit({
      adapters: [new EthersAdapter()], networks, projectId: config.reown.project_id,
      metadata: {name: "Anvaya", description: "Web3 research and execution copilot", url: window.location.origin, icons: []},
      features: {analytics: false, email: false, socials: false},
    });
    modal.subscribeProviders(emit);
    emit();
    return state();
  })();
  return initialization;
}
async function connect() {
  const current = await initialize();
  if (!current.configured || !modal) throw new Error("WalletConnect is currently unavailable. Choose another wallet to continue.");
  if (!modal.getIsConnectedState()) await modal.open({view: "Connect"});
  return state();
}
async function disconnect() {
  await modal?.disconnect("eip155");
  emit();
}
async function getEvmWallet() {
  await initialize();
  const provider = modal?.getProvider<EIP1193Provider>("eip155");
  const address = modal?.getAddress("eip155");
  if (!provider || !address) throw new Error("No Reown EVM wallet is connected.");
  return {provider, address};
}

const bridge = {initialize, connect, disconnect, getState: state, getEvmWallet};
declare global { interface Window { OrbitReown: typeof bridge; } }
window.OrbitReown = bridge;
window.dispatchEvent(new CustomEvent("orbit:reown-ready"));
void initialize().catch(error => window.dispatchEvent(new CustomEvent("orbit:reown-error", {detail: {message: error instanceof Error ? error.message : String(error)}})));
