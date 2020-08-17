import click
import asyncio
import signal
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from . import BoomBox, load_plugin_manager

class PluggyCLI(click.Command):
    """Click command that adds options from pluggy hooks"""

    def __init__(self, *args, **kwargs): 
        super().__init__(*args, **kwargs)
        self.pm = load_plugin_manager()
        # we add an option for every plugin to disable
        for plugin in self.pm.get_plugins():
            name = self.pm.get_name(plugin)
            option = click.Option([f'--{name}/--no-{name}'], default=True)
            self.params.append(option)

@click.command(cls=PluggyCLI)
@click.argument('description', nargs=-1, type=click.UNPROCESSED, required=True)
@click.pass_context
def play(ctx, description, **kwargs):
    description = " ".join(description)
    Gst.init(None)
    async def main(description, pm):
        pipeline = BoomBox.parse_description(description)
        player = BoomBox(pipeline, pm)
        player.setup()
        await player.play_until_eos()
        player.teardown()
    asyncio.run(main(description, ctx.command.pm))

