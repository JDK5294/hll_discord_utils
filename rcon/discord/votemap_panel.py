# rcon/discord/votemap_panel.py
from __future__ import annotations
import asyncio, logging
from typing import List, Optional, Any, Dict, Tuple
import discord
from discord import app_commands
from discord.ext import commands
import rcon.rcon as rcon
from lib.config import config

log = logging.getLogger(__name__)

def _pretty_of(x: Any) -> str:
    p = getattr(x, "pretty_name", None)
    if p: return str(p)
    if isinstance(x, dict): return str(x.get("pretty_name") or x.get("name") or x)
    return str(x)

def _id_of(x: Any) -> str:
    n = getattr(x, "id", None) or getattr(x, "name", None)
    if n: return str(n)
    if isinstance(x, dict): return str(x.get("id") or x.get("name") or x)
    return str(x)

def panel_guard(fn):
    async def _wrapped(self: "VoteMapPanel", *a, **kw):
        try: return await fn(self, *a, **kw)
        except asyncio.CancelledError: raise
        except Exception as e:
            log.exception("[Panel] Handler crashed: %s", e)
            try: await a[0].response.send_message(f"Panel error: {e}", ephemeral=True)
            except Exception: pass
    return _wrapped

class _OwnerView(discord.ui.View):
    def __init__(self, owner_id: int, *, show_settings: bool = False, timeout: Optional[float] = 1800):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.show_settings = show_settings
        self.pending_override_id: Optional[str] = None
        self.pending_override_pretty: Optional[str] = None
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.owner_id: return True
        try: await interaction.response.send_message("This panel belongs to another user. Use /votemap_panel_takeover to claim it.", ephemeral=True)
        except Exception: pass
        return False

class VoteMapPanel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active_owner_id: Optional[int] = None
        self._active_owner_display: Optional[str] = None
        self._override_id: Optional[str] = None
        self._override_pretty: Optional[str] = None
        self._override_task: Optional[asyncio.Task] = None
        self._resume_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        log.info("[Panel] VoteMapPanel.__init__ starting")
        log.info("[Panel] VoteMapPanel.__init__ finished")

    def _vm(self):
        vm = self.bot.get_cog("VoteMap")
        if vm is None: log.error("VoteMapPanel: VoteMap cog was not found.")
        return vm

    async def _notify_ephemeral(self, interaction: discord.Interaction, msg: str):
        if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
        else: await interaction.response.send_message(msg, ephemeral=True)

    def _claim_owner(self, user: discord.abc.User) -> None:
        self._active_owner_id = user.id
        self._active_owner_display = f"{user.name}"
        log.info("[Panel] Ownership claimed by %s (%s)", user.name, user.id)

    def _release_owner(self, reason: str = "manual"):
        prev = self._active_owner_display
        self._active_owner_id = None
        self._active_owner_display = None
        try:
            if self._override_task and not self._override_task.done(): self._override_task.cancel()
        except Exception: pass
        try:
            if self._resume_task and not self._resume_task.done(): self._resume_task.cancel()
        except Exception: pass
        log.info("[Panel] Ownership released (%s). Previous owner: %s", reason, prev or "none")

    def _owner_name(self) -> str:
        return self._active_owner_display or "Unknown"

    async def _status_line(self) -> str:
        bits = []
        try:
            ss = await rcon.get_Server_Status()
            if ss is not None:
                cur = getattr(ss, "current_players", "?"); cap = getattr(ss, "max_players", "?")
                bits.append(f"{cur}/{cap} players")
        except Exception: bits.append("Server status unknown")
        vm = self._vm()
        if vm:
            try:
                running = bool(getattr(vm, "vote_map_active", True))
                active = bool(getattr(vm, "vote_active", False))
                bits.append("Vote: paused" if not running else ("Vote: active" if active else "Vote: idle"))
            except Exception: pass
        if self._override_pretty: bits.append(f"Override scheduled: {self._override_prety}")  # typo fixed below on build
        if self._active_owner_id: bits.append(f"Owner: {self._owner_name()}")
        return " — ".join(bits) if bits else "Status unavailable"

    async def _settings_text(self) -> str:
        lines: List[str] = []
        try:
            ss = await rcon.get_Server_Status()
            if ss is not None:
                cur = getattr(ss, "current_players", "?"); cap = getattr(ss, "max_players", "?")
                lines.append(f"Server: online ({cur}/{cap})")
        except Exception: lines.append("Server: unknown")
        vm = self._vm()
        if vm:
            try:
                running = bool(getattr(vm, "vote_map_active", True))
                active = bool(getattr(vm, "vote_active", False))
                lines.append(f"Vote active: {running and active}")
            except Exception: lines.append("Vote active: unknown")
        try:
            activate_vote = config.get("rcon", 0, "map_vote", 0, "activate_vote")
            deactivate_vote = config.get("rcon", 0, "map_vote", 0, "dectivate_vote")
            reminder_min = config.get("rcon", 0, "map_vote", 0, "reminder", default=0)
            max_rem = config.get("rcon", 0, "map_vote", 0, "max_reminders_per_game", default=0)
            stealth = bool(config.get("rcon", 0, "map_vote", 0, "stealth_vote"))
            dryrun = bool(config.get("rcon", 0, "map_vote", 0, "dryrun"))
            duplicates = bool(config.get("rcon", 0, "map_vote", 0, "duplicate_maps"))
            lines.append(f"Activation: {activate_vote} / Deactivation: {deactivate_vote}")
            lines.append(f"Reminder: {reminder_min}m (max {max_rem or '∞'})")
            lines.append(f"Stealth vote: {'enabled' if stealth else 'disabled'}")
            lines.append(f"Dryrun: {'enabled' if dryrun else 'disabled'}")
            lines.append(f"Duplicate maps: {'on' if duplicates else 'off'}")
        except Exception as e: lines.append(f"(Settings read error: {e})")
        if self._override_pretty:
            lines.append(f"Override scheduled: {self._override_pretty} (applies when current poll ends)")
        return "\n".join(lines)

    async def _embed(self, user: discord.abc.User, show_settings: bool) -> discord.Embed:
        emb = discord.Embed(title="Map Vote Control Panel", description=await self._status_line())
        emb.set_footer(text=f"Opened by {user.name}")
        vm = self._vm()
        try:
            running = bool(getattr(vm, "vote_map_active", True)) if vm else False
            emb.color = discord.Color.blurple() if running else discord.Color.dark_gray()
        except Exception: emb.color = discord.Color.blurple()
        if show_settings: emb.add_field(name="Map Vote Settings", value=await self._settings_text(), inline=False)
        return emb

    async def _watch_for_match_end_then_resume(self, vm):
        if self._resume_task and not self._resume_task.done():
            self._resume_task.cancel()
            try: await self._resume_task
            except Exception: pass
        async def _runner():
            try:
                while True:
                    try: await vm.get_Game_State()
                    except Exception: pass
                    if getattr(vm, "game_active", None) is False: break
                    await asyncio.sleep(3)
                log.info("[Panel] Match ended — re-enabling map vote engine.")
                vm.vote_map_active = True
            except asyncio.CancelledError: pass
            except Exception as e: log.exception("[Panel] Resume watcher error: %s", e)
        self._resume_task = asyncio.create_task(_runner())

    async def _get_proposed_pairs(self, vm) -> List[Tuple[str, str]]:
        pairs: List[Tuple[str, str]] = []
        try:
            proposed = await vm.get_Maps_To_Vote()
            for m in proposed: pairs.append((_id_of(m), _pretty_of(m)))
        except Exception:
            try:
                if getattr(vm, "Maps", None) and getattr(vm.Maps, "maps", None):
                    for m in vm.Maps.maps: pairs.append((_id_of(m), _pretty_of(m)))
            except Exception: pass
        return pairs

    async def _get_live_counts(self, vm) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        try:
            results = await vm.get_Results()
            for item in results:
                key = _id_of(item)
                try:
                    v = int(item.get("votes") if isinstance(item, dict) else getattr(item, "votes", 0) or getattr(item, "count", 0) or 0)
                except Exception: v = 0
                counts[str(key)] = v
        except Exception: pass
        return counts

    async def _current_override_options(self) -> List[discord.SelectOption]:
        """Build dropdown options from the current poll’s maps (max 25)."""
        vm = self._vm()
        if vm is None or not getattr(vm, "vote_active", False) or not getattr(vm, "vote_msg", None): return []
        options: List[discord.SelectOption] = []
        try:
            if getattr(vm, "Maps", None) and getattr(vm.Maps, "maps", None):
                for m in vm.Maps.maps[:25]:
                    options.append(discord.SelectOption(label=_pretty_of(m), value=_id_of(m)))
        except Exception as e:
            log.exception("Building override options failed: %s", e)
        return options

    def _btn_reroll(self, *, row: int = 0) -> discord.ui.Button:
        outer = self
        class _Reroll(discord.ui.Button):
            def __init__(self, row: int): super().__init__(label="Reroll", style=discord.ButtonStyle.green, row=row, custom_id="vm_reroll")
            @panel_guard
            async def callback(self, interaction: discord.Interaction):
                vm = outer._vm()
                if vm is None: return await interaction.response.send_message("VoteMap not loaded.", ephemeral=True)
                if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
                async with outer._lock:
                    try:
                        log.info("[Panel] Reroll by %s", interaction.user)
                        if getattr(vm, "vote_active", False): await vm.stop_Vote()
                        while getattr(vm, "do_map_vote", False): await asyncio.sleep(0.25)
                        await vm.start_Vote()
                        outer._override_id = None; outer._override_pretty = None
                        if outer._override_task and not outer._override_task.done(): outer._override_task.cancel()
                        await outer._refresh(interaction, keep_settings=True)
                    except Exception as e:
                        log.exception("[Panel] Reroll failed: %s", e)
                        await interaction.followup.send(f"Reroll failed: {e}", ephemeral=True)
        return _Reroll(row=row)

    def _btn_pause_resume(self, *, row: int = 0) -> discord.ui.Button:
        outer = self
        class _PauseResume(discord.ui.Button):
            def __init__(self, row: int):
                label = "Pause Vote" if getattr(outer._vm(), "vote_map_active", True) else "Resume Vote"
                super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row, custom_id="vm_pause_resume")
            @panel_guard
            async def callback(self, interaction: discord.Interaction):
                vm = outer._vm()
                if vm is None: return await interaction.response.send_message("VoteMap not loaded.", ephemeral=True)
                if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
                async with outer._lock:
                    try:
                        if getattr(vm, "vote_map_active", True):
                            log.info("[Panel] Pause by %s", interaction.user)
                            vm.vote_map_active = False
                            while getattr(vm, "do_map_vote", False): await asyncio.sleep(0.25)
                            if getattr(vm, "seeded", False):
                                await vm.stop_Vote(); await vm.check_Origin_Map_Rotation()
                            await vm.clear_All_Messages(None, False); await vm.send_Pause_Message()
                            vm.reset_Vote_Variables()
                        else:
                            log.info("[Panel] Resume by %s", interaction.user)
                            vm.vote_map_active = True
                        await outer._refresh(interaction, keep_settings=True)
                    except Exception as e:
                        log.exception("[Panel] Pause/Resume failed: %s", e)
                        await interaction.followup.send(f"Pause/Resume failed: {e}", ephemeral=True)
        return _PauseResume(row=row)

    def _btn_restart(self, *, row: int = 0) -> discord.ui.Button:
        outer = self
        class _Restart(discord.ui.Button):
            def __init__(self, row: int): super().__init__(label="Restart Vote", style=discord.ButtonStyle.secondary, row=row, custom_id="vm_restart")
            @panel_guard
            async def callback(self, interaction: discord.Interaction):
                vm = outer._vm()
                if vm is None: return await interaction.response.send_message("VoteMap not loaded.", ephemeral=True)
                if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
                async with outer._lock:
                    try:
                        log.info("[Panel] Restart by %s", interaction.user)
                        snapshot = None
                        try:
                            if getattr(vm, "vote_active", False) and getattr(vm, "vote_msg", None):
                                snapshot = await vm.get_Maps_from_Vote()
                                if snapshot and getattr(snapshot, "maps", None):
                                    log.info("[Panel] Restart snapshot captured: %s", [getattr(m, "pretty_name", m) for m in snapshot.maps])
                        except Exception: snapshot = None
                        vm.vote_map_active = False
                        if getattr(vm, "vote_active", False): await vm.stop_Vote()
                        while getattr(vm, "do_map_vote", False): await asyncio.sleep(0.25)
                        await vm.clear_All_Messages(None, False)
                        vm.vote_active = False
                        orig_get = getattr(vm, "get_Maps_To_Vote", None)
                        async def _forced_get_Maps_To_Vote(_self=vm):
                            snap = getattr(_self, "_vp_force_snapshot", None)
                            if snap is not None:
                                setattr(_self, "_vp_force_snapshot", None)
                                return snap
                            return await orig_get()
                        if snapshot and getattr(snapshot, "maps", None) and orig_get:
                            setattr(vm, "_vp_force_snapshot", snapshot)
                            vm.get_Maps_To_Vote = _forced_get_Maps_To_Vote.__get__(vm, vm.__class__)
                        vm.vote_map_active = True
                        try: await vm.start_Vote()
                        finally:
                            if orig_get:
                                vm.get_Maps_To_Vote = orig_get
                                if hasattr(vm, "_vp_force_snapshot"): delattr(vm, "_vp_force_snapshot")
                        await outer._refresh(interaction, keep_settings=True)
                    except Exception as e:
                        log.exception("[Panel] Restart failed: %s", e)
                        await interaction.followup.send(f"Restart failed: {e}", ephemeral=True)
        return _Restart(row=row)

    def _btn_end_vote(self, *, row: int = 1) -> discord.ui.Button:
        outer = self
        class _End(discord.ui.Button):
            def __init__(self, row: int): super().__init__(label="End Current Vote", style=discord.ButtonStyle.secondary, row=row, custom_id="vm_end_vote")
            @panel_guard
            async def callback(self, interaction: discord.Interaction):
                vm = outer._vm()
                if vm is None: return await interaction.response.send_message("VoteMap not loaded.", ephemeral=True)
                if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
                async with outer._lock:
                    try:
                        if not getattr(vm, "vote_active", False) or not getattr(vm, "vote_msg", None):
                            return await interaction.followup.send("No active vote to end.", ephemeral=True)
                        log.info("[Panel] End Current Vote by %s", interaction.user)
                        await vm.stop_Vote(); vm.vote_map_active = False
                        await outer._watch_for_match_end_then_resume(vm)
                        await outer._refresh(interaction, keep_settings=True)
                    except Exception as e:
                        log.exception("[Panel] End Current Vote failed: %s", e)
                        await interaction.followup.send(f"End vote failed: {e}", ephemeral=True)
        return _End(row=row)

    def _btn_send_now(self, *, row: int = 1) -> discord.ui.Button:
        outer = self
        class SendNow(discord.ui.Button):
            def __init__(self, row: int): super().__init__(label="Send Vote Message", style=discord.ButtonStyle.secondary, row=row, custom_id="vm_send_vote_message")
            @panel_guard
            async def callback(self, interaction: discord.Interaction):
                if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
                vm = outer._vm()
                if vm is None: return await interaction.followup.send("VoteMap not loaded.", ephemeral=True)
                try:
                    pairs = await outer._get_proposed_pairs(vm)
                    if not pairs: return await interaction.followup.send("No maps available to send right now.", ephemeral=True)
                    counts = await outer._get_live_counts(vm); total = sum(counts.values()) if counts else 0
                    try: header = config.get("rcon", 0, "map_vote", 0, "vote_header")
                    except Exception: header = None
                    if not header: header = "Vote for the next map:"
                    lines: List[str] = []
                    for mid, pretty in pairs:
                        n = int(counts.get(mid, 0))
                        if total > 0:
                            pct = int(round((n/total)*100)); lines.append(f"• {pretty} — {n} vote{'s' if n!=1 else ''} ({pct}%)")
                        else:
                            lines.append(f"• {pretty} — {n} vote{'s' if n!=1 else ''}")
                    message = f"{header}\n" + "\n".join(lines)
                    sent_ok = 0; sent_fail = 0
                    try:
                        players = await rcon.get_Players()
                        seq = getattr(players, "players", None) or players.get("players", []) or []
                        for i, player in enumerate(seq, 1):
                            try:
                                pid = str(getattr(player, "player_id", "") or (player.get("player_id") if isinstance(player, dict) else "") or "")
                                if not pid: continue
                                await rcon.send_Player_Message({"player_id": pid, "message": message}); sent_ok += 1
                            except Exception:
                                sent_fail += 1
                            if i % 10 == 0: await asyncio.sleep(0.2)
                    except Exception: pass
                    if sent_ok == 0:
                        try: await rcon.send_Broadcast_Message({"message": message})
                        except Exception: pass
                    await interaction.followup.send(f"Manual vote reminder sent to {sent_ok} player(s)"+(f", {sent_fail} failed." if sent_fail else "."), ephemeral=True)
                    await interaction.followup.send(f"Preview of message sent:\n{message}", ephemeral=True)
                except Exception as e:
                    log.exception("Send vote message failed: %s", e)
                    await interaction.followup.send(f"Failed: {e}", ephemeral=True)
        return SendNow(row=row)

    def _btn_set_override(self, *, row: int = 3) -> discord.ui.Button:
        outer = self
        class _SetOverride(discord.ui.Button):
            def __init__(self, row: int): super().__init__(label="Set Override", style=discord.ButtonStyle.secondary, row=row, custom_id="vm_set_override")
            @panel_guard
            async def callback(self, interaction: discord.Interaction):
                vm = outer._vm()
                if vm is None: return await interaction.response.send_message("VoteMap not loaded.", ephemeral=True)
                if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
                v: _OwnerView = self.view  # type: ignore
                try:
                    if not getattr(vm, "vote_active", False) or not getattr(vm, "vote_msg", None):
                        return await interaction.followup.send("There’s no active vote to override right now.", ephemeral=True)
                    if not v.pending_override_id:
                        return await interaction.followup.send("Pick a map in the dropdown first.", ephemeral=True)
                    outer._override_id = v.pending_override_id
                    outer._override_pretty = v.pending_override_pretty or v.pending_override_id
                    setattr(vm, "_vp_override_id", outer._override_id); setattr(vm, "_vp_override_pretty", outer._override_pretty)
                    await outer._refresh(interaction, keep_settings=True)
                except Exception as e:
                    log.exception("[Panel] Set Override failed: %s", e)
                    await interaction.followup.send(f"Set Override failed: {e}", ephemeral=True)
        return _SetOverride(row=row)

    def _btn_clear_override(self, *, row: int = 3) -> discord.ui.Button:
        outer = self
        class _ClearOverride(discord.ui.Button):
            def __init__(self, row: int): super().__init__(label="Clear Override", style=discord.ButtonStyle.secondary, row=row, custom_id="vm_clear_override")
            @panel_guard
            async def callback(self, interaction: discord.Interaction):
                if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
                try:
                    outer._override_id = None; outer._override_pretty = None
                    if outer._override_task and not outer._override_task.done():
                        outer._override_task.cancel()
                        try: await outer._override_task
                        except Exception: pass
                    await interaction.followup.send("Override cleared.", ephemeral=True)
                    await outer._refresh(interaction, keep_settings=True)
                except Exception as e:
                    log.exception("[Panel] Clear Override failed: %s", e)
                    await interaction.followup.send(f"Clear Override failed: {e}", ephemeral=True)
        return _ClearOverride(row=row)

    def _btn_close(self, *, row: int = 4) -> discord.ui.Button:
        outer = self
        class _Close(discord.ui.Button):
            def __init__(self, row: int): super().__init__(label="Close Panel", style=discord.ButtonStyle.danger, row=row, custom_id="vm_close_panel")
            @panel_guard
            async def callback(self, interaction: discord.Interaction):
                if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
                try:
                    log.info("[Panel] Close button pressed by %s", interaction.user)
                    try: await interaction.delete_original_response()
                    except Exception:
                        try: await interaction.edit_original_response(view=None)
                        except Exception: pass
                    outer._release_owner(reason="button")
                except Exception as e:
                    log.exception("[Panel] Close panel failed: %s", e)
                    try: await interaction.followup.send(f"Close failed: {e}", ephemeral=True)
                    except Exception: pass
        return _Close(row=row)

    def _override_dropdown(self, options: List[discord.SelectOption], *, row: int = 2) -> discord.ui.Select:
        outer = self
        class _OverrideSelect(discord.ui.Select):
            def __init__(self, row: int):
                super().__init__(placeholder="Choose one current map…", min_values=1, max_values=1, options=options, row=row, custom_id="vm_override_select")
            @panel_guard
            async def callback(self, interaction: discord.Interaction):
                vm = outer._vm()
                if vm is None: return await interaction.response.send_message("VoteMap not loaded.", ephemeral=True)
                if not getattr(vm, "vote_active", False) or not getattr(vm, "vote_msg", None):
                    return await interaction.response.send_message("There’s no active vote to override right now.", ephemeral=True)
                selected_id = self.values[0]; pretty = None
                try:
                    if getattr(vm, "Maps", None) and getattr(vm.Maps, "maps", None):
                        for m in vm.Maps.maps[:25]:
                            if _id_of(m) == selected_id: pretty = _pretty_of(m); break
                except Exception: pass
                v: _OwnerView = self.view  # type: ignore
                v.pending_override_id = selected_id
                v.pending_override_pretty = pretty or selected_id
                log.info("[Panel] Override dropdown selected by %s -> %s", interaction.user, v.pending_override_pretty)
                await interaction.response.send_message(f"Ready to set override to **{v.pending_override_pretty}**. Click **Set Override** to confirm.", ephemeral=True)
        return _OverrideSelect(row=row)

    async def _view(self, owner_id: int, *, show_settings: bool) -> discord.ui.View:
        vm = self._vm()
        vote_active = bool(vm and getattr(vm, "vote_active", False))
        busy = bool(vm and getattr(vm, "do_map_vote", False))
        v = _OwnerView(owner_id, show_settings=show_settings)
        # Row 0
        b0 = self._btn_reroll(row=0); b0.disabled = busy
        b1 = self._btn_pause_resume(row=0); b1.disabled = busy
        b2 = self._btn_restart(row=0); b2.disabled = busy
        v.add_item(b0); v.add_item(b1); v.add_item(b2)
        # Row 1
        e1 = self._btn_end_vote(row=1); e1.disabled = busy or not vote_active
        s1 = self._btn_send_now(row=1); s1.disabled = busy
        v.add_item(e1); v.add_item(s1)
        # Row 2: dropdown alone
        if vote_active:
            opts = await self._current_override_options()
            if opts:
                v.add_item(self._override_dropdown(opts, row=2))
                # Row 3: set/clear
                setbtn = self._btn_set_override(row=3); setbtn.disabled = busy
                clrbtn = self._btn_clear_override(row=3); clrbtn.disabled = busy
                v.add_item(setbtn); v.add_item(clrbtn)
                # Row 4: close
                v.add_item(self._btn_close(row=4))
                return v
        # If no dropdown, put Close on row 3
        v.add_item(self._btn_close(row=3))
        return v

    async def _refresh(self, interaction: discord.Interaction, *, keep_settings: bool, force_show_settings: Optional[bool] = None):
        show_settings = force_show_settings if force_show_settings is not None else keep_settings
        owner_id = self._active_owner_id or interaction.user.id
        emb = await self._embed(interaction.user, show_settings=show_settings)
        view = await self._view(owner_id, show_settings=show_settings)
        if interaction.response.is_done(): await interaction.edit_original_response(embed=emb, view=view)
        else: await interaction.response.edit_message(embed=emb, view=view)

    @app_commands.command(name="votemap_panel", description="Open a private control panel for the map vote.")
    async def votemap_panel(self, interaction: discord.Interaction):
        try:
            if self._vm() is None: return await interaction.response.send_message("VoteMap cog not found.", ephemeral=True)
            if self._active_owner_id is None: self._claim_owner(interaction.user)
            if self._active_owner_id is not None and self._active_owner_id != interaction.user.id:
                log.info("[Panel] Open denied to %s — owner is %s", interaction.user, self._owner_name())
                return await interaction.response.send_message(f"This panel is currently controlled by **{self._owner_name()}**.\nUse **/votemap_panel_takeover** to take control.", ephemeral=True)
            self._claim_owner(interaction.user)
            emb = await self._embed(interaction.user, show_settings=False)
            view = await self._view(interaction.user.id, show_settings=False)
            await interaction.response.send_message(embed=emb, view=view, ephemeral=True)
            log.info("[Panel] Opened by %s (%s)", interaction.user.name, interaction.user.id)
        except Exception as e:
            log.exception("Opening panel failed: %s", e)
            if interaction.response.is_done(): await interaction.followup.send(f"Failed to open panel: {e}", ephemeral=True)
            else: await interaction.response.send_message(f"Failed to open panel: {e}", ephemeral=True)

    @app_commands.command(name="votemap_panel_takeover", description="Claim ownership of the VoteMap control panel.")
    async def votemap_panel_takeover(self, interaction: discord.Interaction):
        try:
            prev = self._owner_name() if self._active_owner_id else None
            self._claim_owner(interaction.user)
            note = "You now control the panel."
            if prev and interaction.user.name != prev: note += f" (Previous owner: **{prev}**)"
            await interaction.response.send_message(note, ephemeral=True)
            log.info("[Panel] Takeover by %s (previous owner: %s)", interaction.user.name, prev or "none")
        except Exception as e:
            log.exception("Takeover failed: %s", e)
            await interaction.response.send_message(f"Failed to take over: {e}", ephemeral=True)

    @app_commands.command(name="votemap_panel_release", description="Release ownership of the VoteMap control panel.")
    async def votemap_panel_release(self, interaction: discord.Interaction):
        try:
            prev = self._owner_name() if self._active_owner_id else None
            self._release_owner(reason="slash")
            await interaction.response.send_message("You have released control of the panel.", ephemeral=True)
            log.info("[Panel] Release via slash by %s (previous owner: %s)", interaction.user.name, prev or "none")
        except Exception as e:
            log.exception("Release failed: %s", e)
            await interaction.response.send_message(f"Failed to release panel: {e}", ephemeral=True)

    async def cog_load(self):
        log.info("VoteMapPanel loaded (no self-sync).")

async def setup(bot: commands.Bot):
    try:
        log.info("[Panel] setup() called; attempting to add cog.")
        await bot.add_cog(VoteMapPanel(bot))
        log.info("[Panel] setup() completed; cog added.")
    except Exception as e:
        log.exception("[Panel] setup() failed while adding cog: %s", e)
        raise
