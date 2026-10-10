"""Configurable key bindings and user line-editor actions (discussion #29).

One table maps *action names* to keys.  A name is dotted with the surface it
belongs to — ``prompt.history_search``, ``picker.next``, ``switcher.new`` —
so one flat namespace covers every surface (the shape XeFM's
``KEY_BINDINGS`` uses).  ``config.py`` changes it with two calls::

    from eosh import keys

    keys.bind("prompt.history_search", "Ctrl-S")     # replace its keys
    keys.bind("picker.next", ["Down", "Ctrl-J"])
    keys.bind("prompt.clear_screen", [])             # unbind

    @keys.action("insert_last_arg", keys="Alt-.")   # a new prompt action
    def insert_last_arg(ctx):
        ...

In ``bind`` a name without a surface means every surface that has an action
of that name (``"accept"`` is ``prompt.accept`` and ``picker.accept``), and
a dotted one narrows it to one surface, winning there over the undotted
entry whatever the call order — XeFM's rule.  A bound key wins over any
default that used it, and a later ``bind`` wins over an earlier one.  A key name that doesn't parse, an action name that
doesn't exist, or a printable key (which typing must keep) is a
``config warning:`` naming the problem, never a silent no-op.  ``reload``
calls :func:`reset`.

There are no multi-stroke keys (``Ctrl-G b``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .user_errors import config_warning

#: The surfaces, in the order ``help keys`` lists them.
CONTEXTS = ("prompt", "picker", "switcher")


@dataclass(frozen=True)
class Action:
    name: str                       # dotted: "prompt.history_search"
    description: str
    default_keys: tuple[str, ...]
    func: Callable | None = None    # a user action's function; None for built-ins

    @property
    def context(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def short_name(self) -> str:
        return self.name.split(".", 1)[1]

    @property
    def is_user(self) -> bool:
        return self.func is not None


_BUILTINS: dict[str, Action] = {}


def _builtin(name: str, keys: Iterable[str], description: str) -> None:
    _BUILTINS[name] = Action(name, description, tuple(keys))


# ── built-in actions ─────────────────────────────────────────────────────────
# Names follow readline's where one exists.  Definition order is the order
# `help keys` lists them in.

_builtin("prompt.accept", ["Enter", "Ctrl-J"], "run the line")
_builtin("prompt.complete", ["Tab"], "open completion for the word at the caret")
_builtin("prompt.history_search", ["Ctrl-R"], "search the shared history")
_builtin("prompt.previous_history", ["Up", "Ctrl-P"], "previous line of this context's history")
_builtin("prompt.next_history", ["Down", "Ctrl-N"], "next line of this context's history")
_builtin("prompt.switch_context", ["Ctrl-]"],
         "open the context switcher (also while a command runs)")
_builtin("prompt.beginning_of_line", ["Ctrl-A", "Home"], "move to the start of the line")
_builtin("prompt.end_of_line", ["Ctrl-E", "End"],
         "move to the end of the line; there, accept the suggestion")
_builtin("prompt.backward_char", ["Ctrl-B", "Left"], "move one character left")
_builtin("prompt.forward_char", ["Ctrl-F", "Right"],
         "move one character right; at the end, accept the suggestion")
_builtin("prompt.backward_word", ["Alt-B"], "move one word left")
_builtin("prompt.forward_word", ["Alt-F"],
         "move one word right; at the end, accept a word of the suggestion")
_builtin("prompt.backward_delete_char", ["Backspace"], "delete the character before the caret")
_builtin("prompt.delete_char", ["Delete"], "delete the character at the caret")
_builtin("prompt.backward_kill_word", ["Ctrl-W"], "delete the word before the caret")
_builtin("prompt.kill_line", ["Ctrl-K"], "delete to the end of the line")
_builtin("prompt.backward_kill_line", ["Ctrl-U"], "delete to the start of the line")
_builtin("prompt.clear_screen", ["Ctrl-L"], "clear the screen")
_builtin("prompt.eof", ["Ctrl-D"], "on an empty line, exit the shell")
_builtin("prompt.interrupt", ["Ctrl-C"], "discard the line")

_builtin("picker.accept", ["Enter", "Ctrl-J"], "choose the highlighted row")
_builtin("picker.cancel", ["Esc"], "close without choosing")
_builtin("picker.interrupt", ["Ctrl-C"], "close without choosing (an interrupt)")
_builtin("picker.previous", ["Up", "Ctrl-P"], "highlight the row above")
_builtin("picker.next", ["Down", "Ctrl-N"], "highlight the row below")
_builtin("picker.complete", ["Tab"], "type the candidates' shared prefix")
_builtin("picker.backward_delete_char", ["Backspace"], "erase one typed character")

# The context switcher is a picker: these are checked first, then picker.*.
_builtin("switcher.new", ["Ctrl-N"], "create a context")
_builtin("switcher.delete", ["Ctrl-D"], "delete the highlighted context")
_builtin("switcher.rename", ["Ctrl-R"], "rename the highlighted context")


# ── user state (reset by reload) ─────────────────────────────────────────────

_user_actions: dict[str, Action] = {}
#: Keys set with bind(), by the name as written (dotted, or a bare short
#: name meaning every surface), in call order (a re-bind moves to the end).
_bindings: dict[str, tuple[str, ...]] = {}
_tables: dict[str, dict[bytes, tuple[str, str]]] = {}   # context -> seq -> (action, spec)


def reset() -> None:
    """Drop everything a config bound or registered (``reload``)."""
    _user_actions.clear()
    _bindings.clear()
    _tables.clear()


def _qualify(name: str) -> str:
    return name if "." in name else f"prompt.{name}"


def get_action(name: str) -> Action | None:
    """The action named *name* (``prompt.`` assumed when undotted); a user
    action of the same name shadows the built-in."""
    name = _qualify(name)
    return _user_actions.get(name) or _BUILTINS.get(name)


def bind(action: str, keys: str | Iterable[str]) -> None:
    """Make *keys* the keys of *action*, replacing its defaults.

    *action* is dotted (``"picker.accept"``: that surface only) or bare
    (``"accept"``: every surface with an action of that name).  On a surface
    where both are bound, the dotted entry wins.  ``[]`` unbinds.  A key
    already used by another action of the same surface moves to this one.
    """
    if not _targets(action):
        config_warning(f"keys.bind: unknown action {action!r}"
                       f" (`help keys` lists them)")
        return
    specs = (keys,) if isinstance(keys, str) else tuple(keys)
    _bindings.pop(action, None)
    _bindings[action] = tuple(s for s in specs if _check_spec(s, action))
    _tables.clear()


def _targets(name: str) -> list[Action]:
    """The actions a bind() entry named *name* applies to."""
    if "." in name:
        a = get_action(name)
        return [a] if a else []
    return [a for ctx in CONTEXTS for a in _actions_in(ctx) if a.short_name == name]


def _binding_for(a: Action) -> tuple[str, ...] | None:
    """The keys bind() gave *a* — its dotted entry, else its bare one."""
    if a.name in _bindings:
        return _bindings[a.name]
    return _bindings.get(a.short_name)


def action(name: str, *, keys: str | Iterable[str] = (), help: str = "",
           override: bool = False) -> Callable[[Callable], Callable]:
    """Register the decorated ``func(ctx)`` as a prompt action.

    *ctx* is the line editor's :class:`~eosh.lineedit.EditorContext`.  A
    built-in's name needs ``override=True``; inside it,
    ``ctx.invoke(<same name>)`` runs the built-in, so an action can wrap one.
    """
    def decorate(func: Callable) -> Callable:
        full = _qualify(name)
        if full.split(".", 1)[0] != "prompt":
            config_warning(f"keys.action: {name!r}: only prompt.* actions can"
                           f" be defined (for now)")
            return func
        if full in _BUILTINS and not override:
            config_warning(f"keys.action: {name!r} is a built-in action;"
                           f" pass override=True to replace it")
            return func
        specs = (keys,) if isinstance(keys, str) else tuple(keys)
        default = _BUILTINS[full].default_keys if full in _BUILTINS and not specs else ()
        valid = tuple(s for s in specs if _check_spec(s, full)) or default
        description = help or (func.__doc__ or "").strip().split("\n")[0]
        _user_actions[full] = Action(full, description, valid, func)
        _tables.clear()
        return func
    return decorate


def _check_spec(spec: str, action_name: str) -> bool:
    try:
        seqs = parse_key(spec)
    except ValueError as e:
        config_warning(f"keys: {action_name}: {e}")
        return False
    if any(_is_printable(s) for s in seqs):
        config_warning(f"keys: {action_name}: {spec!r} is a printable key, which"
                       f" types itself there; add Ctrl- or Alt-")
        return False
    return True


# ── lookup ───────────────────────────────────────────────────────────────────

def _actions_in(context: str) -> list[Action]:
    names = [n for n in _BUILTINS if n.startswith(context + ".")]
    names += [n for n in _user_actions if n.startswith(context + ".") and n not in _BUILTINS]
    return [get_action(n) for n in names]


def _table(context: str) -> dict[bytes, tuple[str, str]]:
    table = _tables.get(context)
    if table is not None:
        return table
    table = {}

    def put(name: str, specs: Iterable[str]) -> None:
        for spec in specs:
            for seq in parse_key(spec):
                table[seq] = (name, spec)

    # Built-in defaults first, then a user action's own keys, then every
    # bind() in call order — each later source takes a key from an earlier.
    # A bare entry skips an action whose dotted entry exists: that one wins.
    actions = _actions_in(context)
    for a in actions:
        if _binding_for(a) is None and not a.is_user:
            put(a.name, a.default_keys)
    for a in actions:
        if _binding_for(a) is None and a.is_user:
            put(a.name, a.default_keys)
    for name, specs in _bindings.items():
        for a in actions:
            if name == a.name or (name == a.short_name and a.name not in _bindings):
                put(a.name, specs)
    _tables[context] = table
    return table


def lookup(context: str, key: bytes) -> str | None:
    """The action *key* (one :func:`terminal.read_key` result) triggers on
    *context*, as a full dotted name."""
    hit = _table(context).get(key)
    return hit[0] if hit else None


def sequences(name: str) -> list[bytes]:
    """Every byte sequence that triggers *name* — for the forwarding loops,
    which scan raw input for the context-switch key."""
    name = _qualify(name)
    table = _table(name.split(".", 1)[0])
    return [seq for seq, (n, _) in table.items() if n == name]


def key_names(name: str) -> list[str]:
    """The key names currently triggering *name*, as written in the config
    and in that order — minus any a later binding took away."""
    action = get_action(name)
    if action is None:
        return []
    table = _table(action.context)
    specs = _binding_for(action)
    if specs is None:
        specs = action.default_keys
    return [spec for spec in dict.fromkeys(specs)
            if any(table.get(seq) == (action.name, spec) for seq in parse_key(spec))]


def hint(name: str, label: str) -> str:
    """``"^N new"`` for a status bar, or ``""`` when *name* has no key."""
    names = key_names(name)
    if not names:
        return ""
    spec = names[0]
    short = f"^{spec[5:].upper()}" if spec.lower().startswith("ctrl-") and len(spec) == 6 else spec
    return f"{short} {label}"


def listing() -> list[tuple[Action, list[str]]]:
    """Every action, by surface, with the keys it currently has."""
    return [(a, key_names(a.name)) for ctx in CONTEXTS for a in _actions_in(ctx)]


# ── key names → bytes ────────────────────────────────────────────────────────
# "Ctrl-R", "Alt-.", "Ctrl-Alt-H", "Shift-Tab", "PageUp", "F5".  Modifiers and
# names are case-insensitive; a bare letter is lowercase unless Shift- is
# given (Alt-B is ESC b, Alt-Shift-B is ESC B).  The bytes are what
# terminal.read_key returns — Windows scan codes are translated to the same
# sequences there.

_MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "meta": "alt",
              "option": "alt", "shift": "shift"}

# Named keys: (unmodified sequences, xterm "CSI 1;m X" final byte or
# "CSI n;m ~" number, for the modified form).
_NAMED: dict[str, tuple[tuple[bytes, ...], str | None]] = {
    "enter": ((b"\r",), None),          # raw mode: the key sends CR; LF is Ctrl-J
    "tab": ((b"\t",), None),
    "esc": ((b"\x1b",), None),
    "backspace": ((b"\x7f", b"\x08"), None),
    "space": ((b" ",), None),
    "up": ((b"\x1b[A", b"\x1bOA"), "A"),
    "down": ((b"\x1b[B", b"\x1bOB"), "B"),
    "right": ((b"\x1b[C", b"\x1bOC"), "C"),
    "left": ((b"\x1b[D", b"\x1bOD"), "D"),
    "home": ((b"\x1b[H", b"\x1b[1~", b"\x1bOH"), "H"),
    "end": ((b"\x1b[F", b"\x1b[4~", b"\x1bOF"), "F"),
    "insert": ((b"\x1b[2~",), "2~"),
    "delete": ((b"\x1b[3~",), "3~"),
    "pageup": ((b"\x1b[5~",), "5~"),
    "pagedown": ((b"\x1b[6~",), "6~"),
    "f1": ((b"\x1bOP",), "P"),
    "f2": ((b"\x1bOQ",), "Q"),
    "f3": ((b"\x1bOR",), "R"),
    "f4": ((b"\x1bOS",), "S"),
}
for _n, _code in zip(range(5, 13), (15, 17, 18, 19, 20, 21, 23, 24)):
    _NAMED[f"f{_n}"] = ((f"\x1b[{_code}~".encode(),), f"{_code}~")
_ALIASES = {"return": "enter", "escape": "esc", "del": "delete", "ins": "insert",
            "pgup": "pageup", "pgdn": "pagedown", "page_up": "pageup",
            "page_down": "pagedown"}

_CTRL_PUNCT = {"@": 0x00, " ": 0x00, "[": 0x1B, "\\": 0x1C, "]": 0x1D,
               "^": 0x1E, "_": 0x1F, "?": 0x7F}


def parse_key(spec: str) -> tuple[bytes, ...]:
    """The byte sequences *spec* names; ValueError says what's wrong."""
    if not isinstance(spec, str) or not spec:
        raise ValueError(f"{spec!r} is not a key name")
    if spec.endswith("--"):
        mod_part, key = spec[:-2], "-"
    elif spec == "-":
        mod_part, key = "", "-"
    else:
        mod_part, _, key = spec.rpartition("-")
        if not key:
            raise ValueError(f"{spec!r}: no key after the modifiers")
    mods: set[str] = set()
    for part in filter(None, mod_part.split("-")) if mod_part else ():
        mod = _MODIFIERS.get(part.lower())
        if mod is None:
            raise ValueError(f"{spec!r}: unknown modifier {part!r}"
                             f" (use Ctrl, Alt or Shift)")
        mods.add(mod)

    lower = _ALIASES.get(key.lower(), key.lower())
    if len(key) > 1 or lower in ("space",):
        if lower not in _NAMED:
            raise ValueError(f"{spec!r}: unknown key {key!r}")
        return _named(spec, lower, mods)
    return _char(spec, key, mods)


def _named(spec: str, name: str, mods: set[str]) -> tuple[bytes, ...]:
    plain, modified = _NAMED[name]
    if not mods:
        return plain
    if name == "tab" and mods == {"shift"}:
        return (b"\x1b[Z",)
    if name == "space" and mods == {"ctrl"}:
        return (b"\x00",)
    if mods == {"alt"} and modified is None:
        return tuple(b"\x1b" + seq for seq in plain)     # Alt-Enter, Alt-Backspace
    if modified is None:
        raise ValueError(f"{spec!r}: a terminal sends no distinct code for it")
    m = 1 + ("shift" in mods) + 2 * ("alt" in mods) + 4 * ("ctrl" in mods)
    if modified.endswith("~"):
        return (f"\x1b[{modified[:-1]};{m}~".encode(),)
    return (f"\x1b[1;{m}{modified}".encode(),)


def _char(spec: str, ch: str, mods: set[str]) -> tuple[bytes, ...]:
    if "shift" in mods:
        if "ctrl" in mods or not ch.isalpha():
            raise ValueError(f"{spec!r}: a terminal sends no distinct code for it"
                             f" (write the shifted character itself)")
        ch = ch.upper()
    elif ch.isalpha() and ch.isascii():
        ch = ch.lower()
    if "ctrl" in mods:
        if ch.isascii() and ch.isalpha():
            code = bytes([ord(ch.lower()) - 0x60])
        elif ch in _CTRL_PUNCT:
            code = bytes([_CTRL_PUNCT[ch]])
        else:
            raise ValueError(f"{spec!r}: a terminal sends no distinct code for it")
    else:
        code = ch.encode("utf-8")
    return (b"\x1b" + code,) if "alt" in mods else (code,)


def _is_printable(seq: bytes) -> bool:
    try:
        text = seq.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return len(text) == 1 and text.isprintable()
