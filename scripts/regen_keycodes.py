#!/usr/bin/env python3
"""Regenerate scripts/zmk_studio_push/keycodes.py from upstream ZMK headers.

Fetches dt-bindings headers from zmkfirmware/zmk main branch, parses
#define NAME (expression) lines, evaluates the expressions in a restricted
Python namespace, and writes a frozen lookup table for the push/pull script.

Usage:
    python3 scripts/regen_keycodes.py [--ref <git-ref>]

Default ref is "main". Uses `gh api` to fetch files so it requires the gh
CLI to be installed and authenticated.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

HEADER_PATHS = [
    "app/include/dt-bindings/zmk/hid_usage_pages.h",
    "app/include/dt-bindings/zmk/hid_usage.h",
    "app/include/dt-bindings/zmk/modifiers.h",
    "app/include/dt-bindings/zmk/keys.h",
]

OUTPUT_PATH = Path(__file__).parent / "zmk_studio_push" / "keycodes.py"


def fetch_file(ref: str, path: str) -> str:
    """Fetch a file from zmkfirmware/zmk via gh api."""
    result = subprocess.run(
        ["gh", "api", f"repos/zmkfirmware/zmk/contents/{path}?ref={ref}", "--jq", ".content"],
        capture_output=True,
        text=True,
        check=True,
    )
    return base64.b64decode(result.stdout.strip()).decode("utf-8")


def fetch_commit_sha(ref: str) -> str:
    """Fetch the resolved commit SHA for a ref."""
    result = subprocess.run(
        ["gh", "api", f"repos/zmkfirmware/zmk/commits/{ref}", "--jq", ".sha"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


# Regex matching #define NAME (expression)  — allows the expression to span
# nested parentheses and also handles the common `#define NAME VALUE` without
# outer parens (for deprecation warnings etc.).
DEFINE_RE = re.compile(
    r"^\s*#define\s+([A-Z_][A-Z0-9_]*)\s+(.+?)\s*(?://.*)?$",
    re.MULTILINE,
)


def parse_defines(text: str) -> tuple[list[tuple[str, str]], set[str]]:
    """Return (pairs, deprecated_names).

    Pairs are (name, raw_expr) in source order, stripping block comments.
    Line comments are preserved on the original text so we can detect
    DEPRECATED markers before stripping them.
    """
    # Collapse line-continuation backslashes first so multi-line #defines
    # become single lines.
    text = re.sub(r"\\\n\s*", " ", text)
    # Strip /* ... */ block comments
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    pairs: list[tuple[str, str]] = []
    deprecated: set[str] = set()
    # Use a line-by-line scan so we can see any trailing // DEPRECATED
    # comment on the same line as a #define.
    for line in text.splitlines():
        m = re.match(r"^\s*#define\s+([A-Z_][A-Z0-9_]*)\s+(.+?)\s*(//.*)?$", line)
        if not m:
            continue
        name = m.group(1)
        expr = m.group(2).strip().rstrip(";").strip()
        comment = m.group(3) or ""
        if "DEPRECATED" in comment.upper():
            deprecated.add(name)
        if "(" in name:
            continue
        pairs.append((name, expr))
    return pairs, deprecated


def build_eval_namespace() -> dict:
    """Build the namespace for eval()-ing ZMK #define expressions."""

    def zmk_hid_usage(page, usage_id):
        return (page << 16) | usage_id

    def apply_mods(mods, keycode):
        return (mods << 24) | keycode

    ns: dict = {
        "__builtins__": {},
        "ZMK_HID_USAGE": zmk_hid_usage,
        "APPLY_MODS": apply_mods,
    }
    # Modifier functions will be added after we parse modifiers.h
    return ns


def evaluate_defines(pairs: list[tuple[str, str]], namespace: dict) -> dict[str, int]:
    """Evaluate each #define expression in sequence, adding to namespace.

    Returns only the keys that resolved to integer values (ignores things
    that reference undefined macros or produce non-int results).
    """
    resolved: dict[str, int] = {}
    for name, expr in pairs:
        # Clean up trailing C comments and fix up common patterns
        expr = re.sub(r"//.*$", "", expr).strip()
        # Remove trailing semicolons just in case
        expr = expr.rstrip(";").strip()
        # Try Python eval. ZMK expressions use | for OR and << for shift which
        # Python handles identically.
        try:
            value = eval(expr, namespace)  # noqa: S307  controlled input
        except Exception:
            continue
        if isinstance(value, int):
            resolved[name] = value & 0xFFFFFFFF
            namespace[name] = resolved[name]
        elif callable(value):
            # Allow modifier functions to be added to namespace (e.g. LC, LS)
            namespace[name] = value
    return resolved


def install_modifier_functions(namespace: dict) -> None:
    """After parsing modifiers.h, install LC/LS/LA/LG/RC/RS/RA/RG as Python fns."""
    mod_map = {
        "LC": "MOD_LCTL",
        "LS": "MOD_LSFT",
        "LA": "MOD_LALT",
        "LG": "MOD_LGUI",
        "RC": "MOD_RCTL",
        "RS": "MOD_RSFT",
        "RA": "MOD_RALT",
        "RG": "MOD_RGUI",
    }
    for fn_name, mod_name in mod_map.items():
        mod_bit = namespace.get(mod_name)
        if mod_bit is None:
            continue

        def _make_fn(bit):
            def _fn(keycode):
                return (bit << 24) | keycode
            return _fn

        namespace[fn_name] = _make_fn(mod_bit)


def generate_output(
    keycodes: dict[str, int], ref: str, sha: str, modifier_bits: dict[str, int]
) -> str:
    """Format the final keycodes.py file."""
    lines: list[str] = []
    lines.append('"""Frozen ZMK keycode symbol → 32-bit encoded integer lookup.')
    lines.append("")
    lines.append("This file is GENERATED by scripts/regen_keycodes.py from ZMK's")
    lines.append("dt-bindings headers. DO NOT EDIT by hand — re-run the generator.")
    lines.append("")
    lines.append(f"ZMK ref:    {ref}")
    lines.append(f"ZMK commit: {sha}")
    lines.append(f"Generated:  {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append('"""')
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("# Encoding: (modifier_byte << 24) | (hid_usage_page << 16) | hid_usage_id")
    lines.append("")
    lines.append("# Modifier bits (shifted left by 24 when OR'd into a keycode).")
    lines.append("MODIFIER_BITS: dict[str, int] = {")
    for fn_name in ("LC", "LS", "LA", "LG", "RC", "RS", "RA", "RG"):
        mod_const = {
            "LC": "MOD_LCTL",
            "LS": "MOD_LSFT",
            "LA": "MOD_LALT",
            "LG": "MOD_LGUI",
            "RC": "MOD_RCTL",
            "RS": "MOD_RSFT",
            "RA": "MOD_RALT",
            "RG": "MOD_RGUI",
        }[fn_name]
        bit = modifier_bits.get(mod_const, 0)
        lines.append(f'    "{fn_name}": 0x{bit:02X},')
    lines.append("}")
    lines.append("")
    lines.append("# Symbol → encoded keycode integer (32-bit).")
    lines.append("KEYCODES: dict[str, int] = {")
    for name in sorted(keycodes):
        value = keycodes[name]
        lines.append(f'    "{name}": 0x{value:08X},')
    lines.append("}")
    lines.append("")
    lines.append("")
    lines.append("def resolve_keycode(symbol: str) -> int:")
    lines.append('    """Look up a ZMK keycode symbol. Raises KeyError if unknown."""')
    lines.append("    return KEYCODES[symbol]")
    lines.append("")
    lines.append("")
    lines.append("def apply_modifier(modifier_fn: str, keycode: int) -> int:")
    lines.append('    """Apply a modifier function like LG/LS/LC/LA to a keycode."""')
    lines.append("    mod_bit = MODIFIER_BITS[modifier_fn]")
    lines.append("    return (mod_bit << 24) | keycode")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="main", help="Git ref of zmkfirmware/zmk")
    args = parser.parse_args()

    print(f"Fetching headers from zmkfirmware/zmk@{args.ref}...")
    sha = fetch_commit_sha(args.ref)
    print(f"Resolved ref to commit: {sha}")

    namespace = build_eval_namespace()
    # Process headers in dependency order so later headers can reference
    # symbols defined earlier.
    all_resolved: dict[str, int] = {}
    all_deprecated: set[str] = set()
    for path in HEADER_PATHS:
        text = fetch_file(args.ref, path)
        print(f"  parsing {path} ({len(text.splitlines())} lines)")
        pairs, deprecated = parse_defines(text)
        all_deprecated.update(deprecated)
        resolved = evaluate_defines(pairs, namespace)
        all_resolved.update(resolved)
        # After modifiers.h, install the LC/LS/etc. functions in the namespace
        if "modifiers.h" in path:
            install_modifier_functions(namespace)

    # Filter out HID_USAGE_* and MOD_* constants which aren't end-user
    # keycodes, and drop any symbols explicitly marked DEPRECATED in their
    # source header.
    filtered = {
        name: value
        for name, value in all_resolved.items()
        if not name.startswith("HID_USAGE_")
        and not name.startswith("MOD_")
        and name not in ("SELECT_MODS", "STRIP_MODS", "APPLY_MODS", "ZMK_HID_USAGE")
        and name not in all_deprecated
    }

    modifier_bits = {
        k: v for k, v in all_resolved.items() if k.startswith("MOD_")
    }

    output = generate_output(filtered, args.ref, sha, modifier_bits)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(output)
    print(f"Wrote {OUTPUT_PATH} with {len(filtered)} keycode symbols.")


if __name__ == "__main__":
    main()
