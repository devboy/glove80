"""Tests for the ZMK Studio RPC framing codec."""
from __future__ import annotations

import pytest

from zmk_studio_push.framing import SOF, ESC, EOF, FrameReader, decode_frame, encode_frame


class TestEncodeFrame:
    def test_empty_payload(self):
        assert encode_frame(b"") == bytes([SOF, EOF])

    def test_single_byte_no_escape(self):
        assert encode_frame(b"\x01") == bytes([SOF, 0x01, EOF])

    def test_plain_multi_byte(self):
        assert encode_frame(b"hello") == bytes([SOF]) + b"hello" + bytes([EOF])

    def test_escapes_sof_byte(self):
        # A payload byte matching SOF (0xAB) must be preceded by ESC (0xAC)
        assert encode_frame(bytes([SOF])) == bytes([SOF, ESC, SOF, EOF])

    def test_escapes_esc_byte(self):
        assert encode_frame(bytes([ESC])) == bytes([SOF, ESC, ESC, EOF])

    def test_escapes_eof_byte(self):
        assert encode_frame(bytes([EOF])) == bytes([SOF, ESC, EOF, EOF])

    def test_escapes_all_three_special_bytes(self):
        payload = bytes([SOF, ESC, EOF])
        expected = bytes([SOF, ESC, SOF, ESC, ESC, ESC, EOF, EOF])
        assert encode_frame(payload) == expected

    def test_preserves_surrounding_bytes(self):
        payload = bytes([0x01, SOF, 0x02])
        assert encode_frame(payload) == bytes([SOF, 0x01, ESC, SOF, 0x02, EOF])


class TestDecodeFrame:
    def test_empty_frame(self):
        assert decode_frame(bytes([SOF, EOF])) == b""

    def test_plain_payload(self):
        assert decode_frame(bytes([SOF]) + b"hello" + bytes([EOF])) == b"hello"

    def test_escaped_sof(self):
        assert decode_frame(bytes([SOF, ESC, SOF, EOF])) == bytes([SOF])

    def test_escaped_esc(self):
        assert decode_frame(bytes([SOF, ESC, ESC, EOF])) == bytes([ESC])

    def test_escaped_eof(self):
        assert decode_frame(bytes([SOF, ESC, EOF, EOF])) == bytes([EOF])

    def test_rejects_missing_sof(self):
        with pytest.raises(ValueError, match="SoF"):
            decode_frame(b"hello" + bytes([EOF]))

    def test_rejects_missing_eof(self):
        with pytest.raises(ValueError, match="EoF"):
            decode_frame(bytes([SOF]) + b"hello")

    def test_rejects_dangling_escape(self):
        with pytest.raises(ValueError, match="escape"):
            decode_frame(bytes([SOF, ESC, EOF]))  # ESC followed directly by EOF


class TestRoundTrip:
    @pytest.mark.parametrize(
        "payload",
        [
            b"",
            b"\x00",
            b"\xff",
            bytes(range(256)),
            bytes([SOF]) * 10,
            bytes([ESC, EOF, SOF] * 20),
            b"Hello, World!" * 100,
        ],
    )
    def test_encode_decode_identity(self, payload: bytes):
        assert decode_frame(encode_frame(payload)) == payload


class TestFrameReader:
    def test_single_complete_frame(self):
        reader = FrameReader()
        frames = reader.feed(encode_frame(b"hello"))
        assert frames == [b"hello"]

    def test_fragments_reassemble(self):
        reader = FrameReader()
        frame = encode_frame(b"the quick brown fox")
        # Feed one byte at a time
        all_frames: list[bytes] = []
        for i in range(len(frame)):
            all_frames.extend(reader.feed(frame[i : i + 1]))
        assert all_frames == [b"the quick brown fox"]

    def test_multiple_frames_in_one_chunk(self):
        reader = FrameReader()
        combined = encode_frame(b"first") + encode_frame(b"second") + encode_frame(b"third")
        frames = reader.feed(combined)
        assert frames == [b"first", b"second", b"third"]

    def test_frames_split_across_chunks(self):
        reader = FrameReader()
        combined = encode_frame(b"one") + encode_frame(b"two")
        # Split in the middle of the second frame
        split = len(combined) // 2 + 1
        frames1 = reader.feed(combined[:split])
        frames2 = reader.feed(combined[split:])
        assert frames1 + frames2 == [b"one", b"two"]

    def test_escape_spanning_chunk_boundary(self):
        reader = FrameReader()
        # Encoded payload where ESC falls right at the boundary
        payload = bytes([0x01, SOF, 0x02])  # encodes to SOF 01 ESC SOF 02 EOF
        encoded = encode_frame(payload)
        # Split at position 3 so ESC ends the first chunk
        frames1 = reader.feed(encoded[:3])
        frames2 = reader.feed(encoded[3:])
        assert frames1 + frames2 == [payload]

    def test_ignores_garbage_before_sof(self):
        reader = FrameReader()
        garbage = b"\x00\x01\x02junk"
        frames = reader.feed(garbage + encode_frame(b"real"))
        assert frames == [b"real"]

    def test_empty_feed_returns_no_frames(self):
        reader = FrameReader()
        assert reader.feed(b"") == []
        assert reader.feed(bytes([SOF])) == []  # Incomplete frame
