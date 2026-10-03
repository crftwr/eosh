"""Line tokenization and quote handling."""

import os
import shlex
import subprocess


def _lookup_var(name: str) -> str | None:
    """Resolve *name* by checking var_registry first, then os.environ.

    Mirrors the set-side lookup order in ``shell._set_variable``: a registered
    :class:`Var` wins over a same-named env key.  Returns None when neither
    source has a value, which the caller renders as an empty string.

    Imported lazily so ``parsing`` stays free of import-time pitash deps.
    """
    from .variables import registry as var_registry

    var = var_registry.get(name)
    if var is not None:
        return var.get()
    return os.environ.get(name)


def _find_cmd_sub_end(line: str, start: int) -> int:
    """Given *line* and *start* pointing at the '(' in ``$(...)``, return the
    index of the matching closing ')'.  Returns -1 if unmatched.

    Handles:
    - Nested ``$(...)`` (and bare sub-shells via ``(...)``)
    - Single-quoted regions (no special chars inside)
    - Double-quoted regions (only backslash escapes active)
    - Backslash escapes outside quotes
    """
    depth = 1
    i = start + 1  # first character after the opening '('
    n = len(line)

    while i < n:
        ch = line[i]

        if ch == '\\' and i + 1 < n:
            i += 2  # skip escaped character
            continue

        if ch == "'":
            # Single-quoted region — pass through until closing '
            i += 1
            while i < n and line[i] != "'":
                i += 1
            if i < n:
                i += 1  # skip closing quote
            continue

        if ch == '"':
            # Double-quoted region — only backslash escapes matter
            i += 1
            while i < n:
                c = line[i]
                if c == '"':
                    i += 1
                    break
                if c == '\\' and i + 1 < n:
                    i += 2
                else:
                    i += 1
            continue

        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return i

        i += 1

    return -1  # unmatched


def _run_cmd_sub(cmd_text: str) -> str:
    """Execute *cmd_text* in a subshell and return its stdout with trailing
    newlines stripped — standard POSIX command-substitution semantics.
    """
    try:
        result = subprocess.run(
            cmd_text,
            shell=True,
            capture_output=True,
            text=True,
            env=os.environ,
        )
        return result.stdout.rstrip('\n')
    except Exception:
        return ""


def expand_vars(line: str) -> str:
    """Expand ``$VAR``, ``${VAR}``, and ``$(cmd)`` in *line*.

    Single-quoted regions are passed through verbatim (no expansion).
    Double-quoted regions are treated the same as bare text for expansion
    purposes (quote stripping happens later in the tokenizer).
    """
    result = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "'":
            # Single-quoted region — no expansion
            j = line.find("'", i + 1)
            if j == -1:
                result.append(line[i:])
                break
            result.append(line[i : j + 1])
            i = j + 1
        elif ch == "$":
            next_i = i + 1
            if next_i < len(line) and line[next_i] == "(":
                # Command substitution: $(...)
                end = _find_cmd_sub_end(line, next_i)
                if end != -1:
                    cmd_text = line[next_i + 1 : end]
                    result.append(_run_cmd_sub(cmd_text))
                    i = end + 1
                else:
                    result.append(ch)
                    i += 1
            elif next_i < len(line) and line[next_i] == "{":
                # Brace-quoted variable: ${VAR}
                end = line.find("}", next_i + 1)
                if end != -1:
                    name = line[next_i + 1 : end]
                    result.append(_lookup_var(name) or "")
                    i = end + 1
                else:
                    result.append(ch)
                    i += 1
            else:
                # Plain variable: $VAR
                j = i + 1
                while j < len(line) and (line[j].isalnum() or line[j] == "_"):
                    j += 1
                if j > i + 1:
                    result.append(_lookup_var(line[i + 1 : j]) or "")
                    i = j
                else:
                    result.append(ch)
                    i += 1
        else:
            result.append(ch)
            i += 1
    return "".join(result)


def tokenize(line: str) -> list[str]:
    """Split a command line into tokens, respecting quotes.

    Returns partial tokens if the line ends mid-word (no trailing space).
    An empty line returns [].

    The backslash keeps its POSIX meaning (escape / line continuation) on every
    platform — on Windows the shell uses ``/`` as the path separator (like Git
    Bash / MSYS), which the file APIs accept natively, so paths never collide
    with escaping.  See completion._to_slash and prompt path rendering.
    """
    try:
        return shlex.split(line)
    except ValueError:
        # Unclosed quote — try closing with each quote character in turn.
        # (The existing token may be single- or double-quoted.)
        for close in ('"', "'"):
            try:
                return shlex.split(line + close)
            except ValueError:
                continue
        # Last resort: naive whitespace split
        return line.split()


def split_for_completion(line: str) -> tuple[list[str], str]:
    """Split line into completed tokens and the current prefix being typed.

    If line ends with whitespace, prefix is "" (user is starting a new arg).
    Otherwise prefix is the partial last token.
    """
    if not line:
        return [], ""

    if line[-1] == " ":
        return tokenize(line), ""

    tokens = tokenize(line)
    if not tokens:
        return [], ""

    return tokens[:-1], tokens[-1]


def raw_token_start(text: str) -> int:
    """Return the index in *text* where its last (partial) token starts.

    This is the **completion anchor**: the position a candidate is inserted at,
    and therefore the position candidate values are measured from.  Everything
    from here to the end of *text* is the raw text of the token being completed.

    Scans forward tracking single- and double-quote state so a token like
    ``'My Documents/'`` counts as one unit.  Note "raw": the returned index
    counts the quote characters, unlike :func:`split_for_completion`, whose
    prefix is what shlex produced after removing them.
    """
    last_start = 0
    i = 0
    while i < len(text):
        c = text[i]
        if c in (" ", "\t"):
            last_start = i + 1
            i += 1
        elif c in ("'", '"'):
            j = text.find(c, i + 1)
            if j == -1:
                break  # unclosed quote — the rest is part of this token
            i = j + 1
        else:
            i += 1
    return last_start
