"""ZMK Studio RPC message framing: SoF/Esc/EoF codec.

Per https://zmk.dev/docs/development/studio-rpc-protocol, both the USB
serial transport and the BLE GATT transport carry protobuf messages inside
a simple framing with three special bytes:

  - Start of Frame (SoF): 0xAB
  - Escape Byte   (Esc):  0xAC
  - End of Frame  (EoF):  0xAD

Any payload byte equal to one of these three must be prefixed with the
escape byte during encoding, and the escape is stripped on decode.

The framing is stateful over streaming transports: bytes arrive in
arbitrary chunks and we must reassemble complete frames. FrameReader
provides that streaming interface; encode_frame/decode_frame are the
one-shot helpers for simple call sites and for testing.
"""

from __future__ import annotations

SOF = 0xAB
ESC = 0xAC
EOF = 0xAD

_SPECIAL = frozenset({SOF, ESC, EOF})


def encode_frame(payload: bytes) -> bytes:
    """Wrap a payload in SoF/EoF with escape bytes for 0xAB/0xAC/0xAD."""
    out = bytearray([SOF])
    for byte in payload:
        if byte in _SPECIAL:
            out.append(ESC)
        out.append(byte)
    out.append(EOF)
    return bytes(out)


def decode_frame(frame: bytes) -> bytes:
    """Decode a single complete frame to its raw payload.

    Raises ValueError if the input is not a well-formed frame.
    """
    if not frame or frame[0] != SOF:
        raise ValueError("Frame does not start with SoF byte")
    if frame[-1] != EOF:
        raise ValueError("Frame does not end with EoF byte")
    out = bytearray()
    i = 1
    end = len(frame) - 1
    while i < end:
        byte = frame[i]
        if byte == ESC:
            if i + 1 >= end:
                raise ValueError("Dangling escape byte before EoF")
            out.append(frame[i + 1])
            i += 2
        else:
            out.append(byte)
            i += 1
    return bytes(out)


class FrameReader:
    """Streaming frame reassembler.

    Feed arbitrary chunks of bytes via feed(); the reader buffers incomplete
    frames and returns complete payloads as they're fully received. Skips
    any garbage bytes that appear before a SoF (useful for recovering from
    transport noise). Handles escape bytes that fall across chunk boundaries.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        # Parser state machine:
        #   0 = waiting for SoF (skipping garbage)
        #   1 = reading payload
        #   2 = just saw ESC, next byte is literal
        self._state = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        """Consume a chunk of bytes, return any complete payloads."""
        frames: list[bytes] = []
        for byte in chunk:
            if self._state == 0:
                # Skipping garbage until SoF
                if byte == SOF:
                    self._buf.clear()
                    self._state = 1
                # else: silently drop
            elif self._state == 1:
                if byte == ESC:
                    self._state = 2
                elif byte == EOF:
                    frames.append(bytes(self._buf))
                    self._buf.clear()
                    self._state = 0
                elif byte == SOF:
                    # Unexpected SoF mid-frame: treat as resync point.
                    self._buf.clear()
                    # Stay in state 1 (we already saw the new SoF)
                else:
                    self._buf.append(byte)
            elif self._state == 2:
                # Literal escaped byte
                self._buf.append(byte)
                self._state = 1
        return frames

    def reset(self) -> None:
        """Clear all buffered state."""
        self._buf.clear()
        self._state = 0
