"""High-level ZMK Studio RPC orchestration.

Wraps a StudioTransport with typed helpers for each operation we need:
- Device info
- Behavior list + details (for mapping source-code names to runtime IDs)
- Keymap get / set_layer_binding / save_changes / discard_changes

Behavior name resolution is the subtle part. ZMK Studio identifies
behaviors by numeric ID at the RPC level, but the source keymap refers
to them by short tokens (kp, mt, lt, mo, to, ...). The firmware reports
each behavior with a human-readable display_name (e.g. "Key Press",
"Mod-Tap"). We hardcode the short-token → display_name map for built-in
ZMK behaviors and fall through to "same name" for custom behaviors
declared via urob's ZMK_HOLD_TAP and friends (which default the
display-name to the node name).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .proto import studio_pb2
from .transport.base import StudioTransport

LOG = logging.getLogger(__name__)


# Source-code behavior token → runtime display_name.
# These come from ZMK's built-in behavior .dtsi files — each sets a
# display-name devicetree property that the firmware exposes via the
# behaviors subsystem.
BUILTIN_DISPLAY_NAMES: dict[str, str] = {
    "kp": "Key Press",
    "mt": "Mod-Tap",
    "lt": "Layer-Tap",
    "mo": "Momentary Layer",
    "to": "To Layer",
    "tog": "Toggle Layer",
    "trans": "Transparent",
    "none": "None",
    "key_repeat": "Key Repeat",
    "caps_word": "Caps Word",
    "sk": "Sticky Key",
    "sl": "Sticky Layer",
    "bt": "Bluetooth",
    "out": "Output Selection",
    "rgb_ug": "RGB Underglow",
    "sys_reset": "Reset",
    "bootloader": "Bootloader",
    "gresc": "Grave Escape",
    "ext_power": "Ext Power",
    "bl": "Backlight",
}


def source_token_to_display_name(token: str) -> str:
    """Map a source-code behavior token to its runtime display_name.

    For built-in ZMK behaviors this uses the BUILTIN_DISPLAY_NAMES map.
    For custom behaviors (smart_num, smart_shft, num_word, ...) it falls
    through to the token itself, since urob's ZMK_HOLD_TAP and friends
    don't set an explicit display-name so the firmware defaults to the
    device name, which equals the token.
    """
    return BUILTIN_DISPLAY_NAMES.get(token, token)


@dataclass
class BehaviorSummary:
    id: int
    display_name: str


@dataclass
class BindingTuple:
    layer_id: int
    position: int
    behavior_id: int
    param1: int
    param2: int


class StudioRPC:
    """Typed wrapper around a StudioTransport for ZMK Studio operations."""

    def __init__(self, transport: StudioTransport):
        self.transport = transport
        self._next_request_id = 1
        # Populated lazily on first call to resolve_behavior_name
        self._behaviors_by_display_name: dict[str, int] | None = None
        self._behaviors_by_id: dict[int, BehaviorSummary] | None = None

    def _new_request(self) -> studio_pb2.Request:
        req = studio_pb2.Request()
        req.request_id = self._next_request_id
        self._next_request_id += 1
        return req

    async def get_device_info(self) -> studio_pb2.GetDeviceInfoResponse:
        req = self._new_request()
        req.core.get_device_info = True
        resp = await self.transport.send_request(req)
        return resp.request_response.core.get_device_info

    async def _load_behavior_catalog(self) -> None:
        """Populate the behavior id ↔ display_name caches."""
        if self._behaviors_by_display_name is not None:
            return

        req = self._new_request()
        req.behaviors.list_all_behaviors = True
        resp = await self.transport.send_request(req)
        ids = list(resp.request_response.behaviors.list_all_behaviors.behaviors)

        by_name: dict[str, int] = {}
        by_id: dict[int, BehaviorSummary] = {}
        for behavior_id in ids:
            details_req = self._new_request()
            details_req.behaviors.get_behavior_details.behavior_id = behavior_id
            details_resp = await self.transport.send_request(details_req)
            details = details_resp.request_response.behaviors.get_behavior_details
            display_name = details.display_name
            by_name[display_name] = behavior_id
            by_id[behavior_id] = BehaviorSummary(
                id=behavior_id, display_name=display_name
            )
            LOG.debug("Behavior id=%d display_name=%r", behavior_id, display_name)
        self._behaviors_by_display_name = by_name
        self._behaviors_by_id = by_id

    async def list_behaviors(self) -> dict[str, int]:
        """Return {display_name: id} for all behaviors compiled into the firmware."""
        await self._load_behavior_catalog()
        assert self._behaviors_by_display_name is not None
        return dict(self._behaviors_by_display_name)

    async def behavior_summary(self, behavior_id: int) -> BehaviorSummary | None:
        await self._load_behavior_catalog()
        assert self._behaviors_by_id is not None
        return self._behaviors_by_id.get(behavior_id)

    async def resolve_behavior(self, source_token: str) -> int:
        """Return the runtime behavior ID for a source-code token.

        Raises KeyError if the behavior isn't present in the firmware.
        """
        await self._load_behavior_catalog()
        assert self._behaviors_by_display_name is not None
        display_name = source_token_to_display_name(source_token)
        if display_name not in self._behaviors_by_display_name:
            available = sorted(self._behaviors_by_display_name)
            raise KeyError(
                f"Behavior {source_token!r} (looking for display_name "
                f"{display_name!r}) not found in firmware. "
                f"Available behaviors: {available}"
            )
        return self._behaviors_by_display_name[display_name]

    async def get_keymap(self) -> studio_pb2.Keymap:
        req = self._new_request()
        req.keymap.get_keymap = True
        resp = await self.transport.send_request(req)
        return resp.request_response.keymap.get_keymap

    async def set_layer_binding(
        self, layer_id: int, position: int, behavior_id: int, p1: int, p2: int
    ) -> None:
        req = self._new_request()
        req.keymap.set_layer_binding.layer_id = layer_id
        req.keymap.set_layer_binding.key_position = position
        req.keymap.set_layer_binding.binding.behavior_id = behavior_id
        req.keymap.set_layer_binding.binding.param1 = p1
        req.keymap.set_layer_binding.binding.param2 = p2
        resp = await self.transport.send_request(req)

        status = resp.request_response.keymap.set_layer_binding
        # SET_LAYER_BINDING_RESP_OK = 0
        if status != 0:
            code_name = {
                0: "OK",
                1: "INVALID_LOCATION",
                2: "INVALID_BEHAVIOR",
                3: "INVALID_PARAMETERS",
            }.get(status, f"UNKNOWN_{status}")
            raise RuntimeError(
                f"set_layer_binding(layer={layer_id}, pos={position}, "
                f"behavior={behavior_id}, p1=0x{p1:X}, p2=0x{p2:X}) "
                f"returned {code_name}"
            )

    async def save_changes(self) -> None:
        req = self._new_request()
        req.keymap.save_changes = True
        resp = await self.transport.send_request(req)
        save = resp.request_response.keymap.save_changes
        which = save.WhichOneof("result")
        if which == "err":
            err_map = {
                0: "OK",
                1: "GENERIC",
                2: "NOT_SUPPORTED",
                3: "NO_SPACE",
            }
            code = err_map.get(save.err, f"UNKNOWN_{save.err}")
            raise RuntimeError(f"save_changes failed: {code}")
        # ok path is fine

    async def discard_changes(self) -> None:
        req = self._new_request()
        req.keymap.discard_changes = True
        await self.transport.send_request(req)

    async def check_unsaved_changes(self) -> bool:
        """Return True if the device has unsaved changes pending in memory."""
        req = self._new_request()
        req.keymap.check_unsaved_changes = True
        resp = await self.transport.send_request(req)
        return bool(resp.request_response.keymap.check_unsaved_changes)
