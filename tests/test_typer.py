"""`mixtape.cli` imports Typer's private copy of Click, `typer._click`, which a Typer release may
move or drop: this says so by name when an upgrade does, apart from the import errors of every
test that imports the CLI."""

import importlib

import pytest

USED = {
    "typer._click.exceptions": ("ClickException", "UsageError"),
    "typer._click.core": ("Context",),
    "typer._click.formatting": ("HelpFormatter",),
}


@pytest.mark.parametrize("module", sorted(USED))
def test_the_private_click_of_typer_still_has_what_the_cli_imports(module: str) -> None:
    try:
        found = importlib.import_module(module)
    except ImportError as exc:
        pytest.fail(f"{module} is gone ({exc}): change the imports at the top of mixtape/cli.py")
    missing = [name for name in USED[module] if not hasattr(found, name)]
    assert not missing, f"{module} has no {', '.join(missing)}: see the imports of mixtape/cli.py"
