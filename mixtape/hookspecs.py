import attr
from typing import Any, Iterable
from pluggy import HookspecMarker
from .players import PlayerType, Gst

hookspec = HookspecMarker("mixtape")

@attr.s
class Option:
    name: str = attr.ib()
    required: bool = attr.ib(default=False)
    type: Any = attr.ib(default=str)

# plugins and config


# @hookspec
# def mixtape_addhooks():
#     """
#     Register a plugin hook
#     """

@hookspec
def mixtape_addoptions(player: PlayerType) -> Iterable[Option]:
    """
    Register an option
    """

# @hookspec
# def mixtape_plugin_registered(player, pipeline, options):
#     pass

# @hookspec
# def mixtape_plugin_autoload(player, pipeline, options):
#     pass



# @hookspec
# def mixtape_configure():
#     pass

# pipeline creation and signals

# @hookspec
# def mixtape_create_pipeline(player):
#     pass

# @hookspec
# def mixtape_on_element_added(player, element):
#     pass

# @hookspec
# def mixtape_on_deep_element_added(player, element):
#     pass

# @hookspec
# def mixtape_on_deep_element_removed(player, element):
#     pass


# player init and teardown

@hookspec
def mixtape_setup(player: PlayerType):
    """
    Hook called on player setup
    """

@hookspec
def mixtape_teardown(player: PlayerType):
    """
    Hook called on player teardown
    """


# pipeline control and event hooks

@hookspec
def mixtape_before_state_changed(player: PlayerType, state: Gst.State):
    """
    Hook called before a `set_state` call.
    """

@hookspec
def mixtape_on_state_changed(player: PlayerType, state: Gst.State):
    """
    Hook called on state changed
    """

def mixtape_on_bus_message(player: PlayerType, msg: Gst.Message):
    """
    Hook called on bus message
    """

# @hookspec
# def mixtape_on_eos(player: PlayerType):
#     pass


# player actions and properties


# @hookspec
# def mixtape_register_method():
#     pass

# @hookspec
# def mixtape_register_property():
#     pass