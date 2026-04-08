"""Tests for RPC layer — behavior name resolution, request serialization."""
from __future__ import annotations

import pytest

from zmk_studio_push.proto import studio_pb2
from zmk_studio_push.rpc import (
    BUILTIN_DISPLAY_NAMES,
    StudioRPC,
    source_token_to_display_name,
)
from zmk_studio_push.transport.base import StudioTransport


class FakeTransport(StudioTransport):
    """In-memory transport that mimics a device for unit tests."""

    def __init__(self):
        self.requests: list[studio_pb2.Request] = []
        self._behavior_catalog: list[tuple[int, str]] = []
        self._set_binding_status = 0  # OK
        self._save_status = "ok"

    def add_behavior(self, behavior_id: int, display_name: str) -> None:
        self._behavior_catalog.append((behavior_id, display_name))

    def set_binding_failure(self, status: int) -> None:
        self._set_binding_status = status

    def set_save_failure(self, err_code: int) -> None:
        self._save_status = ("err", err_code)

    async def send_request(self, request: studio_pb2.Request) -> studio_pb2.Response:
        self.requests.append(request)
        resp = studio_pb2.Response()
        resp.request_response.request_id = request.request_id

        subsystem = request.WhichOneof("subsystem")
        if subsystem == "core":
            kind = request.core.WhichOneof("request_type")
            if kind == "get_device_info":
                resp.request_response.core.get_device_info.name = "fake-toucan"
                resp.request_response.core.get_device_info.serial_number = b"\x01\x02"
        elif subsystem == "behaviors":
            kind = request.behaviors.WhichOneof("request_type")
            if kind == "list_all_behaviors":
                ids = [bid for bid, _ in self._behavior_catalog]
                resp.request_response.behaviors.list_all_behaviors.behaviors.extend(ids)
            elif kind == "get_behavior_details":
                bid = request.behaviors.get_behavior_details.behavior_id
                for catalog_id, name in self._behavior_catalog:
                    if catalog_id == bid:
                        resp.request_response.behaviors.get_behavior_details.id = bid
                        resp.request_response.behaviors.get_behavior_details.display_name = name
                        break
        elif subsystem == "keymap":
            kind = request.keymap.WhichOneof("request_type")
            if kind == "set_layer_binding":
                resp.request_response.keymap.set_layer_binding = self._set_binding_status
            elif kind == "save_changes":
                if self._save_status == "ok":
                    resp.request_response.keymap.save_changes.ok = True
                else:
                    _, code = self._save_status
                    resp.request_response.keymap.save_changes.err = code
            elif kind == "discard_changes":
                resp.request_response.keymap.discard_changes = True
            elif kind == "get_keymap":
                # Empty keymap
                pass
        return resp

    async def close(self) -> None:
        pass

    @property
    def name(self) -> str:
        return "fake:/dev/null"


class TestSourceTokenToDisplayName:
    def test_builtin_kp(self):
        assert source_token_to_display_name("kp") == "Key Press"

    def test_builtin_mt(self):
        assert source_token_to_display_name("mt") == "Mod-Tap"

    def test_builtin_trans(self):
        assert source_token_to_display_name("trans") == "Transparent"

    def test_unknown_falls_through_to_token(self):
        assert source_token_to_display_name("smart_num") == "smart_num"
        assert source_token_to_display_name("custom_behavior_xyz") == "custom_behavior_xyz"

    def test_all_builtins_cover_toucan_keymap(self):
        """Sanity: every token the toucan parser might emit for built-ins
        has an entry here so resolve_behavior won't fall through silently."""
        required = {"kp", "mt", "lt", "mo", "to", "trans", "none"}
        assert required.issubset(BUILTIN_DISPLAY_NAMES.keys())


class TestStudioRPC:
    @pytest.fixture
    def transport(self) -> FakeTransport:
        t = FakeTransport()
        t.add_behavior(1, "Key Press")
        t.add_behavior(2, "Mod-Tap")
        t.add_behavior(3, "Momentary Layer")
        t.add_behavior(4, "Transparent")
        t.add_behavior(5, "smart_num")  # Custom behavior
        return t

    @pytest.fixture
    def rpc(self, transport: FakeTransport) -> StudioRPC:
        return StudioRPC(transport)

    @pytest.mark.asyncio
    async def test_get_device_info(self, rpc: StudioRPC, transport: FakeTransport):
        info = await rpc.get_device_info()
        assert info.name == "fake-toucan"
        assert info.serial_number == b"\x01\x02"
        assert len(transport.requests) == 1

    @pytest.mark.asyncio
    async def test_list_behaviors_caches(
        self, rpc: StudioRPC, transport: FakeTransport
    ):
        mapping = await rpc.list_behaviors()
        assert mapping["Key Press"] == 1
        assert mapping["Mod-Tap"] == 2
        assert mapping["smart_num"] == 5

        # Second call doesn't issue fresh RPCs
        before = len(transport.requests)
        await rpc.list_behaviors()
        assert len(transport.requests) == before

    @pytest.mark.asyncio
    async def test_resolve_behavior_builtin(self, rpc: StudioRPC):
        assert await rpc.resolve_behavior("kp") == 1
        assert await rpc.resolve_behavior("mt") == 2
        assert await rpc.resolve_behavior("mo") == 3
        assert await rpc.resolve_behavior("trans") == 4

    @pytest.mark.asyncio
    async def test_resolve_behavior_custom(self, rpc: StudioRPC):
        assert await rpc.resolve_behavior("smart_num") == 5

    @pytest.mark.asyncio
    async def test_resolve_behavior_missing(self, rpc: StudioRPC):
        with pytest.raises(KeyError, match="not_in_firmware"):
            await rpc.resolve_behavior("not_in_firmware")

    @pytest.mark.asyncio
    async def test_set_layer_binding_success(
        self, rpc: StudioRPC, transport: FakeTransport
    ):
        await rpc.set_layer_binding(
            layer_id=0, position=5, behavior_id=1, p1=0x00070004, p2=0
        )
        # Find the set_layer_binding request
        sets = [
            r for r in transport.requests
            if r.WhichOneof("subsystem") == "keymap"
            and r.keymap.WhichOneof("request_type") == "set_layer_binding"
        ]
        assert len(sets) == 1
        req = sets[0].keymap.set_layer_binding
        assert req.layer_id == 0
        assert req.key_position == 5
        assert req.binding.behavior_id == 1
        assert req.binding.param1 == 0x00070004
        assert req.binding.param2 == 0

    @pytest.mark.asyncio
    async def test_set_layer_binding_failure(
        self, rpc: StudioRPC, transport: FakeTransport
    ):
        transport.set_binding_failure(1)  # INVALID_LOCATION
        with pytest.raises(RuntimeError, match="INVALID_LOCATION"):
            await rpc.set_layer_binding(0, 5, 1, 0, 0)

    @pytest.mark.asyncio
    async def test_save_changes_success(self, rpc: StudioRPC):
        await rpc.save_changes()  # shouldn't raise

    @pytest.mark.asyncio
    async def test_save_changes_failure(
        self, rpc: StudioRPC, transport: FakeTransport
    ):
        transport.set_save_failure(3)  # NO_SPACE
        with pytest.raises(RuntimeError, match="NO_SPACE"):
            await rpc.save_changes()

    @pytest.mark.asyncio
    async def test_discard_changes(self, rpc: StudioRPC, transport: FakeTransport):
        await rpc.discard_changes()
        discards = [
            r for r in transport.requests
            if r.WhichOneof("subsystem") == "keymap"
            and r.keymap.WhichOneof("request_type") == "discard_changes"
        ]
        assert len(discards) == 1

    @pytest.mark.asyncio
    async def test_request_ids_are_unique(
        self, rpc: StudioRPC, transport: FakeTransport
    ):
        await rpc.get_device_info()
        await rpc.get_device_info()
        await rpc.get_device_info()
        ids = [r.request_id for r in transport.requests]
        assert len(set(ids)) == len(ids)
