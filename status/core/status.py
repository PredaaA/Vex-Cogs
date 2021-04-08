import asyncio
import logging
from copy import deepcopy
from time import time

import aiohttp
from redbot.core import Config, commands
from redbot.core.bot import Red
from vexcogutils import format_help, format_info

from ..commands.status_com import StatusCom
from ..commands.statusdev_com import StatusDevCom
from ..commands.statusset_com import StatusSetCom
from ..objects.caches import LastChecked, ServiceCooldown, ServiceRestrictionsCache, UsedFeeds
from ..objects.configwrapper import ConfigWrapper
from ..updateloop.sendupdate import SendUpdate
from ..updateloop.updatechecker import UpdateChecker
from .consts import FEEDS
from .statusapi import StatusAPI

_log = logging.getLogger("red.vexed.status.core")


# cspell:ignore DONT


class Status(commands.Cog, StatusCom, StatusDevCom, StatusSetCom):
    """
    Automatically check for status updates.

    When there is one, it will send the update to all channels that
    have registered to recieve updates from that service.

    There's also the `status` command which anyone can use to check
    updates whereever they want.

    If there's a service that you want added, contact Vexed#3211 or
    make an issue on the GitHub repo (or even better a PR!).
    """

    __version__ = "2.0.1"
    __author__ = "Vexed#3211"

    def format_help_for_context(self, ctx: commands.Context):
        """Thanks Sinbad."""
        return format_help(self, ctx)

    def __init__(self, bot: Red):
        self.bot = bot

        # config
        default = {}
        self.config: Config = Config.get_conf(self, identifier="Vexed-status")  # shit idntfr. bit late to change it...
        self.config.register_global(version=2)
        self.config.register_global(feed_store=default)
        self.config.register_global(old_ids=[])
        self.config.register_global(latest=default)  # this is unused? i think? remove soonish
        self.config.register_channel(feeds=default)
        self.config.register_guild(service_restrictions=default)

        # other stuff
        self.session = aiohttp.ClientSession()
        self.last_checked = LastChecked()
        self.config_wrapper = ConfigWrapper(self.config, self.last_checked)
        self.service_cooldown = ServiceCooldown()
        self.used_feeds = None
        self.service_restrictions_cache = None

        self.statusapi = StatusAPI(self.session)

        asyncio.create_task(self._async_init())

        if 418078199982063626 in self.bot.owner_ids:
            try:
                self.bot.add_dev_env_value("status", lambda _: self)
                self.bot.add_dev_env_value("loop", lambda _: self.update_checker.loop)
                self.bot.add_dev_env_value("statusapi", lambda _: self.statusapi)
                self.bot.add_dev_env_value("sendupdate", lambda _: SendUpdate)
                _log.debug("Added dev env vars.")
            except Exception:
                _log.exception("Unable to add dev env vars.", exc_info=True)

    def cog_unload(self):
        self.update_checker.loop.cancel()
        asyncio.create_task(self.session.close())
        try:
            self.bot.remove_dev_env_value("status")
            self.bot.remove_dev_env_value("loop")
            self.bot.remove_dev_env_value("statusapi")
            self.bot.remove_dev_env_value("sendupdate")
        except KeyError:
            _log.debug("Unable to remove dev env vars. They probably weren't added.")
        _log.info("Status unloaded.")

    async def red_delete_data_for_user(self, **kwargs):
        """Nothing to delete"""
        return

    async def _async_init(self):
        await self.bot.wait_until_red_ready()

        if await self.config.version() != 3:
            _log.info("Getting initial data from services...")
            await self._migrate_to_v3()
            await self._get_initial_data()
            await self.config.incidents.clear()
            await self.config.version.set(3)
            _log.info("Done!")
            act_send = False
        else:
            act_send = True

        self.used_feeds = UsedFeeds(await self.config.all_channels())
        self.service_restrictions_cache = ServiceRestrictionsCache(await self.config.all_guilds())

        # this will start the loop
        self.update_checker = UpdateChecker(
            self.bot,
            self.used_feeds,
            self.last_checked,
            self.config,
            self.config_wrapper,
            self.statusapi,
            actually_send=act_send,
        )

        _log.info("Status cog has been successfully initialized.")

    async def _get_initial_data(self):
        """Start with initial data."""
        old_ids = []
        for service, settings in FEEDS.items():
            _log.debug(f"Starting {service}.")
            try:
                incidents, etag, status = await self.statusapi.incidents(settings["id"])
                if status != 200:
                    _log.warning(f"Unable to get initial data from {service}: HTTP status {status}")
                incs = incidents["incidents"]
                for inc in incs:
                    old_ids.append(inc["id"])
                    old_ids.extend([i["id"] for i in inc["incident_updates"]])
            except Exception:
                _log.warning(f"Unable to get initial data from {service}.", exc_info=True)
                continue

            try:
                scheduled, etag, status = await self.statusapi.scheduled_maintenance(settings["id"])
                if status != 200:
                    _log.warning(f"Unable to get initial data from {service}: HTTP status {status}")
                incs = scheduled["scheduled_maintenances"]
                for inc in incs:
                    old_ids.append(inc["id"])
                    old_ids.extend([i["id"] for i in inc["incident_updates"]])
            except Exception:
                _log.warning(f"Unable to get initial data from {service}.", exc_info=True)
                continue

        await self.config.old_ids.set(old_ids)

    async def _migrate_to_v3(self):
        # ik this is a mess
        really_old = await self.config.all_channels()
        _log.debug("Config migration in progress. Old data is below in case something goes wrong.")
        _log.debug(really_old)
        for c_id, data in really_old.items():
            c_old = deepcopy(data)["feeds"]
            for service in data.get("feeds", {}).keys():
                if service in ["twitter", "status.io", "aws", "gcp", "smartthings"]:
                    c_old.pop(service, None)
                else:
                    c_old[service]["edit_id"] = {}

            await self.config.channel_from_id(c_id).feeds.set(c_old)

    @commands.command(name="statusinfo", hidden=True)
    async def command_statusinfo(self, ctx: commands.Context):
        loopstatus = self.update_checker.loop.is_running()
        try:
            loopintegrity = time() - self.update_checker.loop._last_iteration.timestamp() <= 120
        except AttributeError:
            loopintegrity = False

        extras = {"Loop running": loopstatus, "Loop integrity": loopintegrity}
        main = format_info(self.qualified_name, self.__version__, extras=extras)

        await ctx.send(f"{main}")