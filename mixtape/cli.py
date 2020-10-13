# type: ignore
import click
import asyncio
import gi
import logging
import colorlog
from prompt_toolkit import HTML
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit import PromptSession
import string
from typing import Dict, List

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
        plugin_options = ctx.pm.hook.mixtape_add_options()
        for option_group in plugin_options:
            for option in option_group:
                o = click.Option([f"--{option['name']}"])
                params.append(o)
        return params


def bottom_toolbar(keyboard_mapping: Dict):
    """Returns formatted bottom toolbar"""
    available_commands = ', '.join(f'{v} [{k}]' for k,v in keyboard_mapping.items())
    return HTML(f'Mixtape <b><style bg="ansired">Commands:</style></b>{available_commands}!')


def get_key_command_mapping(commands: List) -> Dict:
    """Returns a mapping of keys to commands"""
    keyboard_mapping = {}
    for command in commands:
        if command[0] not in keyboard_mapping.keys():
            keyboard_mapping[command[0]] = command
        else:
            available_letters = [l for l in string.ascii_lowercase if l not in keyboard_mapping.keys()]
            try:
                keyboard_mapping[available_letters[0]] = command
            except IndexError:
                raise Exception('More commands than lowercase letters!')
    return keyboard_mapping

async def main(description, ctx, options):
    Gst.init(None)
    
    options["description"] = description
    boombox = BoomBox(player=None, pm=ctx.pm, **options)
    help_text = "Press key:"
    boombox.setup()

    session = PromptSession()
    while True:
        with patch_stdout():
            commands = boombox._context.commands
            key_command_mapping = get_key_command_mapping(list(commands))
            toolbar = bottom_toolbar(key_command_mapping)
            result = await session.prompt_async(
                help_text, bottom_toolbar=toolbar
            )
            print("You said: %s" % result)
        if result in key_command_mapping:
            await commands[key_command_mapping[result]]()
            if key_command_mapping[result] == 'stop':
                break
    boombox.teardown()

@click.command(cls=MixtapeCommand)
@click.argument("description", nargs=-1, type=click.UNPROCESSED, required=False)
@click.pass_context
def play(ctx, description, **kwargs):
    description = " ".join(description)

    asyncio.run(main(description, ctx, kwargs))
    
