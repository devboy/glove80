"""Abstract transport interface shared by USB and BLE implementations."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..proto import studio_pb2


class TransportError(RuntimeError):
    """Raised for transport-level failures (connection, read/write)."""


class StudioTransport(ABC):
    """Bidirectional request/response channel to a ZMK Studio device.

    Implementations handle the specifics of USB CDC ACM or BLE GATT, but
    share the same request/response interface and the same framing codec.
    """

    @abstractmethod
    async def send_request(
        self, request: studio_pb2.Request
    ) -> studio_pb2.Response:
        """Send a request and block until the matching response arrives.

        Any async notifications received while waiting for the response
        are silently dropped (they're UI state we don't care about).
        """

    @abstractmethod
    async def close(self) -> None:
        """Close the connection and release any resources."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable identifier (e.g. 'usb:/dev/tty.usbmodem1101')."""
