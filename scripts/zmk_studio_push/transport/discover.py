"""Auto-discovery of ZMK Studio devices across USB and BLE transports."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Literal

from ..proto import studio_pb2
from .base import StudioTransport, TransportError
from .ble import StudioBLE, find_bonded_macos_devices, scan_for_studio_devices
from .usb import StudioSerial, candidate_ports

LOG = logging.getLogger(__name__)

Preference = Literal["auto", "usb", "ble"]


class DeviceNotFoundError(TransportError):
    """Raised when no ZMK Studio device can be discovered."""


async def _probe_transport(transport: StudioTransport) -> StudioTransport:
    """Send a core.get_device_info request to confirm the transport speaks
    ZMK Studio RPC. Returns the transport on success, raises on failure.
    """
    req = studio_pb2.Request()
    req.request_id = 0
    req.core.get_device_info = True
    try:
        response = await transport.send_request(req)
    except Exception:
        await transport.close()
        raise

    which = response.request_response.WhichOneof("subsystem")
    if which != "core":
        await transport.close()
        raise TransportError(
            f"{transport.name}: get_device_info response has unexpected subsystem {which!r}"
        )
    return transport


async def discover_usb() -> StudioTransport:
    """Find and return a USB-connected ZMK Studio device."""
    override = os.environ.get("ZMK_STUDIO_PORT")
    if override:
        LOG.info("Using ZMK_STUDIO_PORT override: %s", override)
        transport = StudioSerial(override)
        return await _probe_transport(transport)

    ports = candidate_ports()
    if not ports:
        raise DeviceNotFoundError(
            "No USB CDC devices found. Is the Toucan LH plugged in?"
        )
    last_err: Exception | None = None
    for port in ports:
        LOG.debug("Probing USB port %s", port)
        transport = StudioSerial(port, request_timeout_s=1.0)
        try:
            return await _probe_transport(transport)
        except Exception as exc:
            LOG.debug("  %s did not respond: %s", port, exc)
            last_err = exc
            continue
    raise DeviceNotFoundError(
        f"No ZMK Studio device found on USB. Tried: {', '.join(ports)}. "
        f"Last error: {last_err}"
    )


async def discover_ble() -> StudioTransport:
    """Find and return a BLE-connected ZMK Studio device.

    Tries three strategies in order:
      1. ZMK_STUDIO_BLE_ADDR env override (CoreBluetooth UUID on macOS)
      2. CoreBluetooth retrieveConnectedPeripheralsWithServices: with the
         Studio service UUID. This finds bonded ZMK devices that are
         currently connected to the system but not advertising — the
         common case when you're typing on the keyboard and the Mac is
         already paired with it.
      3. Active BLE scan with a name filter, for devices that ARE
         advertising (newly powered on, unbonded, etc.)
    """
    override = os.environ.get("ZMK_STUDIO_BLE_ADDR")
    if override:
        LOG.info("Using ZMK_STUDIO_BLE_ADDR override: %s", override)
        transport = StudioBLE(override)
        return await _probe_transport(transport)

    # Strategy 2: query CoreBluetooth for already-bonded peripherals.
    # On macOS this is the only reliable path for keyboards that are
    # currently connected for HID and not advertising.
    bonded = await find_bonded_macos_devices()
    if bonded:
        LOG.info("Found %d bonded ZMK device(s) via CoreBluetooth", len(bonded))
        last_err: Exception | None = None
        for device in bonded:
            LOG.info("Probing bonded device %s (%s)", device.address, device.name)
            transport = StudioBLE(device)
            try:
                return await _probe_transport(transport)
            except Exception as exc:
                LOG.debug("  %s did not respond: %s", device.address, exc)
                last_err = exc
                continue
        # If we found bonded devices but none responded, fall through to
        # an active scan as a last resort.
        LOG.info(
            "All bonded ZMK devices failed to respond; falling back to active scan"
        )

    # Strategy 3: active scan with name filter.
    LOG.info("Scanning for advertising BLE ZMK keyboards by name...")
    devices = await scan_for_studio_devices()
    if not devices:
        raise DeviceNotFoundError(
            "No BLE ZMK devices found.\n"
            "  - No bonded ZMK devices on this Mac (or all probes failed).\n"
            "  - No advertising devices matched the name filter "
            "('toucan', 'glove80', 'zmk' by default).\n"
            "Override with ZMK_STUDIO_BLE_NAME=<substring> or "
            "ZMK_STUDIO_BLE_ADDR=<CoreBluetooth-UUID>. Make sure the keyboard "
            "is powered on and within range."
        )
    last_err = None
    seen: list[str] = []
    for device in devices:
        LOG.info("Probing BLE device %s (%s)", device.address, device.name)
        seen.append(f"{device.address} ({device.name})")
        transport = StudioBLE(device)
        try:
            return await _probe_transport(transport)
        except Exception as exc:
            LOG.debug("  %s did not respond: %s", device.address, exc)
            last_err = exc
            continue
    raise DeviceNotFoundError(
        f"Found {len(devices)} BLE candidate(s) but none responded to "
        f"ZMK Studio RPC.\n  Tried: {', '.join(seen)}\n  Last error: {last_err}"
    )


async def discover(preference: Preference = "auto") -> StudioTransport:
    """Discover a ZMK Studio device using the given transport preference.

    'auto' runs USB and BLE discovery concurrently and returns whichever
    completes successfully first. 'usb' or 'ble' force a single transport.
    Env vars ZMK_STUDIO_PORT and ZMK_STUDIO_BLE_ADDR skip discovery for
    their respective transport.
    """
    if preference == "usb":
        return await discover_usb()
    if preference == "ble":
        return await discover_ble()
    if preference != "auto":
        raise ValueError(f"Unknown transport preference: {preference!r}")

    # Auto: race both, return first to succeed. Carefully cleans up the
    # loser even if it also succeeded (close the leaked transport) or is
    # still pending (cancel it). Keep references to the original task
    # objects so the finally block can guarantee cleanup.
    usb_task = asyncio.create_task(discover_usb(), name="discover_usb")
    ble_task = asyncio.create_task(discover_ble(), name="discover_ble")
    all_tasks = (usb_task, ble_task)

    winner: StudioTransport | None = None
    try:
        pending = set(all_tasks)
        while pending and winner is None:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                try:
                    result = task.result()
                except Exception as exc:
                    LOG.debug("%s failed: %s", task.get_name(), exc)
                    continue
                if winner is None:
                    winner = result
                else:
                    # Two tasks completed in the same wait cycle and both
                    # succeeded. Close the loser to avoid leaking its
                    # connection.
                    LOG.debug("Closing duplicate %s transport", task.get_name())
                    try:
                        await result.close()
                    except Exception as close_exc:
                        LOG.debug("Error closing duplicate transport: %s", close_exc)

        if winner is not None:
            return winner

        # Both tasks failed — assemble a useful error message.
        usb_err: Exception | None = None
        ble_err: Exception | None = None
        try:
            usb_task.result()
        except Exception as exc:
            usb_err = exc
        try:
            ble_task.result()
        except Exception as exc:
            ble_err = exc
        raise DeviceNotFoundError(
            f"No ZMK Studio device found via USB or BLE.\n"
            f"  USB: {usb_err}\n"
            f"  BLE: {ble_err}"
        )
    finally:
        # Cancel any still-running tasks and close any transport that
        # was returned by them. Iterate the original task tuple so we
        # never miss a leaked transport even if `pending` is empty.
        for task in all_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            else:
                # Task is done. If it succeeded and isn't the winner,
                # close the leaked transport.
                if task.cancelled() or task.exception() is not None:
                    continue
                try:
                    leaked = task.result()
                except Exception:
                    continue
                if leaked is winner:
                    continue
                LOG.debug("Closing leaked %s transport", task.get_name())
                try:
                    await leaked.close()
                except Exception as close_exc:
                    LOG.debug("Error closing leaked transport: %s", close_exc)
