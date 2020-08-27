# type: ignore
import click
import asyncio
import gi
import logging
import colorlog
from prompt_toolkit import HTML
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit import PromptSession

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from . import BoomBox, Player, load_mixtape_plugins

logger = logging.getLogger(__name__)
handler = colorlog.StreamHandler()
handler.setFormatter(
    colorlog.ColoredFormatter(
        "(%(asctime)s) [%(log_color)s%(levelname)s] | %(name)s | %(message)s [%(threadName)-10s]"
    )
)

# get root logger
logger = logging.getLogger()
logger.handlers = []
logger.addHandler(handler)
logger.setLevel(logging.INFO)


class MixtapeCommand(click.Command):
    """Click command that adds options from pluggy hooks"""

    def make_context(self, info_name, args, parent=None, **extra):
        """Override make_context to add Boombox early"""
        for key, value in click.core.iteritems(self.context_settings):  # noqa: B301
            if key not in extra:
                extra[key] = value
        ctx = click.Context(self, info_name=info_name, parent=parent, **extra)
        ctx.pm = load_mixtape_plugins()
        with ctx.scope(cleanup=False):
            self.parse_args(ctx, args)
        return ctx

    def get_params(self, ctx):
        """Add plugin params to cli interface"""
        params = super().get_params(ctx)
        for plugin in ctx.pm.get_plugins():
            name = ctx.pm.get_name(plugin)
            option = click.Option([f"--{name}/--no-{name}"], default=True)
            params.append(option)
        return params


def bottom_toolbar():
    """Returns formatted bottom toolbar"""
    return HTML('Mixtape <b><style bg="ansired">Commands:</style></b>!')


@click.command(cls=MixtapeCommand)
@click.argument("description", nargs=-1, type=click.UNPROCESSED, required=True)
@click.pass_context
def play(ctx, description, **kwargs):
    description = " ".join(description)

    async def main(description):
        Gst.init(None)
        player = await Player.from_description(description)
        boombox = BoomBox(player=player, pm=ctx.pm)
        help_text = "Press key:"
        boombox.setup()
        session = PromptSession()
        while True:
            with patch_stdout():

                result = await session.prompt_async(
                    help_text, bottom_toolbar=lambda: bottom_toolbar
                )
                print("You said: %s" % result)
            if result == "p":
                await boombox.play()
            if result == "s":
                await boombox.stop()
                break
        boombox.teardown()

    asyncio.run(main(description))
