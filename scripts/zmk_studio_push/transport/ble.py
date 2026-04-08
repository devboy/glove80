"""BLE GATT transport using bleak."""
from __future__ import annotations

import asyncio
import logging
import os

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from ..framing import FrameReader, encode_frame
from ..proto import studio_pb2
from .base import StudioTransport, TransportError

LOG = logging.getLogger(__name__)

# Studio service UUID and its single bidirectional characteristic.
SERVICE_UUID = "00000000-0196-6107-c967-c5cfb1c2482a"
CHARACTERISTIC_UUID = "00000001-0196-6107-c967-c5cfb1c2482a"

DEFAULT_SCAN_TIMEOUT_S = 4.0
DEFAULT_REQUEST_TIMEOUT_S = 5.0

# Default device name substrings we treat as ZMK candidates. This is
# overrideable via the ZMK_STUDIO_BLE_NAME env var (set to a single
# substring) so the user can target a specific device or board.
DEFAULT_NAME_SUBSTRINGS = ("toucan", "glove80", "zmk")


def _name_matches(device: BLEDevice, substrings: tuple[str, ...]) -> bool:
    name = (device.name or "").lower()
    return any(s in name for s in substrings)


async def scan_for_studio_devices(
    timeout: float = DEFAULT_SCAN_TIMEOUT_S,
) -> list[BLEDevice]:
    """Scan for nearby BLE devices that look like ZMK Studio candidates.

    We deliberately do NOT filter by the Studio service UUID at scan time
    because ZMK keyboards typically only advertise their HID service to
    save power; the Studio GATT service is registered after connection
    but never broadcast. Scanning by service UUID would miss every real
    device. Instead, we scan unfiltered and match candidates by device
    name. The actual Studio characteristic is verified after we connect.

    Override the default name match with ZMK_STUDIO_BLE_NAME=<substring>.
    """
    name_override = os.environ.get("ZMK_STUDIO_BLE_NAME")
    substrings: tuple[str, ...]
    if name_override:
        substrings = (name_override.lower(),)
    else:
        substrings = DEFAULT_NAME_SUBSTRINGS

    LOG.debug("BLE scan: looking for devices whose name contains %s", substrings)
    devices = await BleakScanner.discover(timeout=timeout)
    visible = [d for d in devices if d is not None]
    LOG.debug(
        "BLE scan: found %d visible device(s): %s",
        len(visible),
        [(d.address, d.name) for d in visible],
    )
    matches = [d for d in visible if _name_matches(d, substrings)]
    if not matches and visible:
        LOG.info(
            "BLE scan saw %d device(s) but none matched name filter %s. "
            "Visible: %s. Override with ZMK_STUDIO_BLE_NAME=<substring> "
            "or ZMK_STUDIO_BLE_ADDR=<address>.",
            len(visible),
            substrings,
            [(d.address, d.name) for d in visible],
        )
    return matches


async def find_bonded_macos_devices() -> list[BLEDevice]:
    """macOS-specific: find ZMK devices already bonded to this Mac.

    Bonded BLE peripherals stop advertising once they're connected, so
    BleakScanner can't see them. CoreBluetooth's
    `retrieveConnectedPeripheralsWithServices:` API returns peripherals
    currently connected to the system that expose a given service. We
    query for the ZMK Studio service UUID to find paired ZMK keyboards
    that aren't currently advertising.

    Returns a list of BLEDevice objects suitable for passing to
    BleakClient. We construct them with `details=(peripheral, manager)`,
    which is the structure bleak's CoreBluetooth backend expects from
    its own scanner output. Crucially, we also keep the same
    CentralManagerDelegate alive (attached to the device list) so the
    CBPeripheral isn't released between discovery and connection.

    Returns an empty list on non-macOS platforms or when CoreBluetooth
    isn't available.
    """
    try:
        from bleak.backends.corebluetooth.CentralManagerDelegate import (
            CentralManagerDelegate,
        )
        from CoreBluetooth import CBUUID
    except ImportError:
        return []

    name_override = os.environ.get("ZMK_STUDIO_BLE_NAME")
    substrings: tuple[str, ...]
    if name_override:
        substrings = (name_override.lower(),)
    else:
        substrings = DEFAULT_NAME_SUBSTRINGS

    delegate = CentralManagerDelegate()
    await delegate.wait_until_ready()
    studio_uuid = CBUUID.UUIDWithString_(SERVICE_UUID)
    peripherals = delegate.central_manager.retrieveConnectedPeripheralsWithServices_(
        [studio_uuid]
    )
    results: list[BLEDevice] = []
    for p in peripherals:
        uuid = str(p.identifier().UUIDString())
        name = str(p.name() or "")
        LOG.debug("CoreBluetooth retrieve: id=%s name=%s", uuid, name)
        # Filter by name in case there are multiple ZMK devices and
        # the user wants a specific one. Empty names get through too —
        # better to probe than to silently ignore.
        if name and not any(s in name.lower() for s in substrings):
            continue
        device = BLEDevice(
            address=uuid,
            name=name,
            details=(p, delegate),
        )
        results.append(device)
    # The delegate is captured in `details`, which keeps the central
    # manager alive (and therefore the CBPeripheral alive) as long as
    # any returned BLEDevice is referenced by the caller.
    return results


class StudioBLE(StudioTransport):
    """Async BLE GATT transport for ZMK Studio devices."""

    def __init__(
        self,
        device: str | BLEDevice,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ):
        self._device = device
        self._request_timeout_s = request_timeout_s
        self._client: BleakClient | None = None
        self._reader = FrameReader()
        self._frame_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._address: str = ""
        # Captured in open() so the indication callback can dispatch back
        # to the right loop. Bleak callbacks may run on background threads
        # depending on the OS backend (BlueZ on Linux, WinRT on Windows),
        # so asyncio.Queue.put_nowait must be marshalled via
        # loop.call_soon_threadsafe.
        self._loop: asyncio.AbstractEventLoop | None = None

    async def open(self) -> None:
        if self._client is not None and self._client.is_connected:
            return
        self._loop = asyncio.get_running_loop()
        self._client = BleakClient(self._device)
        try:
            await self._client.connect()
        except Exception as exc:
            raise TransportError(
                f"Failed to connect to BLE device {self._device}: {exc}"
            ) from exc
        self._address = (
            getattr(self._device, "address", None) or str(self._device)
        )
        # Verify the Studio characteristic exists on this device. Since
        # we no longer filter at scan time, this is the moment we confirm
        # we're talking to a ZMK Studio peripheral.
        services = self._client.services
        studio_char = services.get_characteristic(CHARACTERISTIC_UUID)
        if studio_char is None:
            await self._client.disconnect()
            self._client = None
            raise TransportError(
                f"BLE device {self._address} does not expose the ZMK Studio "
                f"characteristic ({CHARACTERISTIC_UUID}). Either it's not a "
                f"ZMK keyboard or the firmware was built without "
                f"CONFIG_ZMK_STUDIO_TRANSPORT_BLE."
            )
        try:
            await self._client.start_notify(CHARACTERISTIC_UUID, self._on_indication)
        except Exception as exc:
            await self._client.disconnect()
            self._client = None
            raise TransportError(
                f"Failed to subscribe to Studio indications on {self._address}: {exc}"
            ) from exc

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.stop_notify(CHARACTERISTIC_UUID)
            except Exception:
                pass
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    @property
    def name(self) -> str:
        return f"ble:{self._address}" if self._address else f"ble:{self._device}"

    def _on_indication(self, _sender, data: bytes) -> None:
        """Indication handler: feed bytes to reader, enqueue complete frames.

        May be called from a background thread (BlueZ/WinRT backends), so
        we marshal the queue insertions back to the asyncio loop via
        call_soon_threadsafe to avoid corrupting the queue.
        """
        loop = self._loop
        if loop is None:
            return
        for frame_bytes in self._reader.feed(bytes(data)):
            loop.call_soon_threadsafe(self._frame_queue.put_nowait, frame_bytes)

    async def send_request(
        self, request: studio_pb2.Request
    ) -> studio_pb2.Response:
        if self._client is None or not self._client.is_connected:
            await self.open()
        assert self._client is not None

        payload = request.SerializeToString()
        framed = encode_frame(payload)
        request_id = request.request_id
        async with self._write_lock:
            await self._client.write_gatt_char(
                CHARACTERISTIC_UUID, framed, response=True
            )

            deadline = asyncio.get_event_loop().time() + self._request_timeout_s
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TransportError(
                        f"Timed out waiting for BLE response to request {request_id}"
                    )
                try:
                    frame_bytes = await asyncio.wait_for(
                        self._frame_queue.get(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    raise TransportError(
                        f"Timed out waiting for BLE response to request {request_id}"
                    )
                response = studio_pb2.Response()
                try:
                    response.ParseFromString(frame_bytes)
                except Exception as exc:
                    LOG.warning("Failed to parse BLE response frame: %s", exc)
                    continue
                which = response.WhichOneof("type")
                if which == "notification":
                    LOG.debug("Ignoring async BLE notification")
                    continue
                if which == "request_response":
                    if response.request_response.request_id == request_id:
                        return response
                    LOG.debug(
                        "Got BLE response for request %d, waiting for %d",
                        response.request_response.request_id,
                        request_id,
                    )
                    continue

    async def __aenter__(self) -> "StudioBLE":
        await self.open()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()
