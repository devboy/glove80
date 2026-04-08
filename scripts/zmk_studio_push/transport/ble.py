"""BLE GATT transport using bleak."""
from __future__ import annotations

import asyncio
import logging

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


async def scan_for_studio_devices(
    timeout: float = DEFAULT_SCAN_TIMEOUT_S,
) -> list[BLEDevice]:
    """Scan for BLE devices advertising the ZMK Studio service UUID."""
    devices = await BleakScanner.discover(
        timeout=timeout,
        service_uuids=[SERVICE_UUID],
    )
    # Some OSes don't filter reliably in advertisements, so double-check.
    return [d for d in devices if d is not None]


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
        await self._client.start_notify(CHARACTERISTIC_UUID, self._on_indication)

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
