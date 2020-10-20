# type: ignore
from typing import Any, Callable, cast, TypeVar
import pluggy
from .core import Context, Player
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst


F = TypeVar("F", bound=Callable[..., Any])
hookspec = cast(Callable[[F], F], pluggy.HookspecMarker("mixtape"))

# plugin 

@hookspec
def mixtape_plugin_init(player: Player, ctx: Context):
    pass

@hookspec
def mixtape_plugin_autoload():
    pass

# interface options

@hookspec
def mixtape_add_options():
    """
    Hook called on setup to add interface options exposed by plug-ins.
    """

@hookspec(firstresult=True)
def mixtape_get_pipeline(ctx: Context):
    """
    Hook allowing a plugin to return a pipeline
    """

# player init and teardown


@hookspec
def mixtape_setup(player: Player, ctx: Context):
    """
    Hook called on player setup
    """


@hookspec
def mixtape_teardown(player: Player, ctx: Context):
    """
    Hook called on player teardown
    """


# pipeline control and event hooks


@hookspec
def mixtape_before_state_changed(player: Player, ctx: Context, state: Gst.State):
    """
    Hook called before a `set_state` call.
    """


@hookspec
def mixtape_on_state_changed(player: Player, ctx: Context, state: Gst.State):
    """
    Hook called on state changed
    """


@hookspec
def mixtape_register_commands(player: Player, ctx: Context):
    pass

# @hookspec
# def mixtape_register_conditions(player: Player, ctx: Context):
#     pass

# def mixtape_on_bus_message(player: Player,ctx: Context, msg: Gst.Message):
#     """
#     Hook called on bus message
#     """

# @hookspec
# def mixtape_on_eos(player: PlayerType):
#     pass


# player actions and properties


# @hookspec
# def mixtape_register_property():
#     pass
