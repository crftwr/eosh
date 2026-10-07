"""Eolith Shell — a lightweight but powerful terminal shell environment."""

from . import notify
from .colors import ColorScheme, set_color_scheme
from .commands import arg, CmdParser, registry as command_registry
from .prompt import set_prompt
from .shell import (
    passthrough_input,
    passthrough_input_block,
    passthrough_run,
)
from .variables import Var, EnvVar, Setting, registry as var_registry

#: Single source of truth for the version string -- the ONLY place the literal
#: appears in this repo. pyproject.toml derives it via its dynamic ``version``
#: (``attr = "eosh.__version__"``), and ``make tag`` rewrites this line.
__version__ = "0.2.0"
__all__ = [
    "arg",
    "CmdParser",
    "ColorScheme",
    "notify",
    "set_color_scheme",
    "set_prompt",
    "Var",
    "EnvVar",
    "Setting",
    "var_registry",
    "command_registry",
    "passthrough_run",
    "passthrough_input",
    "passthrough_input_block",
]
