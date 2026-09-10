/** One browser execution boundary. No adapter may silently switch providers. */
export interface Executor<Request, Prepared, Result> {
  prepare(request: Request): Promise<Prepared>;
  execute(prepared: Prepared): Promise<Result>;
  status(id: string): Promise<unknown>;
}

async function api(path: string, body?: unknown) {
  const response = await fetch(path, body === undefined ? undefined : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Execution service unavailable");
  return data;
}

type JupiterRequest = {
  plan_id: string;
  confirmation_text: string;
  sign: (transaction: string, wallet: string) => Promise<string>;
};
type JupiterPrepared = JupiterRequest & {transaction: string; wallet_address: string};
const jupiter: Executor<JupiterRequest, JupiterPrepared, {signature: string; status: string}> = {
  async prepare(request) {
    const prepared = await api(`/trade-plans/${encodeURIComponent(request.plan_id)}/wallet-transaction`, {confirmation_text: request.confirmation_text});
    return {...request, transaction: prepared.transaction, wallet_address: prepared.wallet_address};
  },
  async execute(prepared) {
    const signed = await prepared.sign(prepared.transaction, prepared.wallet_address);
    return api(`/trade-plans/${encodeURIComponent(prepared.plan_id)}/submit-wallet-transaction`, {
      confirmation_text: prepared.confirmation_text, signed_transaction: signed,
    });
  },
  status: id => api(`/trade-plans/${encodeURIComponent(id)}`),
};

type RelayRequest = Parameters<Window["OrbitRelay"]["getFreshQuote"]>[0];
type RelayPrepared = Awaited<ReturnType<Window["OrbitRelay"]["getFreshQuote"]>>;
type RelayScope = {session_id: string; revision: number};
type RelayExecution = Pick<RelayPrepared, "quote" | "wallet"> & {onProgress: Parameters<Window["OrbitRelay"]["executeQuote"]>[2]; scope?: RelayScope; isCurrent?: () => boolean};
const relay: Executor<RelayRequest, RelayExecution, unknown> = {
  async prepare(request) {
    return {...await window.OrbitRelay.getFreshQuote(request), onProgress: () => {}};
  },
  async execute(prepared) {
    if (prepared.isCurrent && !prepared.isCurrent()) throw new Error("This request was superseded");
    const requestId = prepared.quote.steps?.find(step => step.requestId)?.requestId;
    if (!requestId) throw new Error("Relay quote has no trackable request ID; request a fresh quote.");
    const record = await api(`/executions/relay/${encodeURIComponent(requestId)}`, prepared.scope || {});
    if (!record.execution_claimed) throw new Error("This Relay request was already attempted. Check its settlement; do not submit it again.");
    if (prepared.isCurrent && !prepared.isCurrent()) throw new Error("This request was superseded before wallet approval");
    // The attempt stays locked even if wallet approval or the SDK times out.
    return window.OrbitRelay.executeQuote(prepared.quote, prepared.wallet, prepared.onProgress);
  },
  status: id => api(`/executions/relay/${encodeURIComponent(id)}`),
};

const executors = {
  jupiter, relay,
  executeRelay: (quote: RelayExecution["quote"], wallet: RelayExecution["wallet"], onProgress: RelayExecution["onProgress"], scope?: RelayScope, isCurrent?: () => boolean) => relay.execute({quote, wallet, onProgress, scope, isCurrent}),
};
declare global { interface Window { OrbitExecutors: typeof executors; } }
window.OrbitExecutors = executors;
