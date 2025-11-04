from __future__ import annotations

import json
import logging
import os
from typing import Iterable, Any

import discord
from discord import app_commands

log = logging.getLogger(__name__)

# Prefer the working dir copy first; then explicit env; then /app copy.
CONFIG_PATHS = [
    "./config.json",
    os.getenv("CONFIG_FILE") or "",
    "/app/config.json",
]


def _normalize_ids(v: Any) -> list[int]:
    out: list[int] = []
    if v is None:
        return out
    if isinstance(v, (list, tuple, set)):
        for x in v:
            if isinstance(x, int):
                out.append(x)
            elif isinstance(x, str) and x.strip().isdigit():
                out.append(int(x.strip()))
    elif isinstance(v, str):
        for p in v.replace(";", ",").split(","):
            p = p.strip()
            if p.isdigit():
                out.append(int(p))
    return out


def _load_staff_ids() -> list[int]:
    for path in CONFIG_PATHS:
        if not path:
            continue
        try:
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            rcon = data.get("rcon")
            if isinstance(rcon, list) and rcon:
                ids = _normalize_ids(rcon[0].get("staff_role_ids"))
                if ids:
                    log.info("RBAC: using staff_role_ids from %s: %s", path, ids)
                    return ids
                else:
                    log.warning("RBAC: %s has empty rcon[0].staff_role_ids", path)
        except Exception as e:
            log.error("RBAC: failed reading %s: %s", path, e)
    return []


def _member_role_ids(member: discord.Member | None) -> set[int]:
    if member is None:
        return set()
    return {r.id for r in getattr(member, "roles", []) if isinstance(r, discord.Role)}


def _has_any_role(member: discord.Member | None, allowed: Iterable[int]) -> bool:
    return bool(member and (_member_role_ids(member) & set(int(x) for x in allowed)))


def make_staff_predicate():
    allowed_ids = _load_staff_ids()
    if not allowed_ids:
        async def _deny_all(_: discord.Interaction) -> bool:
            raise app_commands.CheckFailure("RBAC misconfigured: staff_role_ids is empty.")
        return _deny_all

    async def predicate(inter: discord.Interaction) -> bool:
        if inter.guild is None:
            raise app_commands.CheckFailure("Guild only.")
        member = inter.user if isinstance(inter.user, discord.Member) else inter.guild.get_member(inter.user.id)
        if member is None:
            try:
                member = await inter.guild.fetch_member(inter.user.id)
            except Exception:
                raise app_commands.CheckFailure("Unable to resolve your guild roles. Try again.")
        if _has_any_role(member, allowed_ids):
            return True
        raise app_commands.CheckFailure("You must hold a staff role to use this.")
    return predicate


def apply_staff_check_to_tree(tree: app_commands.CommandTree) -> None:
    pred = make_staff_predicate()
    for cmd in tree.walk_commands():
        if pred not in cmd.checks:
            cmd.add_check(pred)
