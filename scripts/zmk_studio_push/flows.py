"""Push, pull, and restore flows — the top-level user-facing operations."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import keycodes as keycodes_module
from .parser import ParsedBinding, ParsedKeymap, parse_keymap_file
from .proto import keymap_pb2, studio_pb2
from .rpc import BUILTIN_DISPLAY_NAMES, StudioRPC, source_token_to_display_name
from .transport.discover import Preference, discover

LOG = logging.getLogger(__name__)

SCHEMA_VERSION = 1
TOUCAN_KEY_COUNT = 42

# Reverse keycode table for pretty-printing in diffs.
_KEYCODE_BY_VALUE: dict[int, str] | None = None


def _reverse_keycodes() -> dict[int, str]:
    global _KEYCODE_BY_VALUE
    if _KEYCODE_BY_VALUE is None:
        # Prefer shorter, more common names when there are aliases
        pref_order: dict[int, str] = {}
        aliases: dict[int, list[str]] = {}
        for name, value in keycodes_module.KEYCODES.items():
            aliases.setdefault(value, []).append(name)
        for value, names in aliases.items():
            # Sort by length ascending then alphabetical for stability
            names.sort(key=lambda n: (len(n), n))
            pref_order[value] = names[0]
        _KEYCODE_BY_VALUE = pref_order
    return _KEYCODE_BY_VALUE


# Modifier bit → wrapper function name. Used by _format_keycode to
# render compound keycodes like LG(N1) instead of raw 0x0807001E.
_MOD_BIT_TO_FN = {
    bit: fn
    for fn, bit in keycodes_module.MODIFIER_BITS.items()
}


def _format_keycode(value: int) -> str:
    """Render a 32-bit ZMK keycode back to source-style text.

    Handles modifier-wrapped keycodes by detecting bits in [31:24] and
    wrapping a known base keycode in LG()/LS()/LC()/LA() etc. If the
    base keycode isn't in the symbol table, falls back to the raw hex
    literal — without modifier wrapping, since arbitrary garbage values
    shouldn't get spuriously decoded as modifier combinations.
    """
    rev = _reverse_keycodes()
    if value in rev:
        return rev[value]
    base = value & 0x00FFFFFF
    mod_byte = (value >> 24) & 0xFF
    if mod_byte != 0 and base in rev:
        inner = rev[base]
        for bit_value in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01):
            if mod_byte & bit_value:
                fn = _MOD_BIT_TO_FN.get(bit_value)
                if fn is None:
                    continue  # shouldn't happen for ZMK's 8 standard mods
                inner = f"{fn}({inner})"
        return inner
    return f"0x{value:08X}"


def _describe_binding(behavior_name: str, p1: int, p2: int) -> str:
    """Render a (behavior, p1, p2) tuple back to source-style text."""
    if behavior_name == "trans":
        return "&trans"
    if behavior_name == "none":
        return "&none"
    if behavior_name == "kp":
        return f"&kp {_format_keycode(p1)}"
    if behavior_name == "mo":
        return f"&mo {p1}"
    if behavior_name == "to":
        return f"&to {p1}"
    if behavior_name == "mt":
        return f"&mt {_format_keycode(p1)} {_format_keycode(p2)}"
    if behavior_name == "lt":
        return f"&lt {p1} {_format_keycode(p2)}"
    return f"&{behavior_name} {p1} {p2}".strip()


def _make_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


@dataclass
class Snapshot:
    """In-memory representation of a backup / current keymap."""

    timestamp: str
    keyboard: str
    device_name: str
    device_serial: bytes
    behaviors: list[dict]  # [{id, display_name, num_params}]
    layers: list[dict]  # [{index, name, bindings: [...]}]

    def to_json(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "timestamp": self.timestamp,
            "keyboard": self.keyboard,
            "device": {
                "name": self.device_name,
                "serial_number": self.device_serial.hex(),
            },
            "behaviors": self.behaviors,
            "layers": self.layers,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Snapshot":
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported schema_version {data.get('schema_version')} "
                f"(expected {SCHEMA_VERSION})"
            )
        device = data.get("device", {})
        return cls(
            timestamp=data.get("timestamp", ""),
            keyboard=data.get("keyboard", ""),
            device_name=device.get("name", ""),
            device_serial=bytes.fromhex(device.get("serial_number", "") or ""),
            behaviors=list(data.get("behaviors", [])),
            layers=list(data.get("layers", [])),
        )


async def _snapshot_current(rpc: StudioRPC, keyboard: str) -> Snapshot:
    """Pull the current keymap from the device into a Snapshot."""
    info = await rpc.get_device_info()
    await rpc.list_behaviors()  # populates caches

    # Build behaviors list from RPC cache
    assert rpc._behaviors_by_id is not None  # populated by list_behaviors
    behaviors_list = []
    for bid, summary in sorted(rpc._behaviors_by_id.items()):
        behaviors_list.append(
            {
                "id": bid,
                "display_name": summary.display_name,
            }
        )

    keymap = await rpc.get_keymap()
    layers_json = []
    for layer_idx, layer in enumerate(keymap.layers):
        bindings_json = []
        for pos, binding in enumerate(layer.bindings):
            behavior_id = binding.behavior_id
            summary = rpc._behaviors_by_id.get(behavior_id)
            display_name = summary.display_name if summary else f"id:{behavior_id}"
            bindings_json.append(
                {
                    "position": pos,
                    "behavior_display_name": display_name,
                    "params": [binding.param1, binding.param2],
                }
            )
        layers_json.append(
            {
                "index": layer_idx,
                "id": layer.id,  # stable firmware-assigned ID, used by RPC writes
                "name": layer.name or f"layer_{layer_idx}",
                "bindings": bindings_json,
            }
        )

    return Snapshot(
        timestamp=_make_timestamp(),
        keyboard=keyboard,
        device_name=info.name,
        device_serial=bytes(info.serial_number),
        behaviors=behaviors_list,
        layers=layers_json,
    )


def _write_snapshot(snapshot: Snapshot, backup_dir: Path, keyboard: str) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"{keyboard}-{snapshot.timestamp}.json"
    path.write_text(json.dumps(snapshot.to_json(), indent=2) + "\n")
    return path


async def pull(
    output_dir: Path, transport: Preference = "auto", keyboard: str = "toucan"
) -> Path:
    """Fetch current keymap from device and write a JSON snapshot."""
    LOG.info("Discovering device (transport=%s)...", transport)
    xport = await discover(transport)
    try:
        LOG.info("Connected to %s", xport.name)
        rpc = StudioRPC(xport)
        snapshot = await _snapshot_current(rpc, keyboard)
        output_path = _write_snapshot(snapshot, Path(output_dir), keyboard)
        LOG.info(
            "Saved snapshot to %s (%d layers, %d behaviors)",
            output_path,
            len(snapshot.layers),
            len(snapshot.behaviors),
        )
        return output_path
    finally:
        await xport.close()


def _parsed_layers_match_runtime(
    parsed: ParsedKeymap, runtime: keymap_pb2.Keymap
) -> tuple[bool, str]:
    """Check layer count and binding count per layer match."""
    if len(parsed.layers) != len(runtime.layers):
        return False, (
            f"Source has {len(parsed.layers)} layers but firmware has "
            f"{len(runtime.layers)}. Rebuild firmware first."
        )
    for parsed_layer, runtime_layer in zip(parsed.layers, runtime.layers):
        if len(parsed_layer.bindings) != len(runtime_layer.bindings):
            return False, (
                f"Layer '{parsed_layer.name}' has {len(parsed_layer.bindings)} "
                f"source bindings but {len(runtime_layer.bindings)} runtime bindings. "
                f"Rebuild firmware first."
            )
    return True, ""


async def _apply_bindings(
    rpc: StudioRPC,
    changes: list[tuple[int, int, int, int, int]],  # (layer, pos, behavior_id, p1, p2)
) -> None:
    """Send set_layer_binding for each change, then save_changes.

    On any error, discard_changes and re-raise.
    """
    await rpc.discard_changes()  # clean slate
    try:
        for layer_id, pos, behavior_id, p1, p2 in changes:
            await rpc.set_layer_binding(layer_id, pos, behavior_id, p1, p2)
        await rpc.save_changes()
    except Exception:
        try:
            await rpc.discard_changes()
        except Exception as cleanup_exc:
            LOG.warning("discard_changes during cleanup also failed: %s", cleanup_exc)
        raise


@dataclass
class DiffEntry:
    """One position-level diff between source and runtime keymap state."""

    layer_runtime_id: int   # Stable Layer.id from firmware (used by set_layer_binding)
    layer_index: int        # Source-order index (matches array position in keymap)
    layer_name: str
    position: int
    current: ParsedBinding  # What's on the device today
    new: ParsedBinding      # What the source says it should be


def _compute_diff(
    parsed: ParsedKeymap,
    runtime: keymap_pb2.Keymap,
    rpc: StudioRPC,
) -> list[DiffEntry]:
    """Position-by-position diff between source and runtime.

    Layers are matched by array position (the structure check guarantees
    counts agree before this is called). The `layer_runtime_id` field is
    populated from `runtime_layer.id` so callers can issue
    set_layer_binding against a stable firmware-assigned ID rather than
    an array index — important if Studio has reordered layers since boot.
    """
    assert rpc._behaviors_by_id is not None

    # Build the display_name → source token inverse map directly from
    # BUILTIN_DISPLAY_NAMES so the two tables can never drift.
    token_by_display_name: dict[str, str] = {
        display_name: token for token, display_name in BUILTIN_DISPLAY_NAMES.items()
    }

    def id_to_token(behavior_id: int) -> str:
        summary = rpc._behaviors_by_id.get(behavior_id)
        if summary is None:
            return f"id:{behavior_id}"
        display = summary.display_name
        return token_by_display_name.get(display, display)

    changes: list[DiffEntry] = []
    for layer_idx, (parsed_layer, runtime_layer) in enumerate(
        zip(parsed.layers, runtime.layers)
    ):
        for pos, (new, current) in enumerate(
            zip(parsed_layer.bindings, runtime_layer.bindings)
        ):
            current_token = id_to_token(current.behavior_id)
            current_parsed = ParsedBinding(
                behavior_name=current_token,
                param1=current.param1,
                param2=current.param2,
            )
            if new != current_parsed:
                changes.append(
                    DiffEntry(
                        layer_runtime_id=runtime_layer.id,
                        layer_index=layer_idx,
                        layer_name=parsed_layer.name,
                        position=pos,
                        current=current_parsed,
                        new=new,
                    )
                )
    return changes


def _format_diff(diff: list[DiffEntry]) -> str:
    lines: list[str] = []
    current_layer_idx: int | None = None
    for entry in diff:
        if entry.layer_index != current_layer_idx:
            if current_layer_idx is not None:
                lines.append("")
            lines.append(
                f"{entry.layer_name} layer (index {entry.layer_index}, "
                f"runtime id {entry.layer_runtime_id}):"
            )
            current_layer_idx = entry.layer_index
        cur_str = _describe_binding(
            entry.current.behavior_name, entry.current.param1, entry.current.param2
        )
        new_str = _describe_binding(
            entry.new.behavior_name, entry.new.param1, entry.new.param2
        )
        lines.append(f"  pos {entry.position:3}: {cur_str:30} → {new_str}")
    return "\n".join(lines)


async def push(
    keymap_path: Path,
    backup_dir: Path,
    transport: Preference = "auto",
    dry_run: bool = False,
    keyboard: str = "toucan",
    expected_key_count: int = TOUCAN_KEY_COUNT,
    force: bool = False,
) -> None:
    """Parse keymap source and push all bindings to the device."""
    keymap_path = Path(keymap_path)
    backup_dir = Path(backup_dir)

    LOG.info("Parsing %s", keymap_path)
    parsed = parse_keymap_file(keymap_path, expected_key_count=expected_key_count)
    LOG.info(
        "Parsed %d layers, %d total bindings",
        len(parsed.layers),
        sum(len(l.bindings) for l in parsed.layers),
    )

    LOG.info("Discovering device (transport=%s)...", transport)
    xport = await discover(transport)
    try:
        LOG.info("Connected to %s", xport.name)
        rpc = StudioRPC(xport)

        # Refuse to push if the device has unsaved changes — those are
        # almost always edits-in-flight from the Studio web UI in another
        # session, and our discard_changes call below would silently nuke
        # them. --force lets the user override.
        try:
            has_unsaved = await rpc.check_unsaved_changes()
        except Exception as exc:
            LOG.debug("check_unsaved_changes failed: %s", exc)
            has_unsaved = False
        if has_unsaved and not force:
            raise RuntimeError(
                "Device has unsaved changes pending — these were likely made "
                "in the ZMK Studio web UI. Save or discard them there first, "
                "or pass --force to overwrite them. Use `make pull-toucan` "
                "before forcing if you want to keep a copy."
            )

        # Validate behavior names against runtime catalog BEFORE touching anything
        await rpc.list_behaviors()
        resolved_behaviors: dict[str, int] = {}
        for layer in parsed.layers:
            for binding in layer.bindings:
                if binding.behavior_name not in resolved_behaviors:
                    try:
                        behavior_id = await rpc.resolve_behavior(binding.behavior_name)
                        resolved_behaviors[binding.behavior_name] = behavior_id
                    except KeyError as exc:
                        raise RuntimeError(
                            f"Cannot push: {exc}"
                        ) from exc

        # Fetch runtime keymap for diff + structure check
        runtime_keymap = await rpc.get_keymap()
        ok, err = _parsed_layers_match_runtime(parsed, runtime_keymap)
        if not ok:
            raise RuntimeError(f"Structure mismatch: {err}")

        # Auto-backup
        snapshot = await _snapshot_current(rpc, keyboard)
        backup_path = _write_snapshot(snapshot, backup_dir, keyboard)
        LOG.info("Auto-backup saved to %s", backup_path)

        # Compute diff
        diff = _compute_diff(parsed, runtime_keymap, rpc)
        if not diff:
            LOG.info("No changes needed — source and device already match.")
            return

        diff_text = _format_diff(diff)
        print(diff_text)
        print()
        print(
            f"Total: {len(diff)} binding(s) changed across "
            f"{len({entry.layer_index for entry in diff})} layer(s)."
        )

        if dry_run:
            LOG.info("Dry run — not applying changes.")
            return

        # Translate diff entries into (layer_id, pos, behavior_id, p1, p2)
        # tuples. layer_id is the runtime Layer.id, NOT the array index.
        changes = [
            (
                entry.layer_runtime_id,
                entry.position,
                resolved_behaviors[entry.new.behavior_name],
                entry.new.param1,
                entry.new.param2,
            )
            for entry in diff
        ]
        LOG.info("Applying %d changes...", len(changes))
        await _apply_bindings(rpc, changes)
        LOG.info("Pushed %d changes, saved to device.", len(changes))
        LOG.info("Backup at %s", backup_path)
    finally:
        await xport.close()


async def restore(
    backup_path: Path,
    backup_dir: Path,
    transport: Preference = "auto",
    dry_run: bool = False,
    keyboard: str = "toucan",
) -> None:
    """Restore a previously-pulled JSON snapshot."""
    backup_path = Path(backup_path)
    data = json.loads(backup_path.read_text())
    snapshot = Snapshot.from_json(data)
    LOG.info(
        "Loaded backup %s (%d layers, %d behaviors)",
        backup_path,
        len(snapshot.layers),
        len(snapshot.behaviors),
    )

    LOG.info("Discovering device (transport=%s)...", transport)
    xport = await discover(transport)
    try:
        LOG.info("Connected to %s", xport.name)
        rpc = StudioRPC(xport)
        await rpc.list_behaviors()
        assert rpc._behaviors_by_display_name is not None
        runtime_display_to_id = rpc._behaviors_by_display_name

        # Pull the current runtime keymap so we can map snapshot layer
        # indices to the device's CURRENT Layer.id values. (The snapshot's
        # `id` field captures what was true at backup time, but a layer
        # restore/reorder since then could have changed it. We re-resolve
        # by index here, which is safe because we also enforce the layer
        # count matches the snapshot below.)
        runtime_keymap = await rpc.get_keymap()
        if len(runtime_keymap.layers) != len(snapshot.layers):
            raise RuntimeError(
                f"Backup has {len(snapshot.layers)} layers but firmware has "
                f"{len(runtime_keymap.layers)}. Cannot restore — the layouts "
                f"differ. Rebuild firmware first, or restore with a matching "
                f"backup."
            )
        index_to_runtime_id = {
            i: layer.id for i, layer in enumerate(runtime_keymap.layers)
        }

        # Re-resolve every backup binding's display_name to current runtime IDs
        changes: list[tuple[int, int, int, int, int]] = []
        for layer in snapshot.layers:
            layer_idx = layer["index"]
            runtime_layer_id = index_to_runtime_id[layer_idx]
            for binding in layer["bindings"]:
                pos = binding["position"]
                display_name = binding["behavior_display_name"]
                if display_name not in runtime_display_to_id:
                    raise RuntimeError(
                        f"Behavior {display_name!r} from backup is not present in "
                        f"current firmware. Available: {sorted(runtime_display_to_id)}"
                    )
                behavior_id = runtime_display_to_id[display_name]
                p1, p2 = binding["params"]
                changes.append((runtime_layer_id, pos, behavior_id, p1, p2))

        # Auto-backup current state before restoring
        current_snapshot = await _snapshot_current(rpc, keyboard)
        current_backup = _write_snapshot(current_snapshot, Path(backup_dir), keyboard)
        LOG.info("Auto-backed-up current state to %s", current_backup)

        if dry_run:
            LOG.info("Dry run — would restore %d bindings. Not applying.", len(changes))
            return

        LOG.info("Restoring %d bindings...", len(changes))
        await _apply_bindings(rpc, changes)
        LOG.info("Restored from %s", backup_path)
    finally:
        await xport.close()
