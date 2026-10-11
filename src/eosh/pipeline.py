"""Quote-aware shell operator parser: pipes, redirections, sequencing."""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional


class DecoratorParseError(ValueError):
    """Raised when a decorator-prefixed line is malformed.

    Examples that raise:

    * ``@watch ls | grep py`` — operator outside braces (use ``{...}``).
    * ``@watch {ls`` — unmatched opening brace.
    * ``@watch {ls} ; pwd`` — sequencing operator after the closing brace
      (only ``|`` composition is supported in the MVP).
    """


@dataclass
class DecoratorCall:
    """A decorator wrapping a parsed pipeline.

    Stored on a :class:`Stage` as ``stage.decorator``.  When set, the
    stage represents "invoke this decorator with this body" rather than
    a regular command.
    """
    name: str
    flag_tokens: list[str]
    body: "Pipeline"


@dataclass
class Redirect:
    kind: str    # ">", ">>", "<", "2>", "2>>"  or "2>&1"
    target: str  # filename, or "1" for 2>&1


@dataclass
class Stage:
    """One command in a pipeline."""
    text: str
    redirects: list[Redirect] = field(default_factory=list)
    decorator: DecoratorCall | None = None


@dataclass
class Pipeline:
    stages: list[Stage]

    def run(self, stdin=None, stdout=None, stderr=None) -> int:
        """Execute the pipeline.  Delegates to the registered executor.

        The executor is registered by :class:`eosh.shell.Shell` on
        construction so a decorator body (or any external caller) can do
        ``pipeline.run()`` without knowing about Shell.  Without a Shell
        instance, this raises ``RuntimeError`` — Pipeline.run() always
        runs *inside* a shell.
        """
        if _executor is None:
            raise RuntimeError(
                "Pipeline.run() called with no executor registered; "
                "construct a Shell first or call set_pipeline_executor()"
            )
        return _executor(self, stdin=stdin, stdout=stdout, stderr=stderr)


@dataclass
class Sequence:
    """Pipelines joined by ;, &&, or ||.  operator is None for the first item."""
    items: list[tuple[str | None, Pipeline]]


# ---------------------------------------------------------------------------
# Pipeline.run() executor indirection
# ---------------------------------------------------------------------------

_executor: Optional[Callable[..., int]] = None


def set_pipeline_executor(fn: Callable[..., int] | None) -> None:
    """Register the function that runs a pipeline.

    Called by ``Shell.__init__`` with a thin wrapper around
    ``Shell._run_pipeline``.  Passing ``None`` clears the registration
    (useful in tests).
    """
    global _executor
    _executor = fn


def _flag_takes_value(decorator_name: str, flag: str) -> bool:
    """True if *flag* on ``@decorator_name`` consumes the next token as its
    value — so ``@watch -n 5 ls`` takes ``5`` as ``-n``'s value and leaves
    ``ls`` as the body.  Decorators are commands named ``@name`` in the
    command registry (imported here, at call time, to keep module loading
    one-way: the registry never imports the parser)."""
    from .commands import registry

    deco = registry.get(f"@{decorator_name}")
    return deco is not None and deco.takes_value(flag)


# ---------------------------------------------------------------------------
# Quote-aware operator splitter
# ---------------------------------------------------------------------------

def split_on_operators(text: str, operators: list[str]) -> list[tuple[str | None, str]]:
    """Split *text* on any token in *operators*, respecting single/double quotes.

    Returns [(op_before | None, segment_text), ...].
    Operators are matched longest-first.
    """
    ops = sorted(operators, key=len, reverse=True)
    segments: list[tuple[str | None, str]] = []
    current: list[str] = []
    pending_op: str | None = None
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        # Quoted region — pass through verbatim
        if ch in ('"', "'"):
            quote = ch
            current.append(ch)
            i += 1
            while i < n:
                c = text[i]
                current.append(c)
                i += 1
                if c == quote:
                    break
                if quote == '"' and c == '\\' and i < n:
                    current.append(text[i])
                    i += 1
            continue

        # Backslash outside quotes
        if ch == '\\' and i + 1 < n:
            current.append(ch)
            current.append(text[i + 1])
            i += 2
            continue

        # Try operators longest-first
        matched: str | None = None
        for op in ops:
            if text[i:i + len(op)] == op:
                matched = op
                break

        if matched:
            segments.append((pending_op, "".join(current)))
            current = []
            pending_op = matched
            i += len(matched)
        else:
            current.append(ch)
            i += 1

    segments.append((pending_op, "".join(current)))
    return segments


# ---------------------------------------------------------------------------
# Redirect extraction
# ---------------------------------------------------------------------------

_REDIRECT_OPS = ["2>&1", "2>>", "2>", ">>", ">", "<"]


def _extract_redirects(text: str) -> tuple[str, list[Redirect]]:
    """Remove redirect tokens (and their targets) from *text*.

    Returns (cleaned_text, list_of_Redirect).
    """
    redirects: list[Redirect] = []
    result: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if ch == ' ':
            result.append(ch)
            i += 1
            continue

        # Quoted token — not a redirect op
        if ch in ('"', "'"):
            quote = ch
            j = i + 1
            while j < n:
                if text[j] == quote:
                    j += 1
                    break
                if text[j] == '\\' and quote == '"' and j + 1 < n:
                    j += 2
                else:
                    j += 1
            result.append(text[i:j])
            i = j
            continue

        # Try redirect operators
        matched_op: str | None = None
        for op in _REDIRECT_OPS:
            if text[i:i + len(op)] == op:
                matched_op = op
                break

        if matched_op:
            i += len(matched_op)
            while i < n and text[i] == ' ':
                i += 1
            if matched_op == "2>&1":
                redirects.append(Redirect(kind="2>&1", target="1"))
                continue
            # Read the target filename
            target: list[str] = []
            if i < n and text[i] in ('"', "'"):
                quote = text[i]
                i += 1
                while i < n and text[i] != quote:
                    if text[i] == '\\' and quote == '"' and i + 1 < n:
                        target.append(text[i + 1])
                        i += 2
                    else:
                        target.append(text[i])
                        i += 1
                i += 1  # closing quote
            else:
                while i < n and text[i] not in (' ', '\t'):
                    target.append(text[i])
                    i += 1
            if target:
                redirects.append(Redirect(kind=matched_op, target="".join(target)))
            continue

        result.append(ch)
        i += 1

    return "".join(result), redirects


# ---------------------------------------------------------------------------
# Glob expansion
# ---------------------------------------------------------------------------

def expand_globs(tokens: list[str], root_dir: str | None = None) -> list[str]:
    """Expand glob patterns; non-matching patterns are kept as-is.  Relative
    patterns match under *root_dir* (default: the process's cwd)."""
    result: list[str] = []
    for token in tokens:
        if any(c in token for c in ('*', '?', '[')):
            recursive = '**' in token
            expanded = glob.glob(os.path.expanduser(token), recursive=recursive,
                                 root_dir=root_dir)
            if expanded:
                result.extend(sorted(expanded))
                continue
        result.append(token)
    return result


# ---------------------------------------------------------------------------
# Brace-aware scanner (decorator scope)
# ---------------------------------------------------------------------------

_DECORATOR_NAME_RE = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")


def _find_matching_brace(text: str, open_pos: int) -> int:
    """Find the index of the ``}`` that closes the ``{`` at *open_pos*.

    The scan respects single-quote regions, double-quote regions
    (with ``\\``-escapes), bare ``\\{`` / ``\\}`` escapes, and ``${...}``
    parameter expansion (treated as a self-balanced span).  Returns -1
    if no matching brace is found.
    """
    if open_pos >= len(text) or text[open_pos] != "{":
        raise ValueError("expected '{' at open_pos")

    depth = 1
    i = open_pos + 1
    n = len(text)

    while i < n:
        ch = text[i]

        # Single-quoted span: every character literal until next '.
        if ch == "'":
            i += 1
            while i < n and text[i] != "'":
                i += 1
            i += 1  # skip closing quote (or run off end)
            continue

        # Double-quoted span: respect \\-escapes, but ${...} inside is
        # still recognised (and balanced) so the outer counter stays
        # consistent.  Treat braces inside as literal.
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                c = text[i]
                if c == "\\" and i + 1 < n:
                    i += 2
                    continue
                # ${...} inside double quotes — skip past matching }
                if c == "$" and i + 1 < n and text[i + 1] == "{":
                    i = _skip_var_expansion(text, i + 1) + 1
                    continue
                i += 1
            i += 1  # skip closing quote
            continue

        # Backslash escape outside quotes
        if ch == "\\" and i + 1 < n:
            i += 2
            continue

        # ${...} parameter expansion — balanced span
        if ch == "$" and i + 1 < n and text[i + 1] == "{":
            i = _skip_var_expansion(text, i + 1) + 1
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1

    return -1


def _skip_var_expansion(text: str, brace_pos: int) -> int:
    """Given the position of the ``{`` after a ``$``, return the index
    of the matching ``}``.  Returns the last index of *text* if unmatched
    (caller will hit end-of-string and bail).
    """
    depth = 1
    i = brace_pos + 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


def _split_tokens_simple(text: str) -> list[str]:
    """Whitespace-split *text* into tokens, respecting quotes and escapes.

    Used to chop a decorator's flag prefix into argparse-ready tokens.
    Quotes are preserved on the boundary character but stripped from the
    token's interior — which is what argparse expects.
    """
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] in (" ", "\t"):
            i += 1
            continue
        buf: list[str] = []
        while i < n and text[i] not in (" ", "\t"):
            ch = text[i]
            if ch in ('"', "'"):
                quote = ch
                i += 1
                while i < n and text[i] != quote:
                    if text[i] == "\\" and quote == '"' and i + 1 < n:
                        buf.append(text[i + 1])
                        i += 2
                    else:
                        buf.append(text[i])
                        i += 1
                i += 1
                continue
            if ch == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            buf.append(ch)
            i += 1
        if buf:
            tokens.append("".join(buf))
    return tokens


def _extract_decorator_prefix(line: str) -> tuple[DecoratorCall | None, str]:
    """Try to peel a leading ``@name [flags] body`` from *line*.

    On success returns ``(DecoratorCall, remainder)`` — the body is parsed
    here recursively.  When the input is just ``@deco {body}`` the remainder
    is the empty string.  When the line continues past the body with one
    or more pipe stages (``@deco {body} | next | other``) the remainder
    starts with ``|`` and is fed back to the outer pipeline parser, which
    splices the additional stages onto the decorator-stage's pipeline.

    Returns ``(None, line)`` if the line does not start with ``@<ident>``.

    Raises :class:`DecoratorParseError` on malformed input.
    """
    stripped = line.lstrip()
    if not stripped.startswith("@"):
        return None, line
    m = _DECORATOR_NAME_RE.match(stripped)
    if not m:
        return None, line

    name = m.group(1)
    rest = stripped[m.end():]

    # Walk forward token-by-token: every leading flag-shaped token (and
    # its value if applicable) belongs to the decorator.  Stop at the
    # first non-flag-shaped token, which is either '{' (braced body)
    # or the first command token (un-braced single-command body).
    flag_tokens: list[str] = []
    i = 0
    n = len(rest)
    while i < n and rest[i] in (" ", "\t"):
        i += 1

    saw_double_dash = False
    while i < n:
        if rest[i] == "{":
            break
        # Read one whitespace-bounded token, respecting quotes
        tok_start = i
        while i < n and rest[i] not in (" ", "\t"):
            if rest[i] in ('"', "'"):
                quote = rest[i]
                i += 1
                while i < n and rest[i] != quote:
                    if rest[i] == "\\" and quote == '"' and i + 1 < n:
                        i += 2
                    else:
                        i += 1
                i += 1
                continue
            if rest[i] == "\\" and i + 1 < n:
                i += 2
                continue
            i += 1
        tok = rest[tok_start:i]

        if tok and tok[0] not in ("-", "+"):
            # First non-flag-shaped token — body starts here.
            i = tok_start
            break

        # Flag-shaped: keep it for the decorator.
        flag_tokens.extend(_split_tokens_simple(tok))
        flag_name = flag_tokens[-1] if flag_tokens else tok

        # Skip whitespace before next token
        while i < n and rest[i] in (" ", "\t"):
            i += 1

        # ``--`` terminates flag parsing — everything after is body.
        if tok == "--":
            saw_double_dash = True
            flag_tokens.pop()  # don't pass `--` to the decorator's argparse
            break

        # If the flag we just consumed takes a value, consume the next
        # token as its argument and append it to flag_tokens.  This is
        # what makes ``@watch -n 5 ls`` parse as ``flags=["-n","5"]``,
        # ``body="ls"``.  ``--flag=value`` is a single token that argparse
        # already understands; no extra handling needed.
        if (
            i < n
            and rest[i] != "{"
            and "=" not in flag_name
            and _flag_takes_value(name, flag_name)
        ):
            val_start = i
            while i < n and rest[i] not in (" ", "\t"):
                if rest[i] in ('"', "'"):
                    quote = rest[i]
                    i += 1
                    while i < n and rest[i] != quote:
                        if rest[i] == "\\" and quote == '"' and i + 1 < n:
                            i += 2
                        else:
                            i += 1
                    i += 1
                    continue
                if rest[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            val_tok = rest[val_start:i]
            flag_tokens.extend(_split_tokens_simple(val_tok))
            while i < n and rest[i] in (" ", "\t"):
                i += 1

    body_text = rest[i:]
    if saw_double_dash:
        # Everything after `--` is the body, even if the next token starts
        # with `-`.  Trust that the body parser handles it.
        pass

    # Body: another decorator (stacking), braced, or bare?
    body_text_stripped = body_text.lstrip()
    remainder_after = ""
    if body_text_stripped.startswith("@") and _DECORATOR_NAME_RE.match(body_text_stripped):
        # `@time @retry -n 3 cmd`: the inner decorator is this one's body,
        # outside in, as Python decorators stack; what follows the inner
        # scope follows this one too.
        inner, remainder_after = _extract_decorator_prefix(body_text_stripped)
        body_pipeline = Pipeline(stages=[Stage(text="", decorator=inner)])
    elif body_text_stripped.startswith("{"):
        # Find the matching brace
        ws_offset = len(body_text) - len(body_text_stripped)
        open_pos = ws_offset
        close_pos = _find_matching_brace(body_text, open_pos)
        if close_pos < 0:
            raise DecoratorParseError(
                f"@{name}: unmatched '{{' in decorator body"
            )
        inner = body_text[open_pos + 1:close_pos]
        remainder_after = body_text[close_pos + 1:]
        body_pipeline = _parse_braced_body(inner, decorator_name=name)
    else:
        # Bare single-command body — operators are rejected.
        body_pipeline = _parse_bare_body(body_text_stripped, decorator_name=name)

    # What follows the scope continues the line: more pipe stages
    # (`@deco {…} | next`) or the next item of the sequence (`@deco {…} &&
    # other`).  Anything else — a redirect, a word — has nothing to bind to.
    remainder_stripped = remainder_after.strip()
    if remainder_stripped and not remainder_stripped.startswith(("|", ";", "&")):
        raise DecoratorParseError(
            f"@{name}: text after closing '}}' must start with `|`, `;`, `&&` "
            f"or `||`; got {remainder_stripped!r} (a redirect goes inside the braces)"
        )

    return DecoratorCall(name=name, flag_tokens=flag_tokens, body=body_pipeline), remainder_stripped


def _parse_braced_body(inner: str, *, decorator_name: str) -> Pipeline:
    """Parse the inside of a decorator's ``{...}`` as a pipeline.

    Currently accepts a single pipeline (no top-level ``;``/``&&``/``||``);
    multi-statement bodies inside braces are deferred — flag clearly so
    a future relaxation has a focused place to land.
    """
    seq = parse_line(inner)
    if not seq.items:
        raise DecoratorParseError(
            f"@{decorator_name}: empty decorator body"
        )
    if len(seq.items) > 1 or seq.items[0][0] is not None:
        raise DecoratorParseError(
            f"@{decorator_name}: ';', '&&', '||' inside decorator body "
            f"are not supported yet"
        )
    return seq.items[0][1]


def _parse_bare_body(text: str, *, decorator_name: str) -> Pipeline:
    """Parse an un-braced decorator body (no operators allowed)."""
    if not text:
        raise DecoratorParseError(
            f"@{decorator_name}: missing command after decorator"
        )
    # Use the same operator splitter so quote/escape handling matches
    # the rest of the parser exactly.  More than one segment means an
    # operator is present at the top level — reject with a clear error.
    pipe_parts = split_on_operators(text, ["|"])
    if len(pipe_parts) > 1:
        raise DecoratorParseError(
            f"@{decorator_name}: '|' in decorator body without braces "
            f"(wrap the pipeline in {{ ... }})"
        )
    seq_parts = split_on_operators(text, [";", "&&", "||"])
    if len(seq_parts) > 1:
        bad_op = seq_parts[1][0]
        raise DecoratorParseError(
            f"@{decorator_name}: {bad_op!r} in decorator body without braces "
            f"(wrap the statement in {{ ... }})"
        )
    cleaned, redirects = _extract_redirects(text)
    return Pipeline(stages=[Stage(text=cleaned.strip(), redirects=redirects)])


# ---------------------------------------------------------------------------
# Top-level parser
# ---------------------------------------------------------------------------

_SEQ_OPS = [";", "&&", "||"]


def _split_first(text: str) -> tuple[str, str | None, str]:
    """``(head, op, tail)``: *text* split at its first top-level `;` / `&&`
    / `||` (quote-aware); ``op`` is None when there is none."""
    parts = split_on_operators(text, _SEQ_OPS)
    if len(parts) == 1:
        return text, None, ""
    head, op = parts[0][1], parts[1][0]
    return head, op, text[len(head) + len(op):]


def _pipe_stages(text: str) -> list[Stage]:
    stages: list[Stage] = []
    for _, stage_text in split_on_operators(text, ["|"]):
        stage_text = stage_text.strip()
        if not stage_text:
            continue
        cleaned, redirects = _extract_redirects(stage_text)
        stages.append(Stage(text=cleaned.strip(), redirects=redirects))
    return stages


def parse_line(line: str) -> Sequence:
    """Parse a raw shell line into a Sequence of Pipelines.

    One item at a time, so that an item starting with a decorator — first or
    after `;` / `&&` / `||` — is peeled off with its braced scope intact
    before the rest of the line is split.
    """
    items: list[tuple[str | None, Pipeline]] = []
    op: str | None = None
    rest = line
    while rest.strip():
        deco_call, after = _extract_decorator_prefix(rest)
        if deco_call is not None:
            # `@deco {…} | more | stages && next`: the decorator stands in
            # as the first stage of its pipeline.
            stages = [Stage(text="", redirects=[], decorator=deco_call)]
            head, next_op, tail = _split_first(after)
            head = head.strip()
            if head:
                stages.extend(_pipe_stages(head[1:]))      # after the `|`
        else:
            head, next_op, tail = _split_first(rest)
            stages = _pipe_stages(head)
        if stages:
            items.append((op, Pipeline(stages=stages)))
        if next_op is None:
            break
        op, rest = next_op, tail
    return Sequence(items=items)
