## Behavior changed

## Safety invariants

- [ ] No model output can sign or submit a transaction.
- [ ] Replaced systems were removed, not left as parallel resolution paths.
- [ ] Unknown submission outcomes remain non-retryable until reconciled.
- [ ] Current request and reviewed transaction remain authoritative.

## Verification

- [ ] Routing and execution tests pass, including negative execution cases.
- [ ] TypeScript and browser executor tests pass.
- [ ] No credentials or raw provider error payloads were added.
- [ ] Migration, rollback, and operator actions are documented.
