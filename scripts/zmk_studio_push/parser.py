"""Parser for ZMK devicetree keymap files.

Converts `config/toucan.keymap` (and similar) into a ParsedKeymap object
that can be pushed over the ZMK Studio RPC protocol. This is NOT a general
devicetree parser — it's targeted at the specific syntax patterns used in
ZMK keymaps using urob's zmk-helpers conventions.

Key design decisions:
- Each behavior has a known, fixed parameter count listed in
  BEHAVIOR_PARAM_COUNTS. Unknown behaviors are a hard error.
- `#define` macros are expanded by literal text substitution, which
  handles both simple integer values (like layer indices) and multi-token
  expansions (like `_SMART_NUM` → `&smart_num NUM 0`).
- Modifier-wrapped keycodes (LG(X), LS(X), nested combinations) are
  evaluated recursively using the MODIFIER_BITS table from keycodes.py.
- Layer indices come from block order, matching ZMK's compiled layout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import keycodes


class ParseError(ValueError):
    """Raised when the keymap file cannot be parsed."""


@dataclass(frozen=True)
class ParsedBinding:
    behavior_name: str
    param1: int
    param2: int


@dataclass
class ParsedLayer:
    index: int
    name: str
    bindings: list[ParsedBinding] = field(default_factory=list)


@dataclass
class ParsedKeymap:
    layers: list[ParsedLayer] = field(default_factory=list)


# Behavior → number of positional params.
# Unknown behaviors will cause a hard error; add new entries here as needed.
BEHAVIOR_PARAM_COUNTS: dict[str, int] = {
    "kp": 1,
    "mt": 2,
    "lt": 2,
    "mo": 1,
    "to": 1,
    "tog": 1,
    "trans": 0,
    "none": 0,
    "sk": 1,
    "sl": 1,
    "caps_word": 0,
    "key_repeat": 0,
    # Custom behaviors from urob's zmk-helpers used in toucan.keymap:
    "smart_num": 2,     # from ZMK_HOLD_TAP(smart_num, ...) → expands _SMART_NUM
    "smart_shft": 0,    # from ZMK_MOD_MORPH(smart_shft, ...)
    "num_word": 1,      # from the zmk-auto-layer module
    "num_dance": 0,     # from ZMK_TAP_DANCE(num_dance, ...)
    # System behaviors:
    "sys_reset": 0,
    "bootloader": 0,
    "bt": 1,
    "out": 1,
    "rgb_ug": 1,
}


# Semantics of each argument position for known behaviors.
# - "keycode": resolve as keycode symbol (e.g. A, F7, C_MUTE, LG(N1))
# - "layer":   resolve via #define or integer literal
# - "int":     integer literal only (useful for numeric params)
BEHAVIOR_ARG_KINDS: dict[str, list[str]] = {
    "kp": ["keycode"],
    "mt": ["keycode", "keycode"],   # mod arg is also a keycode (HID mod usage)
    "lt": ["layer", "keycode"],
    "mo": ["layer"],
    "to": ["layer"],
    "tog": ["layer"],
    "trans": [],
    "none": [],
    "sk": ["keycode"],
    "sl": ["layer"],
    "caps_word": [],
    "key_repeat": [],
    "smart_num": ["layer", "int"],
    "smart_shft": [],
    "num_word": ["layer"],
    "num_dance": [],
    "sys_reset": [],
    "bootloader": [],
    "bt": ["int"],
    "out": ["int"],
    "rgb_ug": ["int"],
}


_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_SIMPLE_DEFINE_RE = re.compile(
    r"^\s*#define\s+([A-Za-z_][A-Za-z0-9_]*)\s+(.+)$",
    re.MULTILINE,
)
_LAYER_BLOCK_RE = re.compile(
    r"layer_([A-Za-z0-9_]+)\s*\{([^}]*)\};",
    re.DOTALL,
)
_BINDINGS_RE = re.compile(
    r"bindings\s*=\s*<\s*(.*?)\s*>\s*;",
    re.DOTALL,
)
_DISPLAY_NAME_RE = re.compile(r'display-name\s*=\s*"([^"]+)"')
# Modifier-wrapper function calls (possibly nested): LG(X), LS(LC(A)), ...
_MOD_FN_RE = re.compile(r"^([A-Z]{1,2})\((.*)\)$")


def parse_keymap_file(
    path: Path,
    expected_key_count: int,
    extra_behavior_params: dict[str, int] | None = None,
) -> ParsedKeymap:
    """Parse a .keymap file by path. See parse_keymap for options."""
    text = Path(path).read_text()
    return parse_keymap(
        text,
        expected_key_count=expected_key_count,
        extra_behavior_params=extra_behavior_params,
        source_name=str(path),
    )


def parse_keymap(
    source: str,
    expected_key_count: int,
    extra_behavior_params: dict[str, int] | None = None,
    source_name: str = "<string>",
) -> ParsedKeymap:
    """Parse keymap source text into a ParsedKeymap.

    Args:
        source: devicetree keymap text (the full file contents)
        expected_key_count: number of key positions per layer (e.g. 42 for
            toucan). Used to validate the parse was consistent.
        extra_behavior_params: additional {name: param_count} entries to
            merge into BEHAVIOR_PARAM_COUNTS (useful for custom behaviors
            in test fixtures).
        source_name: file path or identifier for error messages.
    """
    # Step 1: strip comments
    text = _BLOCK_COMMENT_RE.sub("", source)
    text = _LINE_COMMENT_RE.sub("", text)

    # Step 2: collect #define macros
    defines: dict[str, str] = {}
    for match in _SIMPLE_DEFINE_RE.finditer(text):
        name = match.group(1)
        value = match.group(2).strip()
        # Strip trailing inline slashes etc.
        defines[name] = value

    # Step 3: strip #include lines, they're irrelevant for parsing
    text = re.sub(r"^\s*#include[^\n]*", "", text, flags=re.MULTILINE)

    # Step 4: find all layer_NAME blocks in order
    #
    # Note: the _LAYER_BLOCK_RE is a very simple regex that doesn't handle
    # nested braces. Fortunately the typical keymap layer block only
    # contains a display-name and a bindings list, neither of which have
    # inner braces.
    layer_matches = list(_LAYER_BLOCK_RE.finditer(text))
    if not layer_matches:
        raise ParseError(f"{source_name}: no layer_XXX blocks found")

    # Step 5: for each layer block, extract bindings text and parse
    behavior_params = dict(BEHAVIOR_PARAM_COUNTS)
    if extra_behavior_params:
        behavior_params.update(extra_behavior_params)

    arg_kinds = dict(BEHAVIOR_ARG_KINDS)
    # Default any extra behavior to "int" args
    if extra_behavior_params:
        for name, count in extra_behavior_params.items():
            arg_kinds.setdefault(name, ["int"] * count)

    km = ParsedKeymap()
    for index, match in enumerate(layer_matches):
        layer_suffix = match.group(1)
        block_body = match.group(2)

        bindings_match = _BINDINGS_RE.search(block_body)
        if not bindings_match:
            raise ParseError(
                f"{source_name}: layer_{layer_suffix} has no bindings = <...> property"
            )
        bindings_text = bindings_match.group(1)

        name_match = _DISPLAY_NAME_RE.search(block_body)
        display_name = name_match.group(1) if name_match else layer_suffix

        # Expand #define macros inside the bindings text via simple
        # whole-word substitution, iterated until stable.
        bindings_text = _expand_macros(bindings_text, defines)

        bindings = _parse_bindings(
            bindings_text=bindings_text,
            defines=defines,
            behavior_params=behavior_params,
            arg_kinds=arg_kinds,
            source_name=source_name,
            layer_label=layer_suffix,
        )

        if len(bindings) != expected_key_count:
            raise ParseError(
                f"{source_name}: layer_{layer_suffix} has "
                f"{len(bindings)} bindings but expected key count "
                f"is {expected_key_count}"
            )

        km.layers.append(ParsedLayer(index=index, name=display_name, bindings=bindings))

    return km


def _expand_macros(text: str, defines: dict[str, str]) -> str:
    """Expand #define macros in text via iterated whole-word substitution.

    Iterated because a macro's expansion may itself reference another
    macro (e.g. `_SMART_NUM` → `&smart_num NUM 0`, then `NUM` → `3`).
    Stops when a pass makes no changes.
    """
    prev = None
    current = text
    max_iters = 10
    iteration = 0
    while prev != current and iteration < max_iters:
        prev = current
        for name, value in defines.items():
            # Whole-word replacement only; don't substitute inside longer
            # identifiers.
            current = re.sub(rf"\b{re.escape(name)}\b", value, current)
        iteration += 1
    return current


def _tokenize(text: str) -> list[str]:
    """Simple whitespace tokenizer that also splits around parens."""
    # Normalize all whitespace to a single space so parens are preserved
    text = text.replace("\n", " ").replace("\t", " ").strip()
    return [t for t in text.split() if t]


def _parse_bindings(
    bindings_text: str,
    defines: dict[str, str],
    behavior_params: dict[str, int],
    arg_kinds: dict[str, list[str]],
    source_name: str,
    layer_label: str,
) -> list[ParsedBinding]:
    tokens = _tokenize(bindings_text)
    bindings: list[ParsedBinding] = []
    iterator = iter(range(len(tokens)))

    def context() -> str:
        return f"{source_name}: layer_{layer_label}"

    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("&"):
            raise ParseError(
                f"{context()}: expected a behavior reference (starting with '&') "
                f"but found {token!r} at token position {i}"
            )
        name = token[1:]
        if name not in behavior_params:
            raise ParseError(
                f"{context()}: unknown behavior {token!r}. "
                f"Add it to BEHAVIOR_PARAM_COUNTS in parser.py."
            )
        n_params = behavior_params[name]
        kinds = arg_kinds.get(name, ["int"] * n_params)
        if len(kinds) != n_params:
            # Defensive: shouldn't happen if tables are in sync
            raise ParseError(
                f"{context()}: behavior {token!r} has "
                f"{n_params} params but {len(kinds)} arg kinds configured"
            )
        if i + n_params >= len(tokens):
            raise ParseError(
                f"{context()}: behavior {token!r} needs {n_params} "
                f"arguments but ran out of tokens"
            )
        args = tokens[i + 1 : i + 1 + n_params]
        resolved = [
            _resolve_arg(arg, kind, defines, context())
            for arg, kind in zip(args, kinds)
        ]
        param1 = resolved[0] if len(resolved) >= 1 else 0
        param2 = resolved[1] if len(resolved) >= 2 else 0
        bindings.append(ParsedBinding(behavior_name=name, param1=param1, param2=param2))
        i += 1 + n_params
    return bindings


def _resolve_arg(
    arg: str, kind: str, defines: dict[str, str], context: str
) -> int:
    """Resolve a single argument token to an integer value."""
    arg = arg.strip()
    # Integer literal (decimal or hex)
    if _is_int_literal(arg):
        return _parse_int_literal(arg)

    # Macro reference? Substitute once and retry.
    if arg in defines:
        substituted = defines[arg].strip()
        return _resolve_arg(substituted, kind, defines, context)

    # Keycode argument: look up in keycodes table, or evaluate modifier fn
    if kind == "keycode":
        return _resolve_keycode(arg, context)

    # Layer argument: integer literal or #define lookup already handled above
    if kind == "layer":
        raise ParseError(
            f"{context}: cannot resolve layer argument {arg!r} — "
            f"expected an integer literal or a defined layer name"
        )

    # "int" fallback: try to treat as numeric
    raise ParseError(
        f"{context}: cannot resolve {kind} argument {arg!r}"
    )


def _is_int_literal(token: str) -> bool:
    if not token:
        return False
    if token.startswith(("0x", "0X", "-0x", "-0X")):
        body = token.lstrip("-")[2:]
        return bool(body) and all(c in "0123456789abcdefABCDEF" for c in body)
    if token.startswith("-"):
        return token[1:].isdigit()
    return token.isdigit()


def _parse_int_literal(token: str) -> int:
    return int(token, 0)


def _resolve_keycode(arg: str, context: str) -> int:
    """Resolve a keycode argument, including LG(...), LS(LC(...)), etc."""
    # Bare symbol
    if arg in keycodes.KEYCODES:
        return keycodes.KEYCODES[arg]

    # Modifier wrapper: LG(X), LS(LC(A)), etc.
    # The inner expression might contain more parens, so use a smart parse.
    mod_match = _MOD_FN_RE.match(arg)
    if mod_match:
        fn = mod_match.group(1)
        inner = mod_match.group(2).strip()
        if fn not in keycodes.MODIFIER_BITS:
            raise ParseError(
                f"{context}: unknown modifier function {fn!r} in {arg!r}"
            )
        inner_val = _resolve_keycode(inner, context)
        mod_bit = keycodes.MODIFIER_BITS[fn]
        return (mod_bit << 24) | inner_val

    raise ParseError(
        f"{context}: unknown keycode symbol {arg!r} — cannot resolve "
        f"to an integer value. If this is a new keycode, regenerate "
        f"keycodes.py via scripts/regen_keycodes.py."
    )
