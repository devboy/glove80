"""Tests for push/pull/restore flows using a fake transport."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from zmk_studio_push.flows import (
    SCHEMA_VERSION,
    Snapshot,
    _describe_binding,
    _format_diff,
    pull,
    push,
    restore,
)
from zmk_studio_push.parser import ParsedBinding
from zmk_studio_push.proto import keymap_pb2, studio_pb2
from zmk_studio_push.rpc import StudioRPC
from zmk_studio_push.tests.test_rpc import FakeTransport
from zmk_studio_push.transport.base import StudioTransport


class FakeDevice(FakeTransport):
    """Extended fake that also tracks a keymap state.

    Layers are stored with EXPLICIT, distinct IDs (starting at 100) so
    tests can verify that set_layer_binding requests use the runtime
    Layer.id rather than the array index.
    """

    def __init__(self):
        super().__init__()
        self._layers: list[dict] = []  # [{id, name, bindings}]
        self._pending_changes: dict[tuple[int, int], tuple[int, int, int]] = {}
        self._has_unsaved = False

    def add_layer(
        self,
        name: str,
        bindings: list[tuple[int, int, int]],
        layer_id: int | None = None,
    ) -> None:
        if layer_id is None:
            layer_id = 100 + len(self._layers)  # 100, 101, 102, ...
        self._layers.append({"id": layer_id, "name": name, "bindings": bindings})

    def set_unsaved_changes(self, has_unsaved: bool) -> None:
        self._has_unsaved = has_unsaved

    def _layer_by_id(self, layer_id: int) -> dict | None:
        for layer in self._layers:
            if layer["id"] == layer_id:
                return layer
        return None

    def _build_keymap_response(self) -> keymap_pb2.Keymap:
        km = keymap_pb2.Keymap()
        for layer_info in self._layers:
            layer = km.layers.add()
            layer.id = layer_info["id"]
            layer.name = layer_info["name"]
            for bid, p1, p2 in layer_info["bindings"]:
                binding = layer.bindings.add()
                binding.behavior_id = bid
                binding.param1 = p1
                binding.param2 = p2
        return km

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
            if kind == "get_keymap":
                resp.request_response.keymap.get_keymap.CopyFrom(self._build_keymap_response())
            elif kind == "set_layer_binding":
                req = request.keymap.set_layer_binding
                # Look up layer by ID (not array index!) — this is the
                # behavior we expect from a real ZMK device.
                layer_info = self._layer_by_id(req.layer_id)
                if layer_info is None:
                    # INVALID_LOCATION
                    resp.request_response.keymap.set_layer_binding = 1
                else:
                    pos = req.key_position
                    bindings = layer_info["bindings"]
                    if 0 <= pos < len(bindings):
                        bindings[pos] = (
                            req.binding.behavior_id,
                            req.binding.param1,
                            req.binding.param2,
                        )
                        resp.request_response.keymap.set_layer_binding = 0
                    else:
                        resp.request_response.keymap.set_layer_binding = 1
            elif kind == "save_changes":
                resp.request_response.keymap.save_changes.ok = True
                self._has_unsaved = False
            elif kind == "discard_changes":
                resp.request_response.keymap.discard_changes = True
                self._has_unsaved = False
            elif kind == "check_unsaved_changes":
                resp.request_response.keymap.check_unsaved_changes = self._has_unsaved
        return resp


class TestSnapshot:
    def test_round_trip_json(self):
        snap = Snapshot(
            timestamp="2026-04-08T15-30-00Z",
            keyboard="toucan",
            device_name="fake",
            device_serial=b"\x01\x02",
            behaviors=[{"id": 0, "display_name": "Key Press"}],
            layers=[
                {
                    "index": 0,
                    "name": "Base",
                    "bindings": [
                        {"position": 0, "behavior_display_name": "Key Press", "params": [4, 0]}
                    ],
                }
            ],
        )
        data = snap.to_json()
        assert data["schema_version"] == SCHEMA_VERSION
        assert data["device"]["serial_number"] == "0102"

        reloaded = Snapshot.from_json(data)
        assert reloaded.keyboard == "toucan"
        assert reloaded.device_serial == b"\x01\x02"
        assert reloaded.layers == snap.layers

    def test_rejects_wrong_schema_version(self):
        with pytest.raises(ValueError, match="schema_version"):
            Snapshot.from_json({"schema_version": 99})


class TestDescribeBinding:
    def test_trans(self):
        assert _describe_binding("trans", 0, 0) == "&trans"

    def test_none(self):
        assert _describe_binding("none", 0, 0) == "&none"

    def test_kp_known_keycode(self):
        # F5 = 0x0007003E
        assert _describe_binding("kp", 0x0007003E, 0) == "&kp F5"

    def test_kp_unknown_keycode_shows_hex(self):
        assert _describe_binding("kp", 0xDEADBEEF, 0) == "&kp 0xDEADBEEF"

    def test_mo(self):
        assert _describe_binding("mo", 4, 0) == "&mo 4"

    def test_mt(self):
        # LCTRL=0x000700E0, BSPC=0x0007002A
        assert _describe_binding("mt", 0x000700E0, 0x0007002A) == "&mt LCTRL BSPC"


class TestPullFlow:
    @pytest.fixture
    def device(self) -> FakeDevice:
        d = FakeDevice()
        d.add_behavior(1, "Key Press")
        d.add_behavior(2, "Transparent")
        d.add_behavior(3, "Momentary Layer")
        # Layer 0 with 3 bindings
        d.add_layer("Base", [(1, 0x00070004, 0), (2, 0, 0), (3, 1, 0)])
        # Layer 1
        d.add_layer("Sym", [(2, 0, 0), (1, 0x00070005, 0), (2, 0, 0)])
        return d

    @pytest.mark.asyncio
    async def test_pull_writes_snapshot(
        self, device: FakeDevice, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Patch discover to return our fake
        async def fake_discover(preference):
            return device

        monkeypatch.setattr("zmk_studio_push.flows.discover", fake_discover)

        output_path = await pull(tmp_path, transport="auto")
        assert output_path.exists()
        data = json.loads(output_path.read_text())
        assert data["schema_version"] == SCHEMA_VERSION
        assert data["keyboard"] == "toucan"
        assert len(data["layers"]) == 2
        assert data["layers"][0]["name"] == "Base"
        assert len(data["layers"][0]["bindings"]) == 3
        assert data["layers"][0]["bindings"][0]["behavior_display_name"] == "Key Press"
        assert data["layers"][0]["bindings"][0]["params"] == [0x00070004, 0]


class TestPushFlow:
    """Test the push flow using a tiny crafted keymap + fake device."""

    @pytest.fixture
    def fake_keymap_file(self, tmp_path: Path) -> Path:
        source = """
        #define DEF 0
        #define SYM 1
        / { keymap {
            compatible = "zmk,keymap";
            layer_Base {
                display-name = "Base";
                bindings = < &kp A &kp B &trans >;
            };
            layer_Sym {
                display-name = "Sym";
                bindings = < &trans &kp N1 &kp N2 >;
            };
        }; };
        """
        path = tmp_path / "test.keymap"
        path.write_text(source)
        return path

    @pytest.fixture
    def device(self) -> FakeDevice:
        d = FakeDevice()
        d.add_behavior(1, "Key Press")
        d.add_behavior(2, "Transparent")
        # Initial state: Base layer has &kp Q, &kp B, &trans
        d.add_layer("Base", [(1, 0x00070014, 0), (1, 0x00070005, 0), (2, 0, 0)])
        d.add_layer("Sym", [(2, 0, 0), (1, 0x0007001E, 0), (1, 0x0007001F, 0)])
        return d

    @pytest.mark.asyncio
    async def test_push_dry_run_no_changes_written(
        self,
        fake_keymap_file: Path,
        device: FakeDevice,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        async def fake_discover(preference):
            return device

        monkeypatch.setattr("zmk_studio_push.flows.discover", fake_discover)

        initial_state = [list(layer["bindings"]) for layer in device._layers]
        await push(
            keymap_path=fake_keymap_file,
            backup_dir=tmp_path / "backups",
            transport="auto",
            dry_run=True,
            expected_key_count=3,
        )
        # After dry run the device state should be unchanged
        assert [list(layer["bindings"]) for layer in device._layers] == initial_state

    @pytest.mark.asyncio
    async def test_push_applies_changes(
        self,
        fake_keymap_file: Path,
        device: FakeDevice,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        async def fake_discover(preference):
            return device

        monkeypatch.setattr("zmk_studio_push.flows.discover", fake_discover)

        await push(
            keymap_path=fake_keymap_file,
            backup_dir=tmp_path / "backups",
            transport="auto",
            dry_run=False,
            expected_key_count=3,
        )
        # Base layer position 0 should now be &kp A (0x00070004)
        assert device._layers[0]["bindings"][0] == (1, 0x00070004, 0)
        # Base layer position 1 was already B, position 2 trans — unchanged
        assert device._layers[0]["bindings"][1] == (1, 0x00070005, 0)
        assert device._layers[0]["bindings"][2] == (2, 0, 0)

    @pytest.mark.asyncio
    async def test_push_writes_backup(
        self,
        fake_keymap_file: Path,
        device: FakeDevice,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        async def fake_discover(preference):
            return device

        monkeypatch.setattr("zmk_studio_push.flows.discover", fake_discover)

        backup_dir = tmp_path / "backups"
        await push(
            keymap_path=fake_keymap_file,
            backup_dir=backup_dir,
            transport="auto",
            dry_run=False,
            expected_key_count=3,
        )
        backups = list(backup_dir.glob("*.json"))
        assert len(backups) >= 1

    @pytest.mark.asyncio
    async def test_push_aborts_on_structure_mismatch(
        self,
        fake_keymap_file: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        device = FakeDevice()
        device.add_behavior(1, "Key Press")
        device.add_behavior(2, "Transparent")
        # Only ONE layer on device, but source has two
        device.add_layer("Base", [(1, 0x00070004, 0), (1, 0x00070005, 0), (2, 0, 0)])

        async def fake_discover(preference):
            return device

        monkeypatch.setattr("zmk_studio_push.flows.discover", fake_discover)

        with pytest.raises(RuntimeError, match="Structure mismatch"):
            await push(
                keymap_path=fake_keymap_file,
                backup_dir=tmp_path / "backups",
                transport="auto",
                expected_key_count=3,
            )


class TestPushUsesRuntimeLayerId:
    """C1 regression test: push must use Layer.id, not array index."""

    @pytest.fixture
    def fake_keymap_file(self, tmp_path: Path) -> Path:
        source = """
        #define DEF 0
        / { keymap {
            compatible = "zmk,keymap";
            layer_Base {
                display-name = "Base";
                bindings = < &kp A &kp B >;
            };
            layer_Other {
                display-name = "Other";
                bindings = < &kp C &kp D >;
            };
        }; };
        """
        path = tmp_path / "test.keymap"
        path.write_text(source)
        return path

    @pytest.mark.asyncio
    async def test_set_layer_binding_uses_runtime_layer_id(
        self,
        fake_keymap_file: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Layer IDs are 100, 200 — array indices are 0, 1. The push must
        send set_layer_binding with layer_id=100/200, not 0/1."""
        device = FakeDevice()
        device.add_behavior(1, "Key Press")
        # Layers with non-default IDs (not just 100/101)
        device.add_layer(
            "Base", [(1, 0x00070014, 0), (1, 0x00070015, 0)], layer_id=100
        )
        device.add_layer(
            "Other", [(1, 0x00070016, 0), (1, 0x00070017, 0)], layer_id=200
        )

        async def fake_discover(preference):
            return device

        monkeypatch.setattr("zmk_studio_push.flows.discover", fake_discover)

        await push(
            keymap_path=fake_keymap_file,
            backup_dir=tmp_path / "backups",
            transport="auto",
            expected_key_count=2,
        )
        # Verify that set_layer_binding requests used layer IDs 100 and 200
        sets = [
            r.keymap.set_layer_binding
            for r in device.requests
            if r.WhichOneof("subsystem") == "keymap"
            and r.keymap.WhichOneof("request_type") == "set_layer_binding"
        ]
        layer_ids_used = {s.layer_id for s in sets}
        # The source-code differences are: pos 0 of Base (Q→A), pos 1 of
        # Base (Y→B is wrong, X→B is wrong, B is fine; let's say the diff
        # produces changes for Base and Other layers).
        assert 100 in layer_ids_used or 200 in layer_ids_used, (
            f"Expected at least one runtime layer ID 100 or 200, got: {layer_ids_used}"
        )
        # Crucially: array indices 0 and 1 should NOT appear as layer_id
        assert 0 not in layer_ids_used
        assert 1 not in layer_ids_used


class TestPushAbortsOnUnsavedChanges:
    """H3 regression test: push refuses if unsaved changes exist (without --force)."""

    @pytest.fixture
    def fake_keymap_file(self, tmp_path: Path) -> Path:
        source = """
        #define DEF 0
        / { keymap {
            compatible = "zmk,keymap";
            layer_Base {
                bindings = < &kp A &trans &trans >;
            };
        }; };
        """
        path = tmp_path / "test.keymap"
        path.write_text(source)
        return path

    @pytest.mark.asyncio
    async def test_aborts_without_force(
        self,
        fake_keymap_file: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        device = FakeDevice()
        device.add_behavior(1, "Key Press")
        device.add_behavior(2, "Transparent")
        device.add_layer("Base", [(1, 0x00070004, 0), (2, 0, 0), (2, 0, 0)])
        device.set_unsaved_changes(True)

        async def fake_discover(preference):
            return device

        monkeypatch.setattr("zmk_studio_push.flows.discover", fake_discover)

        with pytest.raises(RuntimeError, match="unsaved changes"):
            await push(
                keymap_path=fake_keymap_file,
                backup_dir=tmp_path / "backups",
                transport="auto",
                expected_key_count=3,
                force=False,
            )

    @pytest.mark.asyncio
    async def test_proceeds_with_force(
        self,
        fake_keymap_file: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        device = FakeDevice()
        device.add_behavior(1, "Key Press")
        device.add_behavior(2, "Transparent")
        device.add_layer("Base", [(1, 0x00070014, 0), (2, 0, 0), (2, 0, 0)])
        device.set_unsaved_changes(True)

        async def fake_discover(preference):
            return device

        monkeypatch.setattr("zmk_studio_push.flows.discover", fake_discover)

        await push(
            keymap_path=fake_keymap_file,
            backup_dir=tmp_path / "backups",
            transport="auto",
            expected_key_count=3,
            force=True,
        )
        # Should have applied the change (Q→A at position 0)
        assert device._layers[0]["bindings"][0] == (1, 0x00070004, 0)


class TestRestoreFlow:
    @pytest.fixture
    def device(self) -> FakeDevice:
        d = FakeDevice()
        d.add_behavior(1, "Key Press")
        d.add_behavior(2, "Transparent")
        d.add_layer(
            "Base",
            [(1, 0x00070004, 0), (1, 0x00070005, 0), (2, 0, 0)],
            layer_id=42,  # arbitrary distinct ID to confirm restore uses it
        )
        return d

    @pytest.fixture
    def backup_file(self, tmp_path: Path) -> Path:
        data = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": "2026-04-08T12-00-00Z",
            "keyboard": "toucan",
            "device": {"name": "x", "serial_number": ""},
            "behaviors": [
                {"id": 1, "display_name": "Key Press"},
                {"id": 2, "display_name": "Transparent"},
            ],
            "layers": [
                {
                    "index": 0,
                    "name": "Base",
                    "bindings": [
                        {"position": 0, "behavior_display_name": "Transparent", "params": [0, 0]},
                        {"position": 1, "behavior_display_name": "Transparent", "params": [0, 0]},
                        {"position": 2, "behavior_display_name": "Key Press", "params": [0x00070020, 0]},
                    ],
                }
            ],
        }
        path = tmp_path / "backup.json"
        path.write_text(json.dumps(data))
        return path

    @pytest.mark.asyncio
    async def test_restore_applies_backup(
        self,
        device: FakeDevice,
        backup_file: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        async def fake_discover(preference):
            return device

        monkeypatch.setattr("zmk_studio_push.flows.discover", fake_discover)

        await restore(
            backup_path=backup_file,
            backup_dir=tmp_path / "backups",
            transport="auto",
        )
        # All positions should now match the backup
        assert device._layers[0]["bindings"][0] == (2, 0, 0)  # Transparent
        assert device._layers[0]["bindings"][1] == (2, 0, 0)  # Transparent
        assert device._layers[0]["bindings"][2] == (1, 0x00070020, 0)  # Key Press N3
