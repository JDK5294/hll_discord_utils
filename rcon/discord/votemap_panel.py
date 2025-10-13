from __future__ import annotations
import asyncio
import logging
from typing import List, Optional
import time
import discord
from discord import app_commands
from discord.ext import commands
import rcon.rcon as rcon
from lib.config import config

log = logging.getLogger(__name__)

class _OwnerView(discord.ui.View):
    def __init__(self, owner_id: int, *, timeout: float | None = 1800):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.pending_override_id: str | None = None
        self.pending_override_pretty: str | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("This panel currently belongs to another user. Use /votemap_panel_takeover to take control.", ephemeral=True)
        return False

class VoteMapPanel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active_owner_id: Optional[int] = None
        self._active_owner_display: Optional[str] = None
        self._override_id: Optional[str] = None
        self._override_pretty: Optional[str] = None
        self._override_poll_msg_id: Optional[int] = None
        self._override_task: Optional[asyncio.Task] = None
        self._resume_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._broadcast_in_progress: bool = False
        self._broadcast_cooldown_until: float = 0.0

    def _vm(self):
        vm = self.bot.get_cog("VoteMap")
        if vm is None:
            log.error("VoteMapPanel: VoteMap cog was not found.")
        return vm

    def _claim_owner(self, user: discord.abc.User) -> None:
        self._active_owner_id = user.id
        self._active_owner_display = f"{user.name}"
        log.info("[Panel] Ownership claimed by %s (%s)", user.name, user.id)

    def _release_owner(self, reason: str = "manual"):
        prev = self._active_owner_display
        self._active_owner_id = None
        self._active_owner_display = None
        try:
            if self._resume_task and not self._resume_task.done():
                self._resume_task.cancel()
        except Exception:
            pass
        log.info("[Panel] Ownership released (%s). Previous owner: %s", reason, prev or "none")

    def _owner_name(self) -> str:
        return self._active_owner_display or "Unknown"

    async def _current_override_options(self) -> List[discord.SelectOption]:
        vm = self._vm()
        if vm is None:
            return []
        if not getattr(vm, "vote_active", False) or not getattr(vm, "vote_msg", None):
            return []
        options: List[discord.SelectOption] = []
        try:
            if getattr(vm, "Maps", None) and getattr(vm.Maps, "maps", None):
                for m in vm.Maps.maps[:25]:
                    options.append(discord.SelectOption(label=m.pretty_name, value=m.id))
        except Exception as e:
            log.exception("Building override options failed: %s", e)
        return options

    async def _status_line(self) -> str:
        bits = []
        try:
            ss = await rcon.get_Server_Status()
            if ss is not None:
                cur = getattr(ss, "current_players", "?")
                cap = getattr(ss, "max_players", "?")
                bits.append(f"Server online — {cur}/{cap} players")
            else:
                bits.append("Server unreachable")
        except Exception:
            bits.append("Server status unknown")
        vm = self._vm()
        if vm:
            try:
                running = bool(getattr(vm, "vote_map_active", True))
                active = bool(getattr(vm, "vote_active", False))
                bits.append("Vote: paused" if not running else ("Vote: active" if active else "Vote: idle"))
            except Exception:
                pass
        if self._override_pretty:
            bits.append(f"Override scheduled: {self._override_pretty} (this poll only)")
        if self._active_owner_id:
            bits.append(f"Panel owner: {self._owner_name()}")
        return " — ".join(bits) if bits else "Status unavailable"

    async def _embed(self, user: discord.abc.User) -> discord.Embed:
        emb = discord.Embed(title="Map Vote Control Panel", description=await self._status_line())
        emb.set_footer(text=f"Opened by {user.name}")
        vm = self._vm()
        try:
            running = bool(getattr(vm, "vote_map_active", True)) if vm else False
            emb.color = discord.Color.blurple() if running else discord.Color.dark_gray()
        except Exception:
            emb.color = discord.Color.blurple()
        return emb

    async def _watch_for_match_end_then_resume(self, vm):
        if self._resume_task and not self._resume_task.done():
            self._resume_task.cancel()
            try:
                await self._resume_task
            except Exception:
                pass
        async def _runner():
            try:
                while True:
                    try:
                        await vm.get_Game_State()
                    except Exception:
                        pass
                    if getattr(vm, "game_active", None) is False:
                        break
                    await asyncio.sleep(3)
                log.info("[Panel] Match ended — re-enabling map vote engine.")
                vm.vote_map_active = True
            except asyncio.CancelledError:
                log.info("[Panel] Resume watcher cancelled.")
            except Exception as e:
                log.exception("[Panel] Resume watcher error: %s", e)
        self._resume_task = asyncio.create_task(_runner())

    async def _watch_for_poll_end_then_apply_override(self, vm):
        if self._override_task and not self._override_task.done():
            self._override_task.cancel()
            try:
                await self._override_task
            except Exception:
                pass
        override_id = self._override_id
        override_pretty = self._override_pretty
        initial_msg_id = getattr(getattr(vm, "vote_msg", None), "id", None)
        self._override_poll_msg_id = initial_msg_id
        async def _runner():
            try:
                while True:
                    vote_active = bool(getattr(vm, "vote_active", False))
                    vote_msg = getattr(vm, "vote_msg", None)
                    if vote_msg and initial_msg_id and vote_msg.id != initial_msg_id:
                        log.info("[Panel] New poll detected — clearing override (previous was bound to %s).", initial_msg_id)
                        self._clear_override_internal()
                        return
                    if not vote_active:
                        break
                    if vote_msg and getattr(vote_msg, "poll", None):
                        try:
                            if vote_msg.poll.is_finalised():
                                break
                        except Exception:
                            pass
                    await asyncio.sleep(2)
                if override_id:
                    try:
                        await vm.set_Map(override_id)
                        log.info("[Panel] Deferred override applied once: %s", override_pretty or override_id)
                    except Exception as e:
                        log.warning("[Panel] Deferred override failed: %s", e)
                self._clear_override_internal()
            except asyncio.CancelledError:
                log.info("[Panel] Override watcher cancelled.")
            except Exception as e:
                log.exception("[Panel] Override watcher error: %s", e)
        self._override_task = asyncio.create_task(_runner())

    def _clear_override_internal(self):
        self._override_id = None
        self._override_pretty = None
        self._override_poll_msg_id = None
        try:
            if self._override_task and not self._override_task.done():
                self._override_task.cancel()
        except Exception:
            pass

    def _btn_reroll(self, *, row: int = 0) -> discord.ui.Button:
        outer = self
        class _Reroll(discord.ui.Button):
            def __init__(self, row: int):
                super().__init__(label="Reroll", style=discord.ButtonStyle.green, row=row, custom_id="vm_reroll")
            async def callback(self, itx: discord.Interaction):
                vm = outer._vm()
                if vm is None:
                    return await itx.response.send_message("VoteMap not loaded.", ephemeral=True)
                if not itx.response.is_done():
                    await itx.response.defer(ephemeral=True)
                async with outer._lock:
                    try:
                        log.info("[Panel] Reroll by %s", itx.user)
                        if getattr(vm, "vote_active", False):
                            await vm.stop_Vote()
                        while getattr(vm, "do_map_vote", False):
                            await asyncio.sleep(0.25)
                        await vm.start_Vote()
                        outer._clear_override_internal()
                        await outer._refresh(itx)
                    except Exception as e:
                        log.exception("[Panel] Reroll failed: %s", e)
                        await itx.followup.send(f"Reroll failed: {e}", ephemeral=True)
        return _Reroll(row=row)

    def _btn_pause_resume(self, *, row: int = 0) -> discord.ui.Button:
        outer = self
        class _PauseResume(discord.ui.Button):
            def __init__(self, row: int):
                label = "Pause Vote" if getattr(outer._vm(), "vote_map_active", True) else "Resume Vote"
                super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row, custom_id="vm_pause_resume")
            async def callback(self, itx: discord.Interaction):
                vm = outer._vm()
                if vm is None:
                    return await itx.response.send_message("VoteMap not loaded.", ephemeral=True)
                if not itx.response.is_done():
                    await itx.response.defer(ephemeral=True)
                async with outer._lock:
                    try:
                        if getattr(vm, "vote_map_active", True):
                            log.info("[Panel] Pause by %s", itx.user)
                            vm.vote_map_active = False
                            while getattr(vm, "do_map_vote", False):
                                await asyncio.sleep(0.25)
                            if getattr(vm, "seeded", False):
                                await vm.stop_Vote()
                                await vm.check_Origin_Map_Rotation()
                            await vm.clear_All_Messages(None, False)
                            await vm.send_Pause_Message()
                            vm.reset_Vote_Variables()
                        else:
                            log.info("[Panel] Resume by %s", itx.user)
                            vm.vote_map_active = True
                        await outer._refresh(itx)
                    except Exception as e:
                        log.exception("[Panel] Pause/Resume failed: %s", e)
                        await itx.followup.send(f"Pause/Resume failed: {e}", ephemeral=True)
        return _PauseResume(row=row)

    def _btn_restart(self, *, row: int = 0) -> discord.ui.Button:
        outer = self
        class _Restart(discord.ui.Button):
            def __init__(self, row: int):
                super().__init__(label="Restart Vote", style=discord.ButtonStyle.secondary, row=row, custom_id="vm_restart")
            async def callback(self, itx: discord.Interaction):
                vm = outer._vm()
                if vm is None:
                    return await itx.response.send_message("VoteMap not loaded.", ephemeral=True)
                if not itx.response.is_done():
                    await itx.response.defer(ephemeral=True)
                async with outer._lock:
                    try:
                        log.info("[Panel] Restart by %s", itx.user)
                        same_maps = None
                        try:
                            if getattr(vm, "vote_active", False) and getattr(vm, "vote_msg", None):
                                same_maps = await vm.get_Maps_from_Vote()
                        except Exception:
                            same_maps = None
                        if getattr(vm, "vote_active", False):
                            await vm.stop_Vote()
                        while getattr(vm, "do_map_vote", False):
                            await asyncio.sleep(0.25)
                        if same_maps and getattr(same_maps, "maps", None):
                            vm.Maps = same_maps
                            await vm.start_Vote()
                        else:
                            await vm.start_Vote()
                        outer._clear_override_internal()
                        await outer._refresh(itx)
                    except Exception as e:
                        log.exception("[Panel] Restart failed: %s", e)
                        await itx.followup.send(f"Restart failed: {e}", ephemeral=True)
        return _Restart(row=row)

    def _btn_end_vote(self, *, row: int = 1) -> discord.ui.Button:
        outer = self
        class _End(discord.ui.Button):
            def __init__(self, row: int):
                super().__init__(label="End Current Vote", style=discord.ButtonStyle.secondary, row=row, custom_id="vm_end_vote")
            async def callback(self, itx: discord.Interaction):
                vm = outer._vm()
                if vm is None:
                    return await itx.response.send_message("VoteMap not loaded.", ephemeral=True)
                if not itx.response.is_done():
                    await itx.response.defer(ephemeral=True)
                async with outer._lock:
                    try:
                        if not getattr(vm, "vote_active", False) or not getattr(vm, "vote_msg", None):
                            return await itx.followup.send("No active vote to end.", ephemeral=True)
                        log.info("[Panel] End Current Vote by %s", itx.user)
                        await vm.stop_Vote()
                        vm.vote_map_active = False
                        await outer._watch_for_match_end_then_resume(vm)
                        await outer._refresh(itx)
                    except Exception as e:
                        log.exception("[Panel] End Current Vote failed: %s", e)
                        await itx.followup.send(f"End vote failed: {e}", ephemeral=True)
        return _End(row=row)

    def _btn_send_vote_message(self, *, row: int = 1) -> discord.ui.Button:
        outer = self
        class _SendVoteMsg(discord.ui.Button):
            def __init__(self, row: int):
                super().__init__(label="Send Vote Message", style=discord.ButtonStyle.primary, row=row, custom_id="vm_send_vote_msg")
            async def callback(self, itx: discord.Interaction):
                now = time.time()
                if outer._broadcast_in_progress or now < outer._broadcast_cooldown_until:
                    if not itx.response.is_done():
                        await itx.response.defer(ephemeral=True)
                    return
                vm = outer._vm()
                if vm is None:
                    return await itx.response.send_message("VoteMap not loaded.", ephemeral=True)
                outer._broadcast_in_progress = True
                try:
                    await outer._refresh(itx)
                except Exception:
                    pass
                if not itx.response.is_done():
                    await itx.response.defer(ephemeral=True)
                async with outer._lock:
                    try:
                        header = config.get("rcon", 0, "map_vote", 0, "vote_header") or "Vote for the next map:"
                        text = ""
                        maps_obj = getattr(vm, "Maps", None)
                        if not (maps_obj and getattr(maps_obj, "maps", None)):
                            try:
                                maps_obj = await vm.get_Maps_from_Vote()
                            except Exception:
                                maps_obj = None
                        if maps_obj and getattr(maps_obj, "maps", None):
                            for m in maps_obj.maps:
                                text += f"{m.pretty_name}\n"
                        else:
                            outer._broadcast_in_progress = False
                            await outer._refresh(itx)
                            return await itx.followup.send("No current map list available to broadcast.", ephemeral=True)
                        dryrun = bool(config.get("rcon", 0, "map_vote", 0, "dryrun"))
                        if dryrun:
                            log.info("[Panel] Dry run active — not sending vote messages.")
                            outer._broadcast_in_progress = False
                            await outer._refresh(itx)
                            return await itx.followup.send("Dry run enabled. No messages sent.", ephemeral=True)
                        players = await rcon.get_Players()
                        sent = 0
                        if players and getattr(players, "players", None):
                            for p in players.players:
                                data = {"player_id": str(p.player_id), "message": f"{header}\n\n{text}"}
                                try:
                                    await rcon.send_Player_Message(data)
                                    sent += 1
                                except Exception as e:
                                    log.warning("Failed sending vote message to %s (%s): %s", getattr(p, "name", "?"), p.player_id, e)
                                await asyncio.sleep(0)
                        await itx.followup.send(f"Vote message sent to {sent} player(s).", ephemeral=True)
                        log.info("[Panel] Vote message broadcast complete (%d recipients).", sent)
                    except Exception as e:
                        log.exception("[Panel] Send Vote Message failed: %s", e)
                        try:
                            await itx.followup.send(f"Send Vote Message failed: {e}", ephemeral=True)
                        except Exception:
                            pass
                    finally:
                        outer._broadcast_in_progress = False
                        outer._broadcast_cooldown_until = time.time() + 5.0
                        try:
                            await outer._refresh(itx)
                        except Exception:
                            pass
        return _SendVoteMsg(row=row)

    def _btn_set_override(self, *, row: int = 3) -> discord.ui.Button:
        outer = self
        class _SetOverride(discord.ui.Button):
            def __init__(self, row: int):
                super().__init__(label="Set Override", style=discord.ButtonStyle.secondary, row=row, custom_id="vm_set_override")
            async def callback(self, itx: discord.Interaction):
                vm = outer._vm()
                if vm is None:
                    return await itx.response.send_message("VoteMap not loaded.", ephemeral=True)
                if not itx.response.is_done():
                    await itx.response.defer(ephemeral=True)
                v: _OwnerView = self.view  # type: ignore
                try:
                    if not getattr(vm, "vote_active", False) or not getattr(vm, "vote_msg", None):
                        return
                    try:
                        if vm.vote_msg and vm.vote_msg.poll and vm.vote_msg.poll.is_finalised():
                            return
                    except Exception:
                        pass
                    if not v.pending_override_id:
                        return
                    outer._override_id = v.pending_override_id
                    outer._override_pretty = v.pending_override_pretty or v.pending_override_id
                    outer._override_poll_msg_id = getattr(getattr(vm, "vote_msg", None), "id", None)
                    await outer._watch_for_poll_end_then_apply_override(vm)
                    v.pending_override_id = None
                    v.pending_override_pretty = None
                    log.info("[Panel] Override scheduled by %s → %s (poll %s)", itx.user, outer._override_pretty, outer._override_poll_msg_id)
                    await outer._refresh(itx)
                except Exception as e:
                    log.exception("[Panel] Set Override failed: %s", e)
        return _SetOverride(row=row)

    def _btn_clear_override(self, *, row: int = 3) -> discord.ui.Button:
        outer = self
        class _ClearOverride(discord.ui.Button):
            def __init__(self, row: int):
                super().__init__(label="Clear Override", style=discord.ButtonStyle.secondary, row=row, custom_id="vm_clear_override")
            async def callback(self, itx: discord.Interaction):
                if not itx.response.is_done():
                    await itx.response.defer(ephemeral=True)
                try:
                    outer._clear_override_internal()
                    log.info("[Panel] Override cleared by %s", itx.user)
                    await itx.followup.edit_message(
                        message_id=(await itx.original_response()).id,
                        embed=await outer._embed(itx.user),
                        view=await outer._view(outer._active_owner_id or itx.user.id),
                    )
                except Exception as e:
                    log.exception("[Panel] Clear Override failed: %s", e)
        return _ClearOverride(row=row)

    def _btn_close(self, *, row: int = 4) -> discord.ui.Button:
        outer = self
        class _Close(discord.ui.Button):
            def __init__(self, row: int):
                super().__init__(label="Close Panel", style=discord.ButtonStyle.danger, row=row, custom_id="vm_close_panel")
            async def callback(self, itx: discord.Interaction):
                if not itx.response.is_done():
                    await itx.response.defer(ephemeral=True)
                try:
                    log.info("[Panel] Close button pressed by %s", itx.user)
                    try:
                        await itx.delete_original_response()
                    except Exception:
                        try:
                            await itx.edit_original_response(view=None)
                        except Exception:
                            pass
                    outer._release_owner(reason="button")
                except Exception as e:
                    log.exception("[Panel] Close panel failed: %s", e)
                    try:
                        await itx.followup.send(f"Close failed: {e}", ephemeral=True)
                    except Exception:
                        pass
        return _Close(row=row)

    def _override_dropdown(self, options: List[discord.SelectOption], *, row: int = 2) -> discord.ui.Select:
        outer = self
        class _OverrideSelect(discord.ui.Select):
            def __init__(self, row: int):
                super().__init__(placeholder="Choose one current map…", min_values=1, max_values=1, options=options, row=row, custom_id="vm_override_select")
            async def callback(self, itx: discord.Interaction):
                vm = outer._vm()
                if vm is None:
                    if not itx.response.is_done():
                        await itx.response.defer(ephemeral=True)
                    return
                if not getattr(vm, "vote_active", False) or not getattr(vm, "vote_msg", None):
                    if not itx.response.is_done():
                        await itx.response.defer(ephemeral=True)
                    return
                selected_id = self.values[0]
                pretty = None
                try:
                    if getattr(vm, "Maps", None) and getattr(vm.Maps, "maps", None):
                        for m in vm.Maps.maps:
                            if m.id == selected_id:
                                pretty = m.pretty_name
                                break
                except Exception:
                    pass
                v: _OwnerView = self.view  # type: ignore
                v.pending_override_id = selected_id
                v.pending_override_pretty = pretty or selected_id
                if not itx.response.is_done():
                    await itx.response.defer(ephemeral=True)
        return _OverrideSelect(row=row)

    async def _view(self, owner_id: int) -> discord.ui.View:
        vm = self._vm()
        vote_active = bool(vm and getattr(vm, "vote_active", False))
        busy = bool(vm and getattr(vm, "do_map_vote", False))
        v = _OwnerView(owner_id)
        reroll = self._btn_reroll(row=0); reroll.disabled = busy
        pause = self._btn_pause_resume(row=0); pause.disabled = busy
        rest = self._btn_restart(row=0); rest.disabled = busy
        v.add_item(reroll); v.add_item(pause); v.add_item(rest)
        endbtn = self._btn_end_vote(row=1); endbtn.disabled = busy or not vote_active
        sendvote = self._btn_send_vote_message(row=1); sendvote.disabled = busy or self._broadcast_in_progress or (time.time() < self._broadcast_cooldown_until)
        v.add_item(endbtn); v.add_item(sendvote)
        if vote_active:
            opts = await self._current_override_options()
            if opts:
                sel = self._override_dropdown(opts, row=2); sel.disabled = busy
                v.add_item(sel)
                setbtn = self._btn_set_override(row=3); setbtn.disabled = busy
                clrbtn = self._btn_clear_override(row=3); clrbtn.disabled = busy
                v.add_item(setbtn); v.add_item(clrbtn)
        v.add_item(self._btn_close(row=4))
        return v

    async def _refresh(self, itx: discord.Interaction):
        owner_id = self._active_owner_id or itx.user.id
        emb = await self._embed(itx.user)
        view = await self._view(owner_id)
        if itx.response.is_done():
            await itx.edit_original_response(embed=emb, view=view)
        else:
            await itx.response.edit_message(embed=emb, view=view)

    @app_commands.command(name="votemap_panel", description="Open a private control panel for the map vote.")
    async def votemap_panel(self, itx: discord.Interaction):
        try:
            if self._vm() is None:
                return await itx.response.send_message("VoteMap cog not found.", ephemeral=True)
            if self._active_owner_id is None:
                self._claim_owner(itx.user)
            if self._active_owner_id is not None and self._active_owner_id != itx.user.id:
                log.info("[Panel] Open denied to %s — owner is %s", itx.user, self._owner_name())
                return await itx.response.send_message(f"This panel is currently controlled by {self._owner_name()}. Use /votemap_panel_takeover to take control.", ephemeral=True)
            self._claim_owner(itx.user)
            emb = await self._embed(itx.user)
            view = await self._view(itx.user.id)
            await itx.response.send_message(embed=emb, view=view, ephemeral=True)
            log.info("[Panel] Opened by %s (%s)", itx.user.name, itx.user.id)
        except Exception as e:
            log.exception("Opening panel failed: %s", e)
            if itx.response.is_done():
                await itx.followup.send(f"Failed to open panel: {e}", ephemeral=True)
            else:
                await itx.response.send_message(f"Failed to open panel: {e}", ephemeral=True)

    @app_commands.command(name="votemap_panel_takeover", description="Claim ownership of the VoteMap control panel.")
    async def votemap_panel_takeover(self, itx: discord.Interaction):
        try:
            prev = self._owner_name() if self._active_owner_id else None
            self._claim_owner(itx.user)
            note = "You now control the panel."
            if prev and itx.user.name != prev:
                note += f" (Previous owner: {prev})"
            await itx.response.send_message(note, ephemeral=True)
            log.info("[Panel] Takeover by %s (previous owner: %s)", itx.user.name, prev or "none")
        except Exception as e:
            log.exception("Takeover failed: %s", e)
            if itx.response.is_done():
                await itx.followup.send(f"Failed to take over: {e}", ephemeral=True)
            else:
                await itx.response.send_message(f"Failed to take over: {e}", ephemeral=True)

    @app_commands.command(name="votemap_panel_release", description="Release ownership of the VoteMap control panel.")
    async def votemap_panel_release(self, itx: discord.Interaction):
        try:
            prev = self._owner_name() if self._active_owner_id else None
            self._release_owner(reason="slash")
            await itx.response.send_message("You have released control of the panel.", ephemeral=True)
            log.info("[Panel] Release via slash by %s (previous owner: %s)", itx.user.name, prev or "none")
        except Exception as e:
            log.exception("Release failed: %s", e)
            if itx.response.is_done():
                await itx.followup.send(f"Failed to release panel: {e}", ephemeral=True)
            else:
                await itx.response.send_message(f"Failed to release panel: {e}", ephemeral=True)

    async def cog_load(self):
        log.info("VoteMapPanel loaded.")
