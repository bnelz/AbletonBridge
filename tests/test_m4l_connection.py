"""Tests for M4LConnection class (MCP_Server/connections/m4l.py).

Covers:
  - _build_osc_message: OSC binary format (string padding, type tags, arg encoding)
  - _parse_m4l_response: JSON extraction from UDP packet bytes
  - _reassemble_chunked_response: multi-chunk reassembly (happy, out-of-order,
        timeout/missing, duplicate)
  - send_command_with_retry: retry-on-busy logic
  - _check_bridge_version: version mismatch warning
"""

import base64
import json
import socket
import struct
import time
from unittest.mock import MagicMock, patch, call

import pytest

from MCP_Server.connections.m4l import M4LConnection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _osc_pad(s: str) -> bytes:
    """Replicate the OSC null-terminated, 4-byte-aligned string encoding."""
    b = s.encode("utf-8") + b"\x00"
    b += b"\x00" * ((4 - len(b) % 4) % 4)
    return b


def _make_b64_osc_packet(payload_dict: dict) -> bytes:
    """Build a fake UDP packet the way the M4L bridge sends it:
    url-safe base64 of JSON, used as the OSC address, followed by a type tag.
    """
    raw_json = json.dumps(payload_dict, separators=(",", ":"))
    b64 = base64.urlsafe_b64encode(raw_json.encode("utf-8")).decode("ascii").rstrip("=")
    # OSC layout: address\0...pad  ,\0\0\0
    return _osc_pad(b64) + _osc_pad(",")


def _make_chunk_packet(chunk_index: int, total: int, json_fragment: str) -> bytes:
    """Build a fake chunked-response UDP packet."""
    piece_b64 = base64.urlsafe_b64encode(json_fragment.encode("utf-8")).decode("ascii").rstrip("=")
    envelope = {"_c": chunk_index, "_t": total, "_d": piece_b64}
    return _make_b64_osc_packet(envelope)


# ===================================================================
# 1. _build_osc_message
# ===================================================================

class TestBuildOscMessage:
    """Verify _build_osc_message produces valid OSC binary."""

    def test_address_only(self):
        """Message with no arguments has the address + a bare comma type tag."""
        msg = M4LConnection._build_osc_message("/ping")
        # Address: /ping\0 padded to 8 bytes
        assert msg[:5] == b"/ping"
        assert msg[5] == 0  # null terminator
        # Address must be 4-byte aligned
        assert len(_osc_pad("/ping")) == 8
        # Type tag section starts right after address
        type_tag_start = len(_osc_pad("/ping"))
        type_tag_bytes = msg[type_tag_start:]
        # Should be ",\0" padded to 4 bytes
        assert type_tag_bytes == _osc_pad(",")

    def test_string_arg_padding(self):
        """String arguments are null-terminated and padded to 4-byte boundary."""
        msg = M4LConnection._build_osc_message("/test", [("s", "ab")])
        # Type tag should be ",s"
        addr_end = len(_osc_pad("/test"))
        type_tag = _osc_pad(",s")
        assert msg[addr_end:addr_end + len(type_tag)] == type_tag
        # String arg: "ab\0" = 3 bytes, padded to 4
        arg_start = addr_end + len(type_tag)
        arg_bytes = msg[arg_start:]
        assert arg_bytes == _osc_pad("ab")
        assert len(arg_bytes) == 4  # "ab\0\0" -> 4 bytes

    def test_string_exact_boundary(self):
        """A string whose length+1 (for null) is already a multiple of 4."""
        # "abc" -> 3 chars + 1 null = 4, already aligned
        msg = M4LConnection._build_osc_message("/x", [("s", "abc")])
        addr_end = len(_osc_pad("/x"))
        type_end = addr_end + len(_osc_pad(",s"))
        arg_bytes = msg[type_end:]
        # "abc\0" = 4 bytes exactly, but OSC spec says we still need the
        # padding formula: (4 - 4%4)%4 = 0 extra.  So 4 bytes total.
        assert arg_bytes[:4] == b"abc\x00"
        # However the implementation adds (4 - len%4)%4 extra nulls, and
        # len("abc\0") = 4, so 0 extra.  Total = 4.
        assert len(arg_bytes) == 4

    def test_int_arg(self):
        """Integer arguments are big-endian 32-bit signed ints."""
        msg = M4LConnection._build_osc_message("/num", [("i", 42)])
        addr_end = len(_osc_pad("/num"))
        type_end = addr_end + len(_osc_pad(",i"))
        int_bytes = msg[type_end:]
        assert int_bytes == struct.pack(">i", 42)

    def test_float_arg(self):
        """Float arguments are big-endian 32-bit IEEE 754 floats."""
        msg = M4LConnection._build_osc_message("/flt", [("f", 3.14)])
        addr_end = len(_osc_pad("/flt"))
        type_end = addr_end + len(_osc_pad(",f"))
        float_bytes = msg[type_end:]
        assert float_bytes == struct.pack(">f", 3.14)

    def test_mixed_args_type_tag(self):
        """Type tag string reflects the order of argument types."""
        msg = M4LConnection._build_osc_message("/mix", [
            ("i", 1),
            ("f", 2.0),
            ("s", "hello"),
            ("i", 3),
        ])
        addr_end = len(_osc_pad("/mix"))
        expected_type_tag = _osc_pad(",ifs i")  # wrong -- compute properly
        # Actually type tag is ",ifsi"
        expected_type_tag = _osc_pad(",ifsi")
        type_tag_section = msg[addr_end:addr_end + len(expected_type_tag)]
        assert type_tag_section == expected_type_tag

    def test_mixed_args_values(self):
        """Values are serialized in order after the type tag."""
        msg = M4LConnection._build_osc_message("/mix", [
            ("i", 10),
            ("s", "ok"),
            ("f", 1.5),
        ])
        addr_end = len(_osc_pad("/mix"))
        type_tag = _osc_pad(",isf")
        val_start = addr_end + len(type_tag)
        remaining = msg[val_start:]

        # First value: int 10
        assert remaining[:4] == struct.pack(">i", 10)
        remaining = remaining[4:]

        # Second value: string "ok"
        ok_padded = _osc_pad("ok")
        assert remaining[:len(ok_padded)] == ok_padded
        remaining = remaining[len(ok_padded):]

        # Third value: float 1.5
        assert remaining == struct.pack(">f", 1.5)

    def test_no_args_defaults_to_empty_list(self):
        """Passing osc_args=None produces message with bare comma type tag."""
        msg1 = M4LConnection._build_osc_message("/a", None)
        msg2 = M4LConnection._build_osc_message("/a", [])
        assert msg1 == msg2

    def test_negative_int(self):
        """Negative integers are encoded correctly as signed 32-bit big-endian."""
        msg = M4LConnection._build_osc_message("/neg", [("i", -1)])
        addr_end = len(_osc_pad("/neg"))
        type_end = addr_end + len(_osc_pad(",i"))
        assert msg[type_end:] == struct.pack(">i", -1)


# ===================================================================
# 2. _parse_m4l_response
# ===================================================================

class TestParseM4LResponse:
    """Test JSON parsing from UDP packet bytes."""

    def test_urlsafe_base64_response(self):
        """Standard path: url-safe base64 JSON as the OSC address."""
        payload = {"status": "success", "result": {"version": "3.2.0"}}
        packet = _make_b64_osc_packet(payload)
        parsed = M4LConnection._parse_m4l_response(packet)
        assert parsed == payload

    def test_standard_base64_fallback(self):
        """Standard (non-url-safe) base64 should also be decoded."""
        payload = {"status": "success", "value": 42}
        raw_json = json.dumps(payload)
        b64 = base64.b64encode(raw_json.encode("utf-8")).decode("ascii")
        # Wrap as OSC packet
        packet = _osc_pad(b64) + _osc_pad(",")
        parsed = M4LConnection._parse_m4l_response(packet)
        assert parsed == payload

    def test_raw_json_fallback(self):
        """If the address is raw JSON (not base64), the parser handles it."""
        payload = {"status": "error", "message": "not found"}
        raw_json = json.dumps(payload)
        packet = _osc_pad(raw_json) + _osc_pad(",")
        parsed = M4LConnection._parse_m4l_response(packet)
        assert parsed == payload

    def test_invalid_data_raises(self):
        """Completely unparseable data raises JSONDecodeError."""
        packet = b"this_is_not_valid\x00\x00\x00\x00,\x00\x00\x00"
        with pytest.raises(json.JSONDecodeError):
            M4LConnection._parse_m4l_response(packet)

    def test_chunked_envelope_detected(self):
        """A chunked envelope packet is parsed correctly, preserving _c/_t/_d keys."""
        piece_b64 = base64.urlsafe_b64encode(b'{"part":').decode("ascii").rstrip("=")
        envelope = {"_c": 0, "_t": 3, "_d": piece_b64}
        packet = _make_b64_osc_packet(envelope)
        parsed = M4LConnection._parse_m4l_response(packet)
        assert parsed["_c"] == 0
        assert parsed["_t"] == 3
        assert parsed["_d"] == piece_b64

    def test_urlsafe_b64_with_special_chars(self):
        """Payload that produces - and _ in url-safe base64 decodes correctly."""
        # Create payload whose base64 would contain + and / in standard encoding
        payload = {"data": "a+b/c=d", "extra": "?" * 50}
        raw_json = json.dumps(payload, separators=(",", ":"))
        b64 = base64.urlsafe_b64encode(raw_json.encode("utf-8")).decode("ascii").rstrip("=")
        packet = _osc_pad(b64) + _osc_pad(",")
        parsed = M4LConnection._parse_m4l_response(packet)
        assert parsed == payload


# ===================================================================
# 3. _reassemble_chunked_response
# ===================================================================

class TestReassembleChunkedResponse:
    """Test multi-chunk reassembly."""

    def _make_conn_with_mock_recv(self):
        """Create an M4LConnection with a mocked recv_sock."""
        conn = M4LConnection()
        conn.recv_sock = MagicMock()
        return conn

    def test_happy_path_in_order(self):
        """All chunks arrive in order and reassemble correctly."""
        payload = {"status": "success", "result": {"params": list(range(50))}}
        full_json = json.dumps(payload, separators=(",", ":"))
        # Split into 3 chunks
        chunk_size = len(full_json) // 3 + 1
        parts = [full_json[i:i + chunk_size] for i in range(0, len(full_json), chunk_size)]
        total = len(parts)

        conn = self._make_conn_with_mock_recv()

        # Build first chunk (passed directly to the method)
        first_b64 = base64.urlsafe_b64encode(parts[0].encode("utf-8")).decode("ascii").rstrip("=")
        first_chunk = {"_c": 0, "_t": total, "_d": first_b64}

        # Remaining chunks arrive via recv_sock
        remaining_packets = []
        for i in range(1, total):
            remaining_packets.append((_make_chunk_packet(i, total, parts[i]), ("127.0.0.1", 9878)))

        conn.recv_sock.recvfrom = MagicMock(side_effect=remaining_packets)

        result = conn._reassemble_chunked_response(first_chunk)
        assert result == payload

    def test_out_of_order_chunks(self):
        """Chunks arriving out of order are reassembled correctly by dict index."""
        payload = {"data": "ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 10}
        full_json = json.dumps(payload, separators=(",", ":"))
        # Split into 4 chunks
        chunk_size = len(full_json) // 4 + 1
        parts = [full_json[i:i + chunk_size] for i in range(0, len(full_json), chunk_size)]
        total = len(parts)

        conn = self._make_conn_with_mock_recv()

        # First chunk is index 2 (out of order)
        first_b64 = base64.urlsafe_b64encode(parts[2].encode("utf-8")).decode("ascii").rstrip("=")
        first_chunk = {"_c": 2, "_t": total, "_d": first_b64}

        # Send remaining in scrambled order: 3, 0, 1
        scrambled_order = [3, 0, 1]
        remaining_packets = []
        for i in scrambled_order:
            remaining_packets.append((_make_chunk_packet(i, total, parts[i]), ("127.0.0.1", 9878)))

        conn.recv_sock.recvfrom = MagicMock(side_effect=remaining_packets)

        result = conn._reassemble_chunked_response(first_chunk)
        assert result == payload

    def test_timeout_missing_chunk(self):
        """Timeout with missing chunks raises an exception with chunk count info."""
        conn = self._make_conn_with_mock_recv()

        first_b64 = base64.urlsafe_b64encode(b'{"a":').decode("ascii").rstrip("=")
        first_chunk = {"_c": 0, "_t": 3, "_d": first_b64}

        # Only one more chunk arrives, then timeout (missing chunk index 2)
        second_b64 = base64.urlsafe_b64encode(b'"val"').decode("ascii").rstrip("=")
        second_envelope = {"_c": 1, "_t": 3, "_d": second_b64}
        second_packet = _make_b64_osc_packet(second_envelope)

        conn.recv_sock.recvfrom = MagicMock(
            side_effect=[
                (second_packet, ("127.0.0.1", 9878)),
                socket.timeout("timed out"),
            ]
        )

        with pytest.raises(Exception, match=r"2/3 chunks"):
            conn._reassemble_chunked_response(first_chunk)

    def test_duplicate_chunk_ignored(self):
        """A duplicate chunk index is logged and ignored, reassembly still works."""
        payload = {"x": 123}
        full_json = json.dumps(payload, separators=(",", ":"))
        # Two chunks
        mid = len(full_json) // 2
        part0 = full_json[:mid]
        part1 = full_json[mid:]
        total = 2

        conn = self._make_conn_with_mock_recv()

        first_b64 = base64.urlsafe_b64encode(part0.encode("utf-8")).decode("ascii").rstrip("=")
        first_chunk = {"_c": 0, "_t": total, "_d": first_b64}

        # Chunk 1 arrives twice (duplicate), then the real chunk 1
        remaining_packets = [
            # Duplicate of chunk 0 -- will overwrite but same data
            (_make_chunk_packet(0, total, part0), ("127.0.0.1", 9878)),
            # Then chunk 1
            (_make_chunk_packet(1, total, part1), ("127.0.0.1", 9878)),
        ]

        conn.recv_sock.recvfrom = MagicMock(side_effect=remaining_packets)

        result = conn._reassemble_chunked_response(first_chunk)
        assert result == payload

    def test_non_chunk_packets_ignored(self):
        """Non-chunk packets received during reassembly are skipped."""
        payload = {"hello": "world"}
        full_json = json.dumps(payload, separators=(",", ":"))
        parts = [full_json[:8], full_json[8:]]
        total = 2

        conn = self._make_conn_with_mock_recv()

        first_b64 = base64.urlsafe_b64encode(parts[0].encode("utf-8")).decode("ascii").rstrip("=")
        first_chunk = {"_c": 0, "_t": total, "_d": first_b64}

        # A non-chunk packet arrives first, then the real chunk 1
        non_chunk_payload = {"status": "success", "id": "stale"}
        remaining_packets = [
            (_make_b64_osc_packet(non_chunk_payload), ("127.0.0.1", 9878)),
            (_make_chunk_packet(1, total, parts[1]), ("127.0.0.1", 9878)),
        ]

        conn.recv_sock.recvfrom = MagicMock(side_effect=remaining_packets)

        result = conn._reassemble_chunked_response(first_chunk)
        assert result == payload


# ===================================================================
# 4. send_command_with_retry
# ===================================================================

class TestSendCommandWithRetry:
    """Test retry-on-busy logic in send_command_with_retry."""

    def _make_conn(self):
        """Create an M4LConnection with send_command mocked."""
        conn = M4LConnection()
        conn._connected = True
        conn.send_sock = MagicMock()
        conn.recv_sock = MagicMock()
        return conn

    @patch("MCP_Server.connections.m4l.time.sleep")
    def test_returns_immediately_on_success(self, mock_sleep):
        """If the first call succeeds, return immediately without retry."""
        conn = self._make_conn()
        success_response = {"status": "success", "result": {"params": []}}
        conn.send_command = MagicMock(return_value=success_response)

        result = conn.send_command_with_retry("get_hidden_params", {"track_index": 0, "device_index": 0})

        assert result == success_response
        conn.send_command.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("MCP_Server.connections.m4l.time.sleep")
    def test_retries_on_busy_then_succeeds(self, mock_sleep):
        """A 'busy' error triggers a retry; success on the 2nd try is returned."""
        conn = self._make_conn()
        busy_response = {"status": "error", "message": "Device busy, try again later"}
        success_response = {"status": "success", "result": {"value": 42}}
        conn.send_command = MagicMock(side_effect=[busy_response, success_response])

        result = conn.send_command_with_retry("ping", max_attempts=3)

        assert result == success_response
        assert conn.send_command.call_count == 2
        # Should have slept 0.5 * (0+1) = 0.5 seconds before retry
        mock_sleep.assert_called_once_with(0.5)

    @patch("MCP_Server.connections.m4l.time.sleep")
    def test_returns_last_error_after_all_retries_exhausted(self, mock_sleep):
        """When all retries are exhausted on busy, return the last error result."""
        conn = self._make_conn()
        busy1 = {"status": "error", "message": "Bridge busy"}
        busy2 = {"status": "error", "message": "Still busy"}
        busy3 = {"status": "error", "message": "Busy again"}
        conn.send_command = MagicMock(side_effect=[busy1, busy2, busy3])

        result = conn.send_command_with_retry("discover_params", max_attempts=3)

        # Returns the last result from the loop (busy3 -- but the code returns last_result
        # which is updated each iteration; the final busy3 triggers continue,
        # last_result becomes busy3, then the loop ends)
        assert result["status"] == "error"
        assert "busy" in result["message"].lower()
        assert conn.send_command.call_count == 3
        # Delays: 0.5*(0+1), 0.5*(1+1), 0.5*(2+1) = 0.5, 1.0, 1.5
        assert mock_sleep.call_count == 3
        mock_sleep.assert_any_call(0.5)
        mock_sleep.assert_any_call(1.0)
        mock_sleep.assert_any_call(1.5)

    @patch("MCP_Server.connections.m4l.time.sleep")
    def test_non_busy_error_returns_immediately(self, mock_sleep):
        """A non-busy error is returned immediately without retries."""
        conn = self._make_conn()
        error_response = {"status": "error", "message": "Parameter index out of range"}
        conn.send_command = MagicMock(return_value=error_response)

        result = conn.send_command_with_retry("set_hidden_param", max_attempts=3)

        assert result == error_response
        conn.send_command.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("MCP_Server.connections.m4l.time.sleep")
    def test_busy_case_insensitive(self, mock_sleep):
        """The busy-detection is case-insensitive (uses .lower())."""
        conn = self._make_conn()
        busy_response = {"status": "error", "message": "BUSY: processing another request"}
        success_response = {"status": "success", "result": {}}
        conn.send_command = MagicMock(side_effect=[busy_response, success_response])

        result = conn.send_command_with_retry("ping", max_attempts=3)

        assert result == success_response
        assert conn.send_command.call_count == 2

    @patch("MCP_Server.connections.m4l.time.sleep")
    def test_default_max_attempts_is_three(self, mock_sleep):
        """Default max_attempts=3 when not specified."""
        conn = self._make_conn()
        busy = {"status": "error", "message": "busy"}
        conn.send_command = MagicMock(return_value=busy)

        conn.send_command_with_retry("ping")

        assert conn.send_command.call_count == 3


# ===================================================================
# 5. _check_bridge_version
# ===================================================================

class TestCheckBridgeVersion:
    """Test version mismatch detection and warning."""

    @patch("MCP_Server.connections.m4l.state")
    @patch("MCP_Server.connections.m4l.logger")
    def test_matching_version_no_warning(self, mock_logger, mock_state):
        """When major.minor match, an info log is emitted (not a warning)."""
        with patch("MCP_Server.connections.m4l.M4LConnection._check_bridge_version.__wrapped__", None, create=True):
            pass  # just ensuring patch context works

        ping_result = {"status": "success", "result": {"version": "3.2.0"}}
        with patch("MCP_Server.__version__", "3.2.0"):
            M4LConnection._check_bridge_version(ping_result)

        mock_state.__setattr__  # accessed to set m4l_bridge_version
        mock_logger.info.assert_called()
        mock_logger.warning.assert_not_called()

    @patch("MCP_Server.connections.m4l.state")
    @patch("MCP_Server.connections.m4l.logger")
    def test_mismatched_version_warns(self, mock_logger, mock_state):
        """When major.minor differ, a warning is logged."""
        ping_result = {"status": "success", "result": {"version": "2.9.0"}}
        with patch("MCP_Server.__version__", "3.2.0"):
            M4LConnection._check_bridge_version(ping_result)

        mock_logger.warning.assert_called()
        warning_msg = mock_logger.warning.call_args[0][0]
        assert "mismatch" in warning_msg.lower() or "Version mismatch" in warning_msg

    @patch("MCP_Server.connections.m4l.state")
    @patch("MCP_Server.connections.m4l.logger")
    def test_matching_major_minor_different_patch(self, mock_logger, mock_state):
        """Patch version differences are OK -- no warning."""
        ping_result = {"status": "success", "result": {"version": "3.2.5"}}
        with patch("MCP_Server.__version__", "3.2.0"):
            M4LConnection._check_bridge_version(ping_result)

        mock_logger.info.assert_called()
        mock_logger.warning.assert_not_called()

    @patch("MCP_Server.connections.m4l.state")
    @patch("MCP_Server.connections.m4l.logger")
    def test_no_version_in_response(self, mock_logger, mock_state):
        """Older bridge with no version field logs info, no warning."""
        ping_result = {"status": "success", "result": {}}
        with patch("MCP_Server.__version__", "3.2.0"):
            M4LConnection._check_bridge_version(ping_result)

        mock_logger.info.assert_called()
        mock_logger.warning.assert_not_called()

    @patch("MCP_Server.connections.m4l.state")
    @patch("MCP_Server.connections.m4l.logger")
    def test_empty_result_field(self, mock_logger, mock_state):
        """When result is None or missing, treated as no version."""
        ping_result = {"status": "success", "result": None}
        with patch("MCP_Server.__version__", "3.2.0"):
            M4LConnection._check_bridge_version(ping_result)

        mock_logger.info.assert_called()
        mock_logger.warning.assert_not_called()

    @patch("MCP_Server.connections.m4l.state")
    @patch("MCP_Server.connections.m4l.logger")
    def test_version_stored_in_state(self, mock_logger, mock_state):
        """Bridge version is saved to state.m4l_bridge_version."""
        ping_result = {"status": "success", "result": {"version": "3.2.1"}}
        with patch("MCP_Server.__version__", "3.2.0"):
            M4LConnection._check_bridge_version(ping_result)

        assert mock_state.m4l_bridge_version == "3.2.1"

    @patch("MCP_Server.connections.m4l.state")
    @patch("MCP_Server.connections.m4l.logger")
    def test_result_is_string_not_dict(self, mock_logger, mock_state):
        """If result is a non-dict (e.g. a string), treat as no version."""
        ping_result = {"status": "success", "result": "pong"}
        with patch("MCP_Server.__version__", "3.2.0"):
            M4LConnection._check_bridge_version(ping_result)

        # Should not raise, and should log info about missing version
        mock_logger.info.assert_called()
        mock_logger.warning.assert_not_called()
