import attr
import pluggy

from typing import Type, Optional, Any

from .players import Player, PlayerType, Gst

@attr.s
class BoomBox(Player):
    "Boom boom"
    pm: Type[pluggy.PluginManager] = attr.ib(repr=False)

    @property
    def hook(self) -> Any:
        """Convenience shortcut for pm hook"""
        return self.pm.hook

    def setup(self) -> None:
        super().setup()
        self.hook.mixtape_setup(player=self)

    def tearddown(self) -> None:
        self.hook.mixtape_teardown(player=self)
        super().setup()

    async def set_state(self, state: Gst.State) -> Gst.StateChangeReturn:
        self.hook.mixtape_before_state_changed(player=self, state=state)
        ret = await super().set_state(state)
        self.hook.mixtape_on_state_changed(player=self, state=state)
        return ret


            