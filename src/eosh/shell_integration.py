"""Prompt and command marks for the terminal — VS Code's OSC 633 and the
FinalTerm OSC 133 that iTerm2, WezTerm, Ghostty, kitty, foot and Windows
Terminal read.

A terminal that knows where each prompt, command line and output starts can
jump between prompts, select one command's output, mark failures in the
gutter — and, in VS Code, pin the running command with sticky scroll.  VS
Code learns this from the integration script it injects into the bash / zsh
it starts, so without marks of its own ``zsh% eosh`` is one command whose
output is the whole eosh session (issue #26).

One line, in the order VS Code's zsh script emits it::

    osc633:  A <prompt> B <line> [F "> " G …]  E;<line> C <output> D;<status> P;Cwd=…
    osc133:  A <prompt> B <line>                        C <output> D;<status>

* ``A`` / ``B`` (``F`` / ``G``) — :func:`prompt_marks`, written by the line
  editor around the prompt on every redraw (outside the prompt string, so
  its width stays the prompt's own).
* ``E`` / ``C`` — :func:`command_started`, from the shell loop before it
  runs the line.
* ``D`` / ``P;Cwd`` — :func:`command_finished`.  A line that ran nothing
  (empty, Ctrl+C, a Ctrl+] switch) is closed the way zsh closes it, with a
  ``D`` without a status.

**Only to terminals known to read them.**  Nushell and fish write OSC 133
everywhere and let the user turn it off; fish 4.0 printed ``;special_key=1``
in Termux, Guacamole and noVNC that way (fish#11749).  Here a stray mark
would also break the line editor's width math, so :data:`TERMINALS` is an
allowlist, matched on the environment.  ``var shell_integration=osc133``
(or ``osc633`` / ``off``) overrides it, and a config adds terminals to it::

    from eosh import shell_integration
    shell_integration.TERMINALS.append(("TERM_PROGRAM", "tmux", "osc133"))
"""

from __future__ import annotations

import os
import sys

OSC633 = "osc633"
OSC133 = "osc133"
AUTO = "auto"
OFF = "off"
MODES = (AUTO, OSC133, OSC633, OFF)

#: ``(environment variable, value, dialect)``, first match wins.  A value of
#: ``None`` matches any non-empty value.  VS Code comes first: its terminal
#: inherits ``WT_SESSION`` / ``KITTY_*`` from whatever started ``code``.
#: Konsole is left out, as fish leaves it out: its default profile draws the
#: marks visibly (fish#11409).  Apple Terminal reads neither.
DEFAULT_TERMINALS: tuple[tuple[str, str | None, str], ...] = (
    ("TERM_PROGRAM", "vscode", OSC633),
    ("TERM_PROGRAM", "iTerm.app", OSC133),
    ("TERM_PROGRAM", "WezTerm", OSC133),
    ("TERM_PROGRAM", "ghostty", OSC133),
    ("TERM", "xterm-ghostty", OSC133),    # TERM survives ssh, TERM_PROGRAM doesn't
    ("TERM", "xterm-kitty", OSC133),
    ("TERM", "foot", OSC133),
    ("TERM", "foot-extra", OSC133),
    ("WT_SESSION", None, OSC133),         # Windows Terminal
)
TERMINALS: list[tuple[str, str | None, str]] = list(DEFAULT_TERMINALS)

_mode = AUTO

# The dialect of the marks that are open, or None.  ``_state`` is "prompt"
# (A written, line being edited) or "running" (C written, output flowing).
_open: str | None = None
_state = "idle"


def detect() -> str | None:
    """The dialect :data:`TERMINALS` names for this environment, or None."""
    for var, value, dialect in TERMINALS:
        actual = os.environ.get(var)
        if actual and (value is None or actual == value):
            return dialect
    return None


def dialect() -> str | None:
    """The marks to write now: the mode, or what :func:`detect` finds for
    ``auto`` — and nothing unless stdout is a terminal."""
    if _mode == OFF:
        return None
    try:
        if not sys.stdout.isatty():
            return None
    except (AttributeError, ValueError):
        return None
    return detect() if _mode == AUTO else _mode


def get_mode() -> str:
    return _mode


def set_mode(mode: str) -> None:
    """``auto`` (detect the terminal), ``osc133``, ``osc633`` or ``off``."""
    global _mode
    if mode not in MODES:
        raise ValueError(f"expected one of {', '.join(MODES)}, got {mode!r}")
    _mode = mode


def reset_config() -> None:
    """``reload``: :data:`TERMINALS` back to the defaults.  The mode stays,
    like ``notify``'s values — ``var`` sets it at the prompt too."""
    TERMINALS[:] = DEFAULT_TERMINALS


def escape_value(text: str) -> str:
    """Escape an OSC 633 ``P`` / ``E`` value as VS Code reads it: ``\\``
    doubled, ``;`` and control characters as ``\\xHH``."""
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == ";" or ord(ch) < 0x20:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


def _osc(kind: str, body: str) -> str:
    return f"\x1b]{633 if kind == OSC633 else 133};{body}\x07"


def _write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def prompt_marks(continuation: bool = False) -> tuple[str, str]:
    """The marks to write before and after a prompt — ``A`` / ``B`` for the
    primary prompt, ``F`` / ``G`` for a ``> `` continuation (OSC 633 only)."""
    global _open, _state
    if continuation:
        if _open == OSC633:
            return _osc(OSC633, "F"), _osc(OSC633, "G")
        return "", ""
    kind = dialect()
    if kind is None:
        return "", ""
    _open, _state = kind, "prompt"
    return _osc(kind, "A"), _osc(kind, "B")


def command_started(line: str) -> None:
    """The prompt's line is about to run: its output starts here."""
    global _state
    if _state != "prompt":
        return
    out = ""
    if _open == OSC633:
        out += _osc(OSC633, f"E;{escape_value(line)}")
    _write(out + _osc(_open, "C"))
    _state = "running"


def command_finished(status: int | None) -> None:
    """Close what is open.  *status* is ``None`` when it is unknown — the
    line ran nothing, was interrupted, or went to the background."""
    global _open, _state
    if _open is None:
        return
    kind = _open
    out = ""
    if _state == "prompt":
        out += (_osc(OSC633, "E;") if kind == OSC633 else "") + _osc(kind, "C")
    out += _osc(kind, "D" if status is None else f"D;{status}")
    if kind == OSC633:
        out += _osc(OSC633, f"P;Cwd={escape_value(os.getcwd())}")
    _write(out)
    _open, _state = None, "idle"


def startup() -> None:
    """Tell VS Code where eosh starts, before the first prompt."""
    if dialect() == OSC633:
        _write(_osc(OSC633, f"P;Cwd={escape_value(os.getcwd())}"))


_ALIASES = {"on": AUTO, "true": AUTO, "yes": AUTO, "1": AUTO,
            "false": OFF, "no": OFF, "0": OFF,
            "133": OSC133, "633": OSC633}


def register_vars() -> None:
    """Register ``shell_integration`` — a :class:`GlobalVar`, like
    ``notify``: it is about the terminal, not about a context."""
    from .completion import ChoiceCompleter
    from .variables import GlobalVar, registry as var_registry

    class _ShellIntegrationVar(GlobalVar):
        @property
        def name(self) -> str:
            return "shell_integration"

        @property
        def description(self) -> str:
            found = detect() or "none"
            return f"Prompt marks for the terminal: auto/osc133/osc633/off (detected: {found})"

        def get(self) -> str | None:
            return _mode

        def set(self, value: str) -> None:
            v = value.strip().lower()
            v = _ALIASES.get(v, v)
            if v not in MODES:
                print(f"shell_integration: expected {'/'.join(MODES)}, got {value!r}",
                      file=sys.stderr)
                return
            set_mode(v)

        def unset(self) -> None:
            set_mode(AUTO)

        @property
        def value_completer(self):
            return ChoiceCompleter(list(MODES))

    var_registry.register(_ShellIntegrationVar())
