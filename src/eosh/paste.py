"""Pasted text at the prompt: what it is and where it runs.

The line editor turns on bracketed paste (``ESC[?2004h``) while it reads a
line, so a paste arrives as one unit between ``ESC[200~`` and ``ESC[201~``
instead of a stream of keys whose newlines would each run a line.  A paste
that is more than one line, or that needs bash, is held as a
:class:`PastedBlock` until Enter.

Where a block runs is decided by its *syntax*, not by the platform.  A
block written for bash — the usual ``export KEY=VALUE`` lines a console
hands out, a ``for`` loop, a heredoc — runs in a child bash through
``source-bash``, whose environment and cwd are imported back.  Anything
else is a series of eosh lines and runs one line at a time, as if typed.
:func:`needs_bash` is a heuristic over the words in command position and a
few constructs eosh's own parser doesn't have; it errs toward eosh, where a
miss is a visible ``command not found`` rather than a silent change of
interpreter.
"""

from __future__ import annotations

import re

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
ENABLE = "\x1b[?2004h"
DISABLE = "\x1b[?2004l"


class PastedBlock(str):
    """A paste held at the prompt and accepted with Enter.

    ``bash`` says where it runs: ``source-bash`` when True, the eosh lines
    one by one when False.
    """

    bash: bool

    def __new__(cls, text: str, bash: bool) -> "PastedBlock":
        obj = super().__new__(cls, text)
        obj.bash = bash
        return obj


# Words that, in command position, mean the text was written for bash: eosh
# has no builtin by that name (`export`, `source`, `unset`, …) or no such
# grammar at all (`if`, `for`, `case`, `{ …; }`, `[[ … ]]`).
_BASH_WORDS = frozenset({
    "export", "source", ".", "unset", "set", "declare", "typeset", "local",
    "readonly", "eval", "trap", "shopt", "umask", "ulimit", "function",
    "if", "then", "elif", "else", "fi", "for", "while", "until", "do", "done",
    "case", "esac", "select", "[[", "}",
})

# Constructs eosh's parser doesn't have, wherever they appear in a line:
# backtick substitution, a heredoc / here-string, arithmetic expansion, and
# `${VAR:-x}`-style parameter operators.  (`$(…)` alone is not here: eosh
# expands it itself.)
_BASH_CONSTRUCT = re.compile(r"`|<<|\$\(\(|\$\{[A-Za-z_][A-Za-z0-9_]*[:#%/^,]")

# `name() {` — a function definition.
_FUNC_DEF = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*\s*\(\s*\)")

# A leading `NAME=VALUE ` prefix (`AWS_PROFILE=x aws …`) is not the command.
_ASSIGN_PREFIX = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|\S)*\s+)+")

# Where a new command starts inside a line: after `;`, `&&`, `||`, `|`, `&`.
_COMMAND_SEP = re.compile(r";|&&|\|\||\||&")

_NEWLINES = re.compile(r"\r\n?")


def normalize(text: str) -> str:
    """CRLF / CR line ends as ``\\n``, tabs as spaces, other control
    characters dropped — what a paste may carry that the line can't."""
    text = _NEWLINES.sub("\n", text).replace("\t", "    ")
    return "".join(ch for ch in text if ch == "\n" or ch.isprintable())


def _strip_quoted(line: str) -> str:
    """*line* with quoted text blanked out, so a `;` or `if` inside a
    string doesn't look like syntax.  Unclosed quotes run to the end."""
    return re.sub(r"'[^']*'?|\"(?:\\.|[^\"\\])*\"?", "''", line)


def needs_bash(text: str) -> bool:
    """True when *text* uses bash syntax eosh can't run itself."""
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        bare = _strip_quoted(line)
        if _BASH_CONSTRUCT.search(bare) or _FUNC_DEF.match(bare):
            return True
        for part in _COMMAND_SEP.split(bare):
            part = _ASSIGN_PREFIX.sub("", part.strip())
            word = part.split(None, 1)[0] if part else ""
            # `(cd x; make)` and `{ a; b; }` start with the bracket glued on.
            if word in _BASH_WORDS or word.startswith(("(", "{")):
                return True
    return False


def eosh_lines(text: str) -> list[str]:
    """The lines of a block meant for eosh, as the prompt would have read
    them: backslash continuations joined, blank lines and ``#`` comments
    dropped."""
    lines: list[str] = []
    pending = ""
    for raw in text.split("\n"):
        stripped = raw.rstrip()
        if stripped.endswith("\\") and not stripped.endswith("\\\\"):
            pending += stripped[:-1]
            continue
        line = (pending + raw).strip()
        pending = ""
        if line and not line.startswith("#"):
            lines.append(line)
    if pending.strip():
        lines.append(pending.strip())
    return lines
