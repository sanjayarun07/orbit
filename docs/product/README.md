# Orbit product documentation

**Code baseline:** `0dc35f6d` · **Reviewed:** 16 September 2026.

Orbit is a conversational research and transaction-preparation workspace. It combines live market and wallet tools, a cited protocol Knowledge Base, reviewable swap workflows, scheduled tasks, accounts, and metered access. The current implementation is finance-specific; the broader business products discussed separately are possible adaptations, not shipped features.

This documentation describes the checked-in implementation, not the values in a developer's private `.env`. Defaults can be overridden. Availability also depends on provider credentials, subscriptions, ingested content, and infrastructure.

## Updates after the reviewed baseline

Before committing these documents, the repository advanced to `8297bd0c`:

- `0c07de7d` adds wallet-only account sign-in; email is no longer the only account entry path.
- `29ef9563` adds account-backed conversation listing and configurable retention: 30 days for signed-in users, two hours for anonymous sessions by default. Custom sidebar organization remains local.
- `8297bd0c` restricts pytest discovery to `tests/`, moves the live diagnostic to `scripts/smoke_tool_selector.py`, and adds dependency/worker readiness checks at `/readyz`.

The detailed guides and test results below remain a dated review of `0dc35f6d`; their earlier statements about email-only accounts, universal two-hour retention, missing readiness checks, and broken test discovery are superseded by these changes. Consult the current [release operations](../production-operations.md) for the updated checks. The historical QA evidence has not been relabeled as a test of these later commits.

## Reading guide

| Document | Audience | Contents |
|---|---|---|
| [Product and user guide](product-guide.md) | Product, support, users | Capabilities, screens, journeys, plans, tasks, and constraints |
| [Architecture and data](architecture.md) | Engineering, technical stakeholders | Request lifecycle, routing, model tiers, execution, storage, and background work |
| [API and integration guide](api-reference.md) | Client and integration developers | Identity, request contracts, errors, HTTP route inventory, and MCP |
| [Setup and operations](operations.md) | Developers and operators | Local setup, deployment, configuration, ingestion, monitoring, recovery |
| [Development and verification](development.md) | Maintainers and reviewers | Extension points, tests, current findings, and documentation maintenance |
| [Stabilization backlog](stabilization-plan.md) | Product and engineering | Prioritized action items, dependencies, and release acceptance criteria |

## Important implementation boundaries

- Conversation messages and context live in Redis with a **two-hour TTL**, not a permanent PostgreSQL transcript archive. PostgreSQL stores conversation ownership separately.
- Sidebar titles, pins, archives, and projects are browser-local metadata. A copied chat link opens for the owning account; it does not publish the conversation.
- The standard execution experience uses a browser wallet. A separate server-signing endpoint **also exists** and requires `LIVE_TRADING` and a matching configured private key. Do not describe the entire codebase as incapable of server signing.
- Answer validation provides warnings and evidence metadata; it does not prove an answer correct or block every unsupported claim.
- Model-assisted tool selection is implemented but **off by default**. Its self-hosted endpoint is separate from the answer-model configuration.
- The shared conversation and scheduling services are extracted: `execution_policy.py`, `session_access.py`, and `task_scheduling.py`. HTTP and MCP reuse these services.

## Source of truth

The documentation was checked against the API handlers, Pydantic models, UI, execution services, routing/catalogue, knowledge modules, persistence, billing, tasks, deployment files, and tests. Source links in each guide identify the relevant modules. The application reports API version `0.3.0`; Python package metadata remains `0.1.0`. The commit above is the unambiguous baseline.

Existing specialist references remain useful: [routing](../routing-architecture.md), [tool catalogue](../tool-catalog.md), [Knowledge Base](../knowledge-service.md), [session state](../session-state.md), [UI design](../ui-design.md), and [production release gates](../production-operations.md). Some older prose describes earlier behavior; the implementation boundaries and verification notes in this documentation take precedence for this baseline.
