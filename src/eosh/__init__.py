"""Eolith Shell — a lightweight but powerful terminal shell environment."""

from . import hooks, keys, notify
from .colors import TERM_BG, TERM_FG, ColorScheme, color_enabled, set_color_scheme
from .commands import arg, CmdParser, registry as command_registry
from .prompt import set_prompt
from .command_context import CommandContext, ShellView
from .variables import Var, EnvVar, PyVar, GlobalVar, registry as var_registry

#: Single source of truth for the version string -- the ONLY place the literal
#: appears in this repo. pyproject.toml derives it via its dynamic ``version``
#: (``attr = "eosh.__version__"``), and ``make tag`` rewrites this line.
__version__ = "0.2.0"
__all__ = [
    "arg",
    "CmdParser",
    "ColorScheme",
    "color_enabled",
    "hooks",
    "keys",
    "notify",
    "set_color_scheme",
    "TERM_FG",
    "TERM_BG",
    "set_prompt",
    "Var",
    "EnvVar",
    "PyVar",
    "GlobalVar",
    "var_registry",
    "command_registry",
    "CommandContext",
    "ShellView",
]
