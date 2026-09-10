"""Allowlisted activity metadata; never forward raw provider payloads by default."""

import re


def public_activity(trajectory: dict | None) -> dict | None:
    if not isinstance(trajectory, dict):
        return None
    result = {}
    for key, name in trajectory.items():
        match = re.fullmatch(r"tool_name_(\d+)", key)
        if not match or not isinstance(name, str) or not re.fullmatch(r"[\w.-]{1,120}", name) or name == "finish":
            continue
        index = match.group(1)
        observation = trajectory.get(f"observation_{index}")
        failed = isinstance(observation, str) and (
            "failed:" in observation.lower() or "error" in observation[:120].lower()
        )
        result[key] = name
        result[f"observation_{index}"] = (
            "Tool call failed: provider details are hidden." if failed
            else "Tool call completed. See the answer for results."
        )
    return result or None
