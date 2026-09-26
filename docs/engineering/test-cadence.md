# Testing cadence

Use the smallest check that can detect the change's likely failure. A full suite and a live sweep remain release evidence; repeating them after every small edit adds time without improving coverage.

## During development

1. Run the changed module's tests and its nearest boundary tests. For a routing or research change, start with:

   ```sh
   .venv/bin/pytest -q tests/test_routing_gate.py tests/test_tool_catalog.py tests/test_research_loop.py tests/test_research_gaps.py
   ```

   For the equities-data gateway, run:

   ```sh
   .venv/bin/pytest -q tests/test_equities_data.py tests/test_equity_research.py
   ```

2. Replay the affected recorded episode once while iterating. `scripts/episode_eval.py` runs once by default and supports `--only <episode-id>`. Use `--k 5` only when explicitly measuring consistency across repeated runs.
3. If browser behavior changed, run the affected UI flow at desktop and mobile widths. Do not spend provider credits on unrelated prompts.

## Before a release candidate

- Freeze the candidate commit and configuration. Run `.venv/bin/pytest -q`, `npm run check:relay`, the relevant browser tests, and the recorded episode suite once against that candidate.
- Run a small live sample selected from the changed capabilities and known failures. Record identity, source support, latency, provider calls and cost. Do not rerun an unchanged live matrix merely because another test passed.
- Run the complete authenticated UI and provider sweep for UAT sign-off or after a change that crosses those boundaries. Keep the resulting report attached to the candidate commit.

## When a check fails

Fix the cause, rerun the failing case and its boundary tests, then rerun the applicable release checks. A sandbox-denied local socket is an environment failure; rerun that test with socket access instead of treating it as a code regression.

The single-run result does not establish consistency. Run a separate repeated evaluation when a reliability claim or a routing comparison needs pass^k evidence.
