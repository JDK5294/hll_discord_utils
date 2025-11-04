# rcon/discord/bot.py
import logging
import threading
import asyncio
import time as _time
import discord
from discord.ext import commands
from discord import app_commands

from lib.config import config
from rcon.discord.serverstatus import ServerStatus
from rcon.discord.maprotation import MapRotation
from rcon.discord.balance import Balance
from rcon.discord.votemap import VoteMap
from rcon.discord.autolevel import AutoLevel
from rcon.discord.comfort import Comfort
from rcon.discord.artillerycalculator import ArtilleryCalculator
from rcon.discord.registration import Registration
from rcon.discord.unregister import Unregister
from rcon.discord.rbac import apply_staff_check_to_tree  # global RBAC

try:
    from rcon.discord.votemap_panel import VoteMapPanel
except Exception:
    VoteMapPanel = None

import rcon.rcon as _rcon

# Preserve originals to avoid recursion when patching
_ORIG_get_Recent_Logs = _rcon.get_Recent_Logs
_ORIG_get_Server_Status = _rcon.get_Server_Status
_ORIG_get_Maps = _rcon.get_Maps
_ORIG_get_Map_History = _rcon.get_Map_History
_ORIG_get_Current_Map = _rcon.get_Current_Map
_ORIG_set_Map_Rotation = _rcon.set_Map_Rotation
_ORIG_get_Players = _rcon.get_Players
_ORIG_send_Player_Message = _rcon.send_Player_Message

_RCON_BACKOFF_UNTIL = 0.0

def _rcon_backing_off():
    return _time.time() < _RCON_BACKOFF_UNTIL

def _rcon_fail():
    global _RCON_BACKOFF_UNTIL
    _RCON_BACKOFF_UNTIL = _time.time() + 10

async def _safe_get_Recent_Logs(payload, model_cls):
    if _rcon_backing_off():
        class _Dummy: logs = []
        return _Dummy()
    try:
        return await _ORIG_get_Recent_Logs(payload, model_cls)
    except Exception as e:
        logger.error(f"RCON get_Recent_Logs failed: {e}")
        _rcon_fail()
        class _Dummy: logs = []
        return _Dummy()

async def _safe_get_Server_Status():
    if _rcon_backing_off():
        return None
    try:
        return await _ORIG_get_Server_Status()
    except Exception as e:
        logger.error(f"RCON get_Server_Status failed: {e}")
        _rcon_fail()
        return None

async def _safe_get_Maps():
    if _rcon_backing_off():
        return None
    try:
        return await _ORIG_get_Maps()
    except Exception as e:
        logger.error(f"RCON get_Maps failed: {e}")
        _rcon_fail()
        return None

async def _safe_get_Map_History(n):
    if _rcon_backing_off():
        return []
    try:
        return await _ORIG_get_Map_History(n)
    except Exception as e:
        logger.error(f"RCON get_Map_History failed: {e}")
        _rcon_fail()
        return []

async def _safe_get_Current_Map():
    if _rcon_backing_off():
        return None
    try:
        return await _ORIG_get_Current_Map()
    except Exception as e:
        logger.error(f"RCON get_Current_Map failed: {e}")
        _rcon_fail()
        return None

async def _safe_set_Map_Rotation(payload):
    if _rcon_backing_off():
        return None
    try:
        return await _ORIG_set_Map_Rotation(payload)
    except Exception as e:
        logger.error(f"RCON set_Map_Rotation failed: {e}")
        _rcon_fail()
        return None

async def _safe_get_Players():
    if _rcon_backing_off():
        return None
    try:
        return await _ORIG_get_Players()
    except Exception as e:
        logger.error(f"RCON get_Players failed: {e}")
        _rcon_fail()
        return None

async def _safe_send_Player_Message(data):
    if _rcon_backing_off():
        return None
    try:
        return await _ORIG_send_Player_Message(data)
    except Exception as e:
        logger.error(f"RCON send_Player_Message failed: {e}")
        _rcon_fail()
        return None

# safe wrappers
_rcon.get_Recent_Logs = _safe_get_Recent_Logs
_rcon.get_Server_Status = _safe_get_Server_Status
_rcon.get_Maps = _safe_get_Maps
_rcon.get_Map_History = _safe_get_Map_History
_rcon.get_Current_Map = _safe_get_Current_Map
_rcon.set_Map_Rotation = _safe_set_Map_Rotation
_rcon.get_Players = _safe_get_Players
_rcon.send_Player_Message = _safe_send_Player_Message

logger = logging.getLogger(__name__)

class MainBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="/", intents=intents)
        self.shutdown_event = asyncio.Event()

    async def on_ready(self):
        logger.info(f'Logged in as {self.user} (ID: {self.user.id})')
        while not self.shutdown_event.is_set():
            await asyncio.sleep(5)

    async def setup_hook(self):
        # Load cogs first so commands exist
        if config.get("rcon", 0, "server_status", 0, "enabled"):
            logger.info("Start server status")
            await self.add_cog(ServerStatus(self)) 

        if config.get("rcon", 0, "map_rotation", 0, "enabled"):
            logger.info("Start map rotation")
            await self.add_cog(MapRotation(self)) 
        
        if config.get("rcon", 0, "server_balance", 0, "enabled"):
            logger.info("Start server balance")
            await self.add_cog(Balance(self)) 
        
        if config.get("rcon", 0, "map_vote", 0, "enabled"):
            logger.info("Start map vote")
            await self.add_cog(VoteMap(self)) 

        if config.get("rcon", 0, "auto_level", 0, "enabled"):
            logger.info("Start auto level")
            await self.add_cog(AutoLevel(self)) 

        if config.get("rcon", 0, "comfort_functions", 0, "enabled"):
            logger.info("Start comfort functions")
            await self.add_cog(Comfort(self))

        if config.get("rcon", 0, "register_player", 0, "enabled"):
            logger.info("Start registration functions")
            await self.add_cog(Registration(self))
            await self.add_cog(Unregister(self))

        if config.get("rcon", 0, "artillery_calculator", 0, "enabled"):
            logger.info("Start auto artillery calculator")
            await self.add_cog(ArtilleryCalculator(self))             

        if VoteMapPanel is not None:
            try:
                await self.add_cog(VoteMapPanel(self))
                logger.info("VoteMapPanel loaded.")
            except Exception as e:
                logger.warning(f"VoteMapPanel could not be loaded (continuing without it): {e}")
        else:
            logger.info("VoteMapPanel not present; continuing without it.")

        # Attach staff predicate to every slash command, then sync
        apply_staff_check_to_tree(self.tree)

        @self.tree.error
        async def _on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            if isinstance(error, app_commands.CheckFailure):
                msg = str(error) if str(error) else "You must hold a staff role to use this."
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(msg, ephemeral=True)
                    else:
                        await interaction.followup.send(msg, ephemeral=True)
                except Exception:
                    pass

        await self.tree.sync()
        logger.info("Slash commands have been synced.")

    def run_bot(self):
        self.tree.clear_commands(guild=discord.Object(id=1299285373855203349))
        logger.info("Slash commands have been synced.")
        token = config.get("rcon", 0, "discord_token")
        self.run(token)

    def shutdown_bot(self):
        asyncio.run_coroutine_threadsafe(self.close(), self.loop)

bot = None
bot_thread = None

def start_bot():
    global bot
    global bot_thread

    bot = MainBot()
    bot_thread = threading.Thread(target=bot.run_bot)
    bot_thread.start()

def shutdown_bot():
    global bot
    global bot_thread

    bot.shutdown_bot()
    bot_thread.join()
