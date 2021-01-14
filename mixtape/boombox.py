import logging

from collections import ChainMap, UserDict
from typing import Any, List, Mapping, Optional, Tuple

import attr
import gi
import simplug

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .players import Player
from .exceptions import BoomBoxNotConfigured

logger = logging.getLogger(__name__)


hookspec = simplug.Simplug("mixtape")


class Context(UserDict[Any, Any]):
    """
    Application state object
    """


class PluginSpec:
    """Mixtape plugin namespace"""

    @hookspec.spec
    def mixtape_plugin_init(self, ctx: Context) -> None:
        """Called"""

    @hookspec.spec
    def mixtape_add_pipelines(self, ctx: Context) -> Mapping[str, Any]:
        """
        Hook allowing a plugin to return a pipeline
        """

    # player init and teardown

    @hookspec.spec
    def mixtape_player_setup(self, ctx: Context, player: Player) -> None:
        """
        Hook called on player setup
        """

    @hookspec.spec
    def mixtape_player_teardown(self, ctx: Context, player: Player) -> None:
        """
        Hook called on player teardown
        """

    # pipeline control and event hooks

    @hookspec.spec
    async def mixtape_on_message(self, ctx: Context, player: Player, message: Gst.Message) -> None:
        """
        Generic hook for all bus messages
        """

    @hookspec.spec
    async def mixtape_before_state_changed(
        self, ctx: Context, player: Player, state: Gst.State
    ) -> None:
        """
        Hook called before a `set_state` call.
        """

    @hookspec.spec
    async def mixtape_on_ready(self, ctx: Context, player: Player) -> None:
        """
        Shortcut Hook called on state changed to READY
        """

    @hookspec.spec
    async def mixtape_on_pause(self, ctx: Context, player: Player) -> None:
        """
        Shortcut Hook called on state changed to PAUSED
        """

    @hookspec.spec
    async def mixtape_on_play(self, ctx: Context, player: Player) -> None:
        """
        Shortcut Hook called on state changed to PLAYING
        """

    @hookspec.spec
    async def mixtape_on_stop(self, ctx: Context, player: Player) -> None:
        """
        Hook called on state changed to NULL
        """

    # asyncio player events

    @hookspec.spec
    async def mixtape_on_eos(self, ctx: Context, player: Player) -> None:
        """
        Hook called on eos
        """

    @hookspec.spec
    async def mixtape_on_error(self, ctx: Context, player: Player) -> None:
        """
        Hook called on bus message error
        """


@attr.s
class BoomBox:
    """
    Facade object that orchestrates plugin callbacks
    and exposes plugin events and properties.
    """

    player: Player = attr.ib()
    context: Context = attr.ib(repr=False)
    _hookspec: simplug.Simplug = attr.ib(repr=False)

    @property
    def _hooks(self) -> simplug.SimplugHooks:
        """Shortcut property for plugin hooks"""
        return self._hookspec.hooks

    def setup(self) -> None:
        """Wrapper for player setup"""
        self.player.setup()
        self._hooks.mixtape_player_setup(ctx=self.context, player=self.player)

    def teardown(self) -> None:
        """wrapper for player teardown"""
        self._hooks.mixtape_player_teardown(ctx=self.context, player=self.player)
        self.player.teardown()

    async def ready(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        """Wrapper for player ready"""
        await self._hooks.mixtape_before_state_changed(
            ctx=self.context, player=self.player, state=Gst.State.READY
        )
        ret = await self.player.ready()
        await self._hooks.mixtape_on_ready(ctx=self.context, player=self.player)
        return ret

    async def pause(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        """Wrapper for player pause"""
        await self._hooks.mixtape_before_state_changed(
            ctx=self.context, player=self.player, state=Gst.State.PAUSED
        )
        ret = await self.player.pause()
        await self._hooks.mixtape_on_pause(ctx=self.context, player=self.player)
        return ret

    async def play(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        """Wrapper for player play"""
        await self._hooks.mixtape_before_state_changed(
            ctx=self.context, player=self.player, state=Gst.State.PLAYING
        )
        ret = await self.player.play()
        await self._hooks.mixtape_on_play(ctx=self.context, player=self.player)
        return ret

    async def stop(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        """Wrapper for player stop"""
        await self._hooks.mixtape_before_state_changed(
            ctx=self.context, player=self.player, state=Gst.State.NULL
        )
        ret = await self.player.stop()
        await self._hooks.mixtape_on_stop(ctx=self.context, player=self.player)
        return ret

    @classmethod
    async def init(
        cls,
        player: Optional[Any] = None,
        context: Optional[Context] = None,
        plugins: Optional[List[Any]] = None,
        **settings: Any,
    ) -> "BoomBox":
        """Boombox async init method"""

        # load plugins
        if plugins:
            for plugin in plugins:
                hookspec.register(plugin)
        else:
            hookspec.load_entrypoints("mixtape")

        # init context

        if context is None:
            context = Context()

        # init plugins

        hookspec.hooks.mixtape_plugin_init(ctx=Context)

        # init player

        if not player:
            context["pipelines"] = ChainMap(*hookspec.hooks.mixtape_add_pipelines(ctx=context))

            try:
                description = context["pipelines"][settings["name"]]
            except AttributeError:
                raise BoomBoxNotConfigured("Pipeline needed explicitly or provided by hook")
            else:
                player = await Player.from_description(description)

        return cls(player, context, hookspec)
