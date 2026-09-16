"""User-facing task scheduling policy shared by HTTP and chat/MCP controls.

The task store and worker remain in tasks; this service owns account access,
resume scheduling, validation errors, and chat timezone preference handling.
"""
from app import accounts, tasks
from app.identity import Identity
from app.models import ChatRequest
from app.service_errors import ServiceError


async def list_for_user(identity: Identity) -> dict:
    items = await tasks.list_tasks(identity.user["id"])
    return {"tasks": [tasks.public(t) for t in items], "limit": tasks.TASK_LIMITS.get(identity.plan.id, 3), "unread": await tasks.unread_count(identity.user["id"])}


async def require_task(task_id: str, user_id: str) -> dict:
    task = await tasks.get_task(task_id)
    if task is None or task["user_id"] != user_id:
        raise ServiceError(404, "Task not found")
    return task


async def create_task(user: dict, kind: str, spec: dict, schedule: dict, channel: str = "inapp", tz_offset_min: int = 0, title: str | None = None) -> dict:
    try:
        return await tasks.create_task(user, kind, spec, schedule, channel, tz_offset_min, title)
    except (ValueError, KeyError, TypeError) as exc:
        raise ServiceError(400, str(exc) or "Invalid task") from exc


async def update_task(task_id: str, user_id: str, user: dict | None = None, **fields) -> dict:
    current = await require_task(task_id, user_id)
    if fields.get("status") == "active" and current["status"] != "active":
        # Resuming a paused task makes it active, so it has to pass the same
        # plan limit creating one does.
        if user is not None:
            try:
                await tasks.assert_can_activate(user, exclude_task_id=task_id)
            except ValueError as exc:
                raise ServiceError(403, str(exc)) from exc
        fields["next_run_at"] = tasks.next_run(current["schedule"], current.get("tz_offset_min", 0))
    task = await tasks.update_task(task_id, user_id, **fields)
    if task is None:
        raise ServiceError(404, "Task not found")
    return task


async def delete_task(task_id: str, user_id: str) -> bool:
    if not await tasks.delete_task(task_id, user_id):
        raise ServiceError(404, "Task not found")
    return True


async def run_now(task_id: str, user_id: str) -> dict:
    return await tasks.run_task(await require_task(task_id, user_id))


async def handle_chat_control(body: ChatRequest, identity: Identity, action: dict | None) -> str | None:
    # Imported here because the natural-language adapter also calls this service.
    from app import tasks_nl

    if not identity.signed_in or action is not None or not tasks_nl.is_task_control(body.message):
        return None
    preferences = identity.user.get("preferences") or {}
    if body.tz_offset_min is not None and preferences.get("tz_offset_min") != body.tz_offset_min:
        identity.user = await accounts.update_user(identity.user["id"], preferences={"tz_offset_min": body.tz_offset_min}) or identity.user
    tz = body.tz_offset_min if body.tz_offset_min is not None else int((identity.user.get("preferences") or {}).get("tz_offset_min") or 0)
    return await tasks_nl.handle(body.message, identity.user, tz)
