"""USB CDC ACM transport using pyserial + asyncio.to_thread."""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import serial
import serial.tools.list_ports

from ..framing import FrameReader, encode_frame
from ..proto import studio_pb2
from .base import StudioTransport, TransportError

LOG = logging.getLogger(__name__)

# Short per-read timeout so the read loop polls frequently enough to
# notice cancellation/timeouts. The total request budget is enforced
# separately by send_request's deadline.
_PER_READ_TIMEOUT_S = 0.05
DEFAULT_REQUEST_TIMEOUT_S = 4.0


def candidate_ports() -> list[str]:
    """Return serial port device paths that look like a USB CDC device.

    We filter to /dev/tty.usbmodem* / /dev/tty.usbserial* (macOS) and
    /dev/ttyACM* / /dev/ttyUSB* (Linux). The caller will probe each to
    find one that speaks ZMK Studio RPC. This is more reliable than
    VID/PID filtering because ZMK's VID/PID can be overridden per-board.

    On macOS we deliberately avoid /dev/cu.* (the callout side) because
    the /dev/tty.* side is what pyserial opens by default, and cu.*
    also shows Bluetooth and debug ports we don't want.
    """
    ports = []
    for info in serial.tools.list_ports.comports():
        dev = info.device
        if dev.startswith("/dev/tty.usbmodem") or dev.startswith("/dev/tty.usbserial"):
            ports.append(dev)
        elif dev.startswith("/dev/ttyACM") or dev.startswith("/dev/ttyUSB"):
            ports.append(dev)
        # Otherwise skip (cu.*, bluetooth, debug consoles, etc.)
    return ports


class StudioSerial(StudioTransport):
    """Async wrapper around a pyserial connection to a ZMK Studio device.

    Baud rate is not specified because Zephyr USB CDC ACM ignores it —
    data flows over USB bulk endpoints at USB speeds, not a real UART.

    The read loop uses a short per-read timeout (50ms) and a separate
    request-level deadline. This makes Ctrl-C and asyncio cancellation
    propagate within ~50ms instead of being stuck inside an
    uninterruptible blocking C call to serial.read.
    """

    def __init__(
        self,
        port: str,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ):
        self._port_path = port
        self._request_timeout_s = request_timeout_s
        self._ser: serial.Serial | None = None
        self._reader = FrameReader()
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        if self._ser is not None:
            return
        try:
            self._ser = await asyncio.to_thread(
                serial.Serial,
                self._port_path,
                timeout=_PER_READ_TIMEOUT_S,
            )
        except (serial.SerialException, OSError) as exc:
            raise TransportError(
                f"Failed to open serial port {self._port_path}: {exc}"
            ) from exc

    async def close(self) -> None:
        if self._ser is not None:
            ser, self._ser = self._ser, None
            try:
                await asyncio.to_thread(ser.close)
            except Exception as exc:
                LOG.debug("Error closing serial port: %s", exc)

    @property
    def name(self) -> str:
        return f"usb:{self._port_path}"

    async def send_request(
        self, request: studio_pb2.Request
    ) -> studio_pb2.Response:
        if self._ser is None:
            await self.open()
        assert self._ser is not None

        payload = request.SerializeToString()
        framed = encode_frame(payload)
        request_id = request.request_id
        async with self._write_lock:
            await asyncio.to_thread(self._ser.write, framed)
            try:
                await asyncio.to_thread(self._ser.flush)
            except Exception:
                pass

            # Read loop: short per-read timeouts so cancellation propagates
            # quickly. Total budget is self._request_timeout_s.
            loop = asyncio.get_event_loop()
            deadline = loop.time() + self._request_timeout_s
            while loop.time() < deadline:
                chunk = await asyncio.to_thread(self._ser.read, 256)
                if not chunk:
                    # Yield to the event loop so cancellation can fire
                    await asyncio.sleep(0)
                    continue
                for frame_bytes in self._reader.feed(chunk):
                    response = studio_pb2.Response()
                    try:
                        response.ParseFromString(frame_bytes)
                    except Exception as exc:
                        LOG.warning("Failed to parse response frame: %s", exc)
                        continue
                    which = response.WhichOneof("type")
                    if which == "notification":
                        LOG.debug("Ignoring async notification")
                        continue
                    if which == "request_response":
                        if response.request_response.request_id == request_id:
                            return response
                        LOG.debug(
                            "Got response for request %d, waiting for %d",
                            response.request_response.request_id,
                            request_id,
                        )
                        continue

        raise TransportError(
            f"Timed out waiting for response to request {request_id} on {self.name}"
        )

    async def __aenter__(self) -> "StudioSerial":
        await self.open()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()
