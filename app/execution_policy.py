"""Shared chat execution policy for HTTP and MCP.

Owns admission, credit holds, session serialization, revision checks, charter
policy, agent execution and persistence. Transport adapters only resolve the
caller and translate ServiceError into their response format.
"""
from __future__ import annotations

import asyncio
import json
import time
import logging
import re
from uuid import uuid4

from app import address_roles, accounts, credits, notifications, research_gaps, session_access, task_scheduling, tool_outcomes
from app.answer_validator import validate_answer
from app.experience import (advance_session_context, build_context_capsules, build_evidence_summary,
    with_resolved_token, build_gas_advisory, build_intent_lock, build_trade_readiness)
from app.graph import run_agent
from app.identity import Identity, current_identity, service_identity
from app import charts, decision_records, exit_controls, followups, jobs, streaming, turn_log, user_memory
from app import evidence as evidence_envelopes

# Fire-and-forget work that must still finish: kept here so a shutdown can
# wait for it instead of dropping it (asyncio keeps only weak references to
# tasks nobody holds) -- review, 2026-09-20.
_BACKGROUND: set[asyncio.Task] = set()


def background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


async def drain_background(timeout: float = 15.0) -> int:
    """Wait for outstanding background work; how many were pending."""
    pending = [t for t in _BACKGROUND if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)
    return len(pending)
from app.integrations import tradingview
from app.limits import acquire_chat_slot, allow_chat_request, allow_chat_request_from_ip, release_chat_slot
from app.metrics import increment
from app.models import AgentResponse, ChatRequest, RiskCharterFields
from app.plans import reset_plan_owner, set_plan_owner, mark_plan_superseded
from app.public_activity import public_activity
from app.routing.controls import is_trade_confirmation, is_charter_clear
from app.routing.workflow import WorkflowState, WorkflowEvent, apply_event
from app.service_errors import ServiceError, safe_detail as _safe_detail
from app.sessions import CoordinationStoreFull, acquire_session_turn, commit_turn, extend_retention, get_session_snapshot, history_text_from_messages
from app.settings import settings
from app.billing_plans import FREE
from app.solana_rpc import rpc

logger = logging.getLogger(__name__)


async def execute_chat_turn(body: ChatRequest, identity: Identity | str) -> AgentResponse:
    """One chat turn, logged whatever happens (app/turn_log.py): the answer
    with its tools, validation and gate verdicts on success; the status and
    detail on every refusal, timeout and crash. Then the turn itself."""
    resolved = identity if not isinstance(identity, str) else (current_identity.get() or service_identity(identity))
    transport = "stream" if streaming.active() else ("mcp" if resolved.kind == "service" else "json")
    started = time.monotonic()

    async def log(status: str, http_status: int, **fields) -> None:
        # The log is never a reason for a turn to fail or to wait: the write
        # runs as a tracked background task (drained at shutdown), and the
        # store itself bounds its own wait (review, 2026-09-22).
        try:
            background(turn_log.record(message=body.message, status=status, latency_ms=int((time.monotonic() - started) * 1000), http_status=http_status,
                                       transport=transport, identity=resolved, wallet=body.wallet_address, **fields))
        except Exception:
            logger.warning("turn log failed", exc_info=True)

    try:
        response = await _admitted_chat_turn(body, resolved)
    except ServiceError as exc:
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, default=str)
        await log("error", exc.status_code, error=detail, session_id=body.session_id)
        raise
    except BaseException as exc:
        await log("error", 500, error=f"{type(exc).__name__}: {exc}"[:500], session_id=body.session_id)
        raise
    await log("ok", 200, session_id=response.session_id, response=response)
    return response


async def _admitted_chat_turn(body: ChatRequest, identity: Identity | str) -> AgentResponse:
    """One chat turn with the same admission, per-session locking, budgets and
    persistence as POST /chat. Also the entry point for the MCP server.

    `identity` decides rate limits, sign-in gates and credits: a signed-in user
    or API key is charged from its ledger, an anonymous visitor from the trial,
    and a service caller (a plain string, e.g. the MCP server without a user
    key) is never charged."""
    if isinstance(identity, str):
        identity = current_identity.get() or service_identity(identity)
    session_id = body.session_id or str(uuid4())
    # A conversation is private to the account that first used it while signed
    # in; nobody else may read, extend or delete it by knowing its id. A
    # signed-in caller claims it here, BEFORE any work -- for an id the caller
    # named and for one the server just minted alike. The server-minted case
    # used to be mapped only after the turn, with the failure swallowed: under
    # a database fault a private answer came back on a conversation nobody
    # owned, claimable by whoever learned its id. Now the claim is part of the
    # turn, and if it cannot be recorded there is no turn.
    if body.session_id:
        await _require_session_access(session_id, identity, claim=True)
    elif identity.signed_in:
        try:
            if not await accounts.touch_chat_session(identity.user["id"], session_id):
                raise ServiceError(409, "Could not start a new conversation; try again.")
        except ServiceError:
            raise
        except Exception as exc:
            logger.warning("chat session ownership could not be recorded", exc_info=True)
            raise ServiceError(503, "History is unavailable right now, so this conversation cannot be started. Try again shortly.") from exc
    if body.wallet_address and not identity.signed_in and identity.kind != "service":
        raise ServiceError(401, {"error": "sign_in_required", "message": "Sign in with your email to use a wallet, trade, or keep history."})
    if identity.api_key is not None and not identity.has_scope("chat"):
        raise ServiceError(403, "This API key does not have the chat scope")
    # A plan against the server-held wallet is refused at quoting time for
    # anyone not entitled to that wallet, not only at signing time -- a quote
    # that can never be executed should not exist, and the confirm route's
    # own check then covers the case where the entitlement changes between.
    if body.wallet_address and settings.allow_custodial_signing:
        from app import deployment
        from app.execution import custodial_signer_address

        signer = custodial_signer_address()
        if signer and body.wallet_address.strip() == signer and identity.principal_id not in deployment.custodial_principals():
            raise ServiceError(403, {"error": "custodial_wallet_not_entitled",
                                     "message": "This account may not trade from the server-held wallet."})
    turn_id = str(uuid4())
    reserved = 0
    if identity.kind != "service":
        try:
            reserved = await credits.reserve(identity.account_id, turn_id)
        except credits.InsufficientCredits as exc:
            increment("chat_insufficient_credits")
            raise ServiceError(402, {
                "error": "insufficient_credits", "balance": exc.balance, "required": exc.required,
                "plan": identity.plan.id, "signed_in": identity.signed_in,
                "message": (f"You've used your trial credits. Sign in to get {FREE.monthly_credits} free credits every month."
                            if not identity.signed_in else "You're out of credits for this month. Upgrade or buy a credit pack."),
            })
    # principal_id, not account_id: a plan belongs to the person who asked for
    # it, not to whichever account is billed for the turn. Team members share
    # the owner's billing account.
    owner_token = set_plan_owner(identity.principal_id)
    try:
        response = await _execute_chat_turn(body, identity, session_id)
    except BaseException:
        if reserved:
            await credits.release(identity.account_id, turn_id, reserved)
        raise
    finally:
        reset_plan_owner(owner_token)
    if reserved:
        cost, kind = credits.turn_cost(response.intent, response.trajectory, response.team_report)
        charged = await credits.settle(
            identity.account_id, turn_id, reserved, cost, kind, api_key_id=identity.api_key["id"] if identity.api_key else None,
        )
        balance = await credits.balance(identity.account_id)
        response.credits = {"charged": charged, "kind": kind, "balance": balance}
        if identity.signed_in:
            try:
                await notifications.maybe_low_credit_alert(identity.team_owner or identity.user, balance)
            except Exception:
                logger.warning("low-credit alert failed", exc_info=True)
    return response


async def _require_session_access(session_id: str, identity: Identity | None, claim: bool = False) -> None:
    """Translate ownership failures into the shared service error contract."""
    try:
        await session_access.require_session_access(session_id, identity, claim=claim)
    except session_access.SessionAccessDenied as exc:
        raise ServiceError(404, "Conversation not found") from exc


async def _require_still_admitted(session_id: str, identity: Identity) -> None:
    """The admission check again, under the turn lease.

    Ownership is checked and claimed before the lease, and the wait for the
    lease is exactly where that can go stale: a deletion running under the
    lease removes the ownership mapping, and another account can then claim
    the id. A queued turn that went on with its earlier admission committed
    a private question and answer into that other account's conversation.
    So nothing is read or written until the conversation is confirmed to be
    what the caller was admitted to: owned by them, or (for a visitor)
    owned by nobody. Anything else is reported as not found, the same answer
    the first check gives."""
    owner = await accounts.chat_session_owner(session_id)
    if owner != session_access.caller_user_id(identity):
        raise ServiceError(404, "Conversation not found")


async def _execute_chat_turn(body: ChatRequest, identity: Identity, session_id: str) -> AgentResponse:
    allowed, retry_after = await allow_chat_request(identity.rate_limit_key, getattr(identity.plan, "chat_requests_per_minute", None))
    if allowed and identity.kind != "service":
        # The identity bucket is keyed by a device id the caller chooses; the
        # network bucket cannot be rotated away from the client side.
        allowed, retry_after = await allow_chat_request_from_ip(identity.ip)
    if not allowed:
        increment("chat_rate_limited")
        raise ServiceError(
            429,
            "Too many chat requests; please retry shortly",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        await acquire_chat_slot()
    except asyncio.TimeoutError as exc:
        increment("chat_queue_timeouts")
        raise ServiceError(503, "Chat service is busy; please retry shortly") from exc
    increment("chat_requests")
    session_lease = None
    try:
        try:
            session_lease = await acquire_session_turn(session_id)
        except CoordinationStoreFull as exc:
            increment("chat_lock_store_full")
            raise ServiceError(503, "The conversation store is full; please retry shortly") from exc
        except asyncio.TimeoutError as exc:
            raise ServiceError(
                409,
                "Another request is already updating this chat. Wait for it to finish and retry.",
            ) from exc
        await _require_still_admitted(session_id, identity)
        messages, session_context = await get_session_snapshot(session_id)
        history = history_text_from_messages(messages)
        # What Orbit remembers about a signed-in user from earlier chats, as
        # context the general and research paths read (app/user_memory.py).
        memory_user = identity.user["id"] if getattr(identity, "signed_in", False) and identity.user and not user_memory.opted_out(identity.user) else None
        if memory_user and user_memory.enabled():
            try:
                recalled = await asyncio.wait_for(user_memory.recall(memory_user, body.message), timeout=6)
                history = user_memory.with_block(history, recalled)
                if recalled:
                    session_context = {**session_context, "user_memory": [f["fact"] for f in recalled]}
            except Exception:
                logger.warning("user memory recall skipped for %s", memory_user[:8], exc_info=True)
        current_revision = int(session_context.get("revision", 0))
        if body.context_revision is not None and body.context_revision != current_revision:
            raise ServiceError(
                409,
                "This chat changed in another tab or request. Refresh the conversation before continuing.",
            )
        action = body.quick_action.model_dump() if body.quick_action else None
        # A quick action is authoritative only for the exact context revision
        # that produced it. Never reinterpret a stale action as free text.
        if action and (
            action["context_revision"] != current_revision
            or action["prompt"] != body.message
        ):
            raise ServiceError(
                409,
                "This quick action belongs to an older response. Use an action from the latest response.",
            )
        if action:
            latest_actions = next(
                (
                    item.get("quick_actions") or []
                    for item in reversed(messages)
                    if item.get("role") == "assistant"
                    and int(item.get("session_revision") or -1) == current_revision
                ),
                [],
            )
            authorized = next(
                (item for item in latest_actions if item.get("id") == action["id"]),
                None,
            )
            if authorized is None or body.quick_action.model_dump() != authorized:
                raise ServiceError(
                    409,
                    "This quick action is not authorized by the latest response. Refresh the conversation.",
                )
            # Route only with the server-persisted command, never client claims.
            action = authorized
        # Invalidate the old approval before potentially slow inference. A new
        # turn must not leave its previous quote executable during generation.
        active_plan_id = (session_context.get("active_workflow") or {}).get("plan_id")
        if not active_plan_id and session_context.get("active_workflow"):
            active_plan_id = next((
                (item.get("trade_plan") or {}).get("plan_id")
                for item in reversed(messages) if item.get("trade_plan")
            ), None)
        if active_plan_id and not is_trade_confirmation(body.message):
            await mark_plan_superseded(active_plan_id)
        if body.team_mode is not None:
            session_context = {**session_context, "team_mode": body.team_mode}
        charter_fields = body.risk_charter_fields
        # A signed-in user's saved risk charter is the default for every new
        # conversation (the card in any conversation updates that default).
        default_fields = ((identity.user or {}).get("preferences") or {}).get("risk_charter_fields") if identity.signed_in else None
        if default_fields and current_revision == 0 and not session_context.get("risk_charter") and charter_fields is None:
            try:
                seeded = RiskCharterFields(**default_fields)
                if not seeded.is_empty():
                    session_context = {**session_context, "risk_charter": seeded.render(), "risk_charter_fields": seeded.model_dump()}
            except (TypeError, ValueError):
                logger.warning("ignoring malformed saved risk charter for user %s", identity.user["id"])
        if charter_fields is not None and identity.signed_in and not charter_fields.is_empty():
            await accounts.update_user(identity.user["id"], preferences={"risk_charter_fields": charter_fields.model_dump()})
        elif identity.signed_in and default_fields and is_charter_clear(body.message):
            # "clear my risk charter" clears the saved default too; otherwise the
            # next conversation would silently re-apply the rules just removed.
            await accounts.update_user(identity.user["id"], preferences={"risk_charter_fields": None})
            session_context = {**session_context, "risk_charter": None, "risk_charter_fields": None}
        if charter_fields is not None:
            if charter_fields.is_empty():
                raise ServiceError(400, "Choose at least one rule for the risk charter.")
            if charter_fields.max_trade_usd is not None and charter_fields.max_trade_usd > settings.max_trade_usd:
                raise ServiceError(400, f"Max per trade cannot exceed the built-in cap of ${settings.max_trade_usd:,.2f}.")
            if charter_fields.max_slippage_bps is not None and charter_fields.max_slippage_bps > settings.max_slippage_bps:
                raise ServiceError(400, f"Max slippage cannot exceed the built-in cap of {settings.max_slippage_bps} bps.")
            # The canonical text is what the chat phrase path would have stored,
            # so the transcript, the chip and the Risk agent all see one rendering.
            body.message = f"set my risk charter: {charter_fields.render()}"
        # A wallet connected earlier in this conversation is remembered: a turn
        # that names none uses it, until the client disconnects (DELETE /chat/wallet).
        remembered_wallet = ((session_context or {}).get("connected_wallet") or {}).get("address")
        effective_wallet = body.wallet_address or remembered_wallet or ""
        task_reply = await task_scheduling.handle_chat_control(body, identity, action)
        exit_reply = False
        if task_reply is None and action is None and (identity.signed_in or exit_controls.is_public_control(body.message)) and exit_controls.is_exit_control(body.message):
            # Sizing before entry needs no account (a guest asked it, 2026-09-23); the position controls need one.
            task_reply = await exit_controls.handle(body.message, identity.user if identity.signed_in else None, effective_wallet or None,
                                                    focus=(session_context or {}).get("focus"),
                                                    last_capabilities=(session_context or {}).get("last_capabilities"))
            exit_reply = task_reply is not None
        if task_reply is not None:
            from app.graph import AgentRun
            # No trajectory: a task control is a plain (1-credit) turn, not a tool turn.
            # An exit control is remembered as such ("I only want a read-only estimate" right after it).
            run = AgentRun(answer=task_reply, trajectory=None, trade_plan=None, intent="general", capabilities=["exit_control"] if exit_reply else [], control=True)
            turn_evidence = []
        else:
            # The user's TradingView token is bound to this turn (None when
            # they have no connection), so the TradingView tools can match
            # and act on their behalf, and only theirs.
            signed_in_user = identity.user["id"] if getattr(identity, "signed_in", False) and identity.user else None
            tv_bound = await tradingview.bind_turn(signed_in_user)
            # Decision receipts made during this turn belong to this user
            # (None: an anonymous decision, kept without an owner).
            receipts_bound = decision_records.bind_turn(signed_in_user)
            # A job started in this turn: owned by this user, billed to this
            # account, its answer delivered to this conversation.
            jobs_bound = jobs.bind_turn(signed_in_user, identity.account_id if signed_in_user else None, session_id)
            evidence_bound = evidence_envelopes.start_turn()
            # The addresses this conversation already typed (the focus token,
            # the focus wallet): a wallet reader refuses a token mint.
            roles_bound = address_roles.bind_turn(session_context, body.message)
            try:
                run = await asyncio.wait_for(
                    run_agent(
                        body.message,
                        effective_wallet,
                        history,
                        session_context,
                        action,
                    ),
                    timeout=(settings.research_loop_timeout_seconds if settings.research_loop_enabled else settings.chat_execution_timeout_seconds),
                )
                turn_evidence = evidence_envelopes.collected()
            finally:
                address_roles.end_turn(roles_bound)
                evidence_envelopes.end_turn(evidence_bound)
                tradingview.current_token.reset(tv_bound)
                decision_records.current_user.reset(receipts_bound)
                jobs.current_owner.reset(jobs_bound)
        increment(f"intent_{run.intent}")
        answer, trajectory, plan = run
        client_trajectory = trajectory if settings.expose_tool_trajectory else public_activity(trajectory)
        # Quick-action chips stay off (generic, 2026-09-14). Related questions
        # are different: written from this answer and kept only when grounded
        # in it (app/followups.py); [] renders nothing.
        suggestions: list[str] = []
        intent_lock = build_intent_lock(plan, run.cross_chain_swap, body.wallet_address)
        context_capsules = with_resolved_token(
            build_context_capsules(
                body.message, history, answer, plan, run.cross_chain_swap, body.wallet_address
            ),
            run.resolved_token,
        )
        evidence = build_evidence_summary(trajectory)
        # Step-7 answer validation: provenance / freshness / grounding of the
        # surfaced answer against the tool evidence. Advisory only -- attached for
        # the client and monitoring, never blocks or rewrites the answer.
        validation = validate_answer(body.message, answer, trajectory, run.intent, evidence=turn_evidence)
        if validation is not None and validation.status == "warn":
            increment("answer_validation_warn")
        # Close the loop: credit or debit every tool that ran, so the router's
        # ranking learns from what the answer could actually use.
        try:
            await tool_outcomes.record_turn(trajectory, validation)
        except Exception:
            logger.warning("tool outcome recording failed", exc_info=True)
        # The fall-through log: a research turn that only web search could answer
        # is a data gap; tagged by topic so paid-source decisions are counted.
        try:
            await research_gaps.record(body.message, run.intent, tool_outcomes.tools_in(trajectory), account_id=identity.account_id if identity else None, session_id=session_id)
        except Exception:
            logger.warning("research gap recording failed", exc_info=True)
        trade_readiness = build_trade_readiness(plan)
        gas_advisory = build_gas_advisory(run.cross_chain_swap)
        from app import research_objective
        objective = await research_objective.update((session_context or {}).get("research_objective"), body.message, answer,
                                                    intent=run.intent, contract=getattr(run, "contract", None))
        next_context = advance_session_context(
            session_context,
            body.message,
            effective_wallet or None,
            run.intent,
            run.capabilities,
            context_capsules,
            intent_lock,
            run.cross_chain_swap,
            last_contract=getattr(run, "contract", None),
            research_objective=objective,
            answer=answer,
        )
        # A pending token disambiguation lives exactly one turn: set when this
        # turn asked which chain a symbol is on, otherwise cleared (the follow-up
        # consumes it before the graph runs -- see resolve_pending_token).
        next_context["pending_token"] = run.pending_token
        next_context["pending_wallet_request"] = run.pending_wallet_request
        if charter_fields is not None:
            next_context["risk_charter_fields"] = charter_fields.model_dump()
        if next_context.get("active_workflow") and plan:
            next_context["active_workflow"]["plan_id"] = plan.plan_id
        old_workflow = WorkflowState.from_context(session_context.get("active_workflow"))
        new_workflow = WorkflowState.from_context(next_context.get("active_workflow"))
        event = WorkflowEvent.TOPIC_CHANGE if new_workflow is None else WorkflowEvent.REPLACE
        next_workflow, superseded_plan = apply_event(old_workflow, event, new_workflow)
        if superseded_plan:
            await mark_plan_superseded(superseded_plan)
        next_context["active_workflow"] = next_workflow.as_context() if next_workflow else None
        quick_actions: list = []
        # The chart the browser draws under the answer: the resolved token's
        # spot pair, or the equity's listing. Best effort and bounded; never
        # a reason for a turn to fail.
        async def _chart():
            try:
                return await asyncio.wait_for(asyncio.to_thread(charts.chart_for, run, body.message), timeout=6)
            except Exception:
                logger.debug("chart card skipped", exc_info=True)
                return None

        async def _related():
            # A command about the user's own tasks or positions is not a
            # research subject: "who operates this wallet" was offered under
            # the user's own exit card (live, 2026-09-22).
            return [] if getattr(run, "control", False) else await followups.generate(body.message, answer, run.intent, plan)

        chart, suggestions = await asyncio.gather(_chart(), _related())
        if memory_user and user_memory.enabled() and plan is None:
            # Off the turn's critical path: the answer is already written.
            background(user_memory.extract(memory_user, session_id, body.message, answer, run.intent))
        assistant_metadata = {
            "chart": chart,
            "routing_decision": getattr(run, "routing_decision", None),
            "trajectory": client_trajectory,
            "trade_plan": plan.model_dump(mode="json") if plan else None,
            "suggestions": suggestions,
            "quick_actions": [item.model_dump() for item in quick_actions],
            "session_revision": next_context["revision"],
            "intent": run.intent,
            "capabilities": run.capabilities,
            "cross_chain_swap": run.cross_chain_swap.model_dump() if run.cross_chain_swap else None,
            "context_capsules": [item.model_dump() for item in context_capsules],
            "intent_lock": intent_lock.model_dump() if intent_lock else None,
            "evidence": evidence.model_dump(mode="json") if evidence else None,
            "trade_readiness": trade_readiness.model_dump() if trade_readiness else None,
            "gas_advisory": gas_advisory.model_dump() if gas_advisory else None,
            "risk_assessment": run.risk_assessment.model_dump() if run.risk_assessment else None,
            "team_report": run.team_report,
            "validation": validation.model_dump() if validation else None,
            # The job whose answer this is: the durable record delivery and
            # billing reconcile from, whatever happens to the acknowledgement
            # (review, 2026-09-22).
            "job_id": getattr(run, "job_id", None) if getattr(run, "job_attached", False) else None,
        }
        await commit_turn(
            session_id,
            body.message,
            answer,
            assistant_metadata,
            next_context,
        )
        if getattr(run, "job_id", None) and getattr(run, "job_attached", False):
            # The job's answer is now in the conversation and charged as this
            # turn: acknowledge it so it is never delivered again. The commit
            # is the durable record; a failed acknowledgement is reconciled
            # by maintenance from the message's job id, so it never fails
            # the turn.
            try:
                await jobs.acknowledge(run.job_id)
            except Exception:
                logger.warning("job %s: acknowledgement failed after commit; maintenance reconciles", run.job_id, exc_info=True)
        if identity.signed_in:
            # Still under the lease, so this refresh of last_used (and the
            # longer retention a signed-in account's history gets) can never
            # re-map a conversation a deletion has just forgotten: deletion
            # takes the same lease, and the ownership was confirmed above.
            try:
                await accounts.touch_chat_session(identity.user["id"], session_id)
                await extend_retention(session_id, settings.chat_history_signed_in_ttl_seconds)
            except Exception:
                logger.warning("chat session ownership update failed", exc_info=True)
        return AgentResponse(
            envelopes=[e.public() for e in turn_evidence],
            job_id=getattr(run, "job_id", None) if not getattr(run, "job_attached", False) else None,
            answer=answer,
            trade_plan=plan,
            trajectory=client_trajectory,
            session_id=session_id,
            suggestions=suggestions,
            quick_actions=quick_actions,
            session_revision=next_context["revision"],
            intent=run.intent,
            capabilities=run.capabilities,
            cross_chain_swap=run.cross_chain_swap,
            context_capsules=context_capsules,
            intent_lock=intent_lock,
            evidence=evidence,
            trade_readiness=trade_readiness,
            gas_advisory=gas_advisory,
            risk_assessment=run.risk_assessment,
            team_report=run.team_report,
            validation=validation,
            chart=chart,
            team_mode=bool(next_context.get("team_mode")),
            risk_charter=next_context.get("risk_charter") or None,
            risk_charter_fields=next_context.get("risk_charter_fields") or None,
            answer_gate=getattr(run, "answer_gate", None),
        )
    except asyncio.TimeoutError as exc:
        increment("chat_timeouts")
        raise ServiceError(504, "The request took too long; please retry") from exc
    except ValueError as exc:
        increment("chat_errors")
        raise ServiceError(400, _safe_detail(exc, "The request could not be completed")) from exc
    except ServiceError:
        raise
    except Exception as exc:
        increment("chat_errors")
        error_id = str(uuid4())
        logger.exception("Chat request failed [%s]", error_id)
        raise ServiceError(500, f"Chat request failed (reference {error_id})") from exc
    finally:
        if session_lease is not None:
            await session_lease.release()
        release_chat_slot()


async def solana_execution_status(signature: str):
    """Execution-concierge status for a previously submitted Solana transaction."""
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{64,96}", signature):
        raise ServiceError(400, "Invalid Solana transaction signature")
    try:
        payload = await rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
    except Exception as exc:
        raise ServiceError(502, _safe_detail(exc, "Solana status provider is unavailable")) from exc
    value = (payload or {}).get("value", [None])[0] if isinstance(payload, dict) else None
    if value is None:
        return {"signature": signature, "status": "not_found", "finalized": False}
    error = value.get("err")
    confirmation = value.get("confirmationStatus") or "processed"
    return {
        "signature": signature,
        "status": "failed" if error else confirmation,
        "finalized": not error and confirmation == "finalized",
        "confirmations": value.get("confirmations"),
        "slot": value.get("slot"),
        "error": error,
    }

