"""Tests for AbletonConnection class and get_ableton_connection singleton."""

import json
import socket
import threading
import pytest
from unittest.mock import MagicMock, Mock, patch, call

from MCP_Server.connections.ableton import (
    AbletonConnection, get_ableton_connection, NON_IDEMPOTENT_COMMANDS,
    SLOW_COMMAND_TIMEOUTS,
)
from MCP_Server.constants import TIER_0_COMMANDS, TIER_1_COMMANDS, TIER_2_COMMANDS, MODIFYING_COMMANDS
import MCP_Server.state as state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response_bytes(payload: dict) -> bytes:
    """Encode a dict as a newline-delimited JSON bytes object, matching the wire format."""
    return (json.dumps(payload) + "\n").encode("utf-8")


def _successful_response(result=None):
    """Return a standard success response dict."""
    return {"status": "success", "result": result or {}}


# ---------------------------------------------------------------------------
# 1. AbletonConnection.send_command() with mocked socket
# ---------------------------------------------------------------------------

class TestSendCommand:
    """Tests for send_command with a mocked TCP socket."""

    def _make_connection(self, recv_data: bytes = None):
        """Create an AbletonConnection with a pre-attached mock socket.

        The mock socket's recv method returns *recv_data* on the first call,
        then b"" on subsequent calls (simulating connection close).
        """
        conn = AbletonConnection(host="localhost", port=9877)
        conn._recv_buffer = ""
        mock_sock = MagicMock(spec=socket.socket)
        if recv_data is not None:
            mock_sock.recv.return_value = recv_data
        conn.sock = mock_sock
        return conn, mock_sock

    # -- Successful round-trip ------------------------------------------

    def test_successful_command_round_trip(self):
        """send_command sends JSON+newline and returns parsed result."""
        expected_result = {"tempo": 120.0}
        response_bytes = _make_response_bytes(_successful_response(expected_result))
        conn, mock_sock = self._make_connection(response_bytes)

        result = conn.send_command("get_session_info")

        # Verify the socket received the correct payload
        sent_bytes = mock_sock.sendall.call_args[0][0]
        sent_payload = json.loads(sent_bytes.decode("utf-8").strip())
        assert sent_payload == {"type": "get_session_info", "params": {}}
        assert sent_bytes.endswith(b"\n"), "Payload must be newline-delimited"

        # Verify the parsed result
        assert result == expected_result

    def test_send_command_passes_params(self):
        """send_command forwards params dict to the wire payload."""
        params = {"track_index": 0, "name": "Bass"}
        response_bytes = _make_response_bytes(_successful_response())
        conn, mock_sock = self._make_connection(response_bytes)

        conn.send_command("set_track_name", params=params)

        sent_bytes = mock_sock.sendall.call_args[0][0]
        sent_payload = json.loads(sent_bytes.decode("utf-8").strip())
        assert sent_payload["params"] == params

    # -- Tier-based delays ---------------------------------------------

    @patch("MCP_Server.connections.ableton.time.sleep")
    def test_tier0_no_delay(self, mock_sleep):
        """Tier 0 commands should NOT call time.sleep at all."""
        tier0_cmd = next(iter(TIER_0_COMMANDS))
        response_bytes = _make_response_bytes(_successful_response())
        conn, _ = self._make_connection(response_bytes)

        conn.send_command(tier0_cmd)

        mock_sleep.assert_not_called()

    @patch("MCP_Server.connections.ableton.time.sleep")
    def test_tier1_post_delay_only(self, mock_sleep):
        """Tier 1 commands should sleep 0.05s after receiving the response."""
        tier1_cmd = next(iter(TIER_1_COMMANDS))
        response_bytes = _make_response_bytes(_successful_response())
        conn, _ = self._make_connection(response_bytes)

        conn.send_command(tier1_cmd)

        mock_sleep.assert_called_once_with(0.05)

    @patch("MCP_Server.connections.ableton.time.sleep")
    def test_tier2_pre_and_post_delay(self, mock_sleep):
        """Tier 2 commands should sleep 0.1s before AND after the response (twice)."""
        # Pick a Tier 2 command that is NOT non-idempotent to avoid retry logic
        # interfering. "load_instrument_or_effect" is Tier 2 but not in
        # NON_IDEMPOTENT_COMMANDS... actually let's just pick one and control the path.
        tier2_cmd = "load_instrument_or_effect"
        assert tier2_cmd in TIER_2_COMMANDS
        response_bytes = _make_response_bytes(_successful_response())
        conn, _ = self._make_connection(response_bytes)

        conn.send_command(tier2_cmd)

        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(0.1)
        # Both calls should be 0.1
        assert all(c == call(0.1) for c in mock_sleep.call_args_list)

    # -- Non-idempotent retry prevention --------------------------------

    def test_non_idempotent_max_attempts_is_one(self):
        """create_midi_track (non-idempotent) should NOT be retried on failure."""
        assert "create_midi_track" in NON_IDEMPOTENT_COMMANDS

        conn, mock_sock = self._make_connection()
        mock_sock.sendall.side_effect = socket.error("broken pipe")

        with pytest.raises(Exception, match="failed after 1 attempts"):
            conn.send_command("create_midi_track")

        # sendall should have been called exactly once (no retry)
        assert mock_sock.sendall.call_count == 1

    @patch("MCP_Server.connections.ableton.time.sleep")
    def test_non_idempotent_no_retry_on_recv_failure(self, mock_sleep):
        """Non-idempotent commands should raise immediately on receive failure."""
        conn, mock_sock = self._make_connection()
        mock_sock.sendall.return_value = None
        mock_sock.recv.side_effect = ConnectionResetError("reset")

        with pytest.raises(Exception, match="failed after 1 attempts"):
            conn.send_command("create_audio_track")

    # -- Idempotent retry -----------------------------------------------

    @patch("MCP_Server.connections.ableton.time.sleep")
    def test_idempotent_retries_once_on_socket_failure(self, mock_sleep):
        """Idempotent commands (max_attempts=2) should retry once after failure."""
        cmd = "get_session_info"
        assert cmd not in NON_IDEMPOTENT_COMMANDS

        response_bytes = _make_response_bytes(_successful_response({"tempo": 120}))

        conn = AbletonConnection(host="localhost", port=9877)
        conn._recv_buffer = ""

        # First socket: fails on sendall
        first_sock = MagicMock(spec=socket.socket)
        first_sock.sendall.side_effect = socket.error("broken pipe")
        conn.sock = first_sock

        # After reconnect, a new working socket appears
        second_sock = MagicMock(spec=socket.socket)
        second_sock.recv.return_value = response_bytes

        # Patch connect() so the retry path gets a fresh socket
        def fake_connect():
            conn.sock = second_sock
            conn._recv_buffer = ""
            return True

        with patch.object(conn, "connect", side_effect=fake_connect):
            result = conn.send_command(cmd)

        assert result == {"tempo": 120}
        # First socket tried once, second socket tried once
        assert first_sock.sendall.call_count == 1
        assert second_sock.sendall.call_count == 1

    @patch("MCP_Server.connections.ableton.time.sleep")
    def test_idempotent_raises_after_two_failures(self, mock_sleep):
        """If both attempts fail, the exception surfaces."""
        cmd = "get_session_info"

        conn = AbletonConnection(host="localhost", port=9877)
        conn._recv_buffer = ""

        failing_sock = MagicMock(spec=socket.socket)
        failing_sock.sendall.side_effect = socket.error("broken pipe")
        conn.sock = failing_sock

        # Reconnect also fails
        def fake_connect():
            conn.sock = MagicMock(spec=socket.socket)
            conn.sock.sendall.side_effect = socket.error("still broken")
            conn._recv_buffer = ""
            return True

        with patch.object(conn, "connect", side_effect=fake_connect):
            with pytest.raises(Exception, match="failed after 2 attempts"):
                conn.send_command(cmd)

    # -- Timeout handling -----------------------------------------------

    @patch("MCP_Server.connections.ableton.time.sleep")
    def test_timeout_raises_on_non_idempotent(self, mock_sleep):
        """socket.timeout during receive should propagate for non-idempotent commands."""
        conn, mock_sock = self._make_connection()
        mock_sock.sendall.return_value = None
        mock_sock.recv.side_effect = socket.timeout("timed out")

        with pytest.raises(Exception, match="failed after 1 attempts"):
            conn.send_command("create_midi_track")

    @patch("MCP_Server.connections.ableton.time.sleep")
    def test_timeout_allows_retry_for_idempotent(self, mock_sleep):
        """socket.timeout on first attempt should allow a retry for idempotent commands."""
        cmd = "get_session_info"
        response_bytes = _make_response_bytes(_successful_response({"ok": True}))

        conn = AbletonConnection(host="localhost", port=9877)
        conn._recv_buffer = ""

        # First socket: times out on recv
        first_sock = MagicMock(spec=socket.socket)
        first_sock.sendall.return_value = None
        first_sock.recv.side_effect = socket.timeout("timed out")
        conn.sock = first_sock

        # Second socket: succeeds
        second_sock = MagicMock(spec=socket.socket)
        second_sock.sendall.return_value = None
        second_sock.recv.return_value = response_bytes

        def fake_connect():
            conn.sock = second_sock
            conn._recv_buffer = ""
            return True

        with patch.object(conn, "connect", side_effect=fake_connect):
            result = conn.send_command(cmd)

        assert result == {"ok": True}


# ---------------------------------------------------------------------------
# 2. AbletonConnection.connect() / disconnect()
# ---------------------------------------------------------------------------

class TestConnectDisconnect:
    """Tests for connection lifecycle methods."""

    @patch("MCP_Server.connections.ableton.socket.socket")
    def test_successful_connect(self, mock_socket_cls):
        """connect() should set self.sock on success and return True."""
        mock_sock_instance = MagicMock()
        mock_socket_cls.return_value = mock_sock_instance

        conn = AbletonConnection(host="localhost", port=9877)
        result = conn.connect()

        assert result is True
        assert conn.sock is mock_sock_instance
        mock_sock_instance.connect.assert_called_once_with(("localhost", 9877))
        mock_sock_instance.settimeout.assert_called_once_with(5.0)

    @patch("MCP_Server.connections.ableton.socket.socket")
    def test_failed_connect_leaves_sock_none(self, mock_socket_cls):
        """connect() should leave self.sock as None on failure and return False."""
        mock_sock_instance = MagicMock()
        mock_sock_instance.connect.side_effect = ConnectionRefusedError("refused")
        mock_socket_cls.return_value = mock_sock_instance

        conn = AbletonConnection(host="localhost", port=9877)
        result = conn.connect()

        assert result is False
        assert conn.sock is None

    def test_connect_returns_true_if_already_connected(self):
        """connect() should short-circuit if sock is already set."""
        conn = AbletonConnection(host="localhost", port=9877)
        conn.sock = MagicMock()

        result = conn.connect()

        assert result is True

    def test_disconnect_closes_and_nils_socket(self):
        """disconnect() should close the socket and set it to None."""
        mock_sock = MagicMock()
        conn = AbletonConnection(host="localhost", port=9877)
        conn.sock = mock_sock

        conn.disconnect()

        mock_sock.close.assert_called_once()
        assert conn.sock is None

    def test_disconnect_when_not_connected(self):
        """disconnect() when already disconnected should not raise."""
        conn = AbletonConnection(host="localhost", port=9877)
        conn.sock = None

        conn.disconnect()  # Should not raise

        assert conn.sock is None

    def test_disconnect_handles_close_exception(self):
        """disconnect() should handle errors from sock.close() gracefully."""
        mock_sock = MagicMock()
        mock_sock.close.side_effect = OSError("close failed")
        conn = AbletonConnection(host="localhost", port=9877)
        conn.sock = mock_sock

        conn.disconnect()  # Should not raise

        assert conn.sock is None

    def test_disconnect_also_closes_udp_socket(self):
        """disconnect() should also close the UDP socket if present."""
        mock_tcp = MagicMock()
        mock_udp = MagicMock()
        conn = AbletonConnection(host="localhost", port=9877)
        conn.sock = mock_tcp
        conn._udp_sock = mock_udp

        conn.disconnect()

        mock_tcp.close.assert_called_once()
        mock_udp.close.assert_called_once()
        assert conn.sock is None
        assert conn._udp_sock is None


# ---------------------------------------------------------------------------
# 3. get_ableton_connection() singleton behavior
# ---------------------------------------------------------------------------

class TestGetAbletonConnection:
    """Tests for the module-level get_ableton_connection() factory."""

    def test_returns_existing_valid_connection(self):
        """Should return the existing connection if socket is valid."""
        mock_conn = MagicMock(spec=AbletonConnection)
        mock_conn.sock = MagicMock()
        mock_conn.sock.getpeername.return_value = ("localhost", 9877)
        state.ableton_connection = mock_conn

        result = get_ableton_connection()

        assert result is mock_conn
        # Should not have tried to create a new connection
        mock_conn.sock.getpeername.assert_called_once()

    @patch("MCP_Server.connections.ableton.AbletonConnection")
    def test_creates_new_connection_when_none(self, MockAbletonConn):
        """Should create a new AbletonConnection when state is None."""
        state.ableton_connection = None
        state.ableton_connected_event = threading.Event()

        mock_instance = MagicMock()
        mock_instance.connect.return_value = True
        mock_instance.send_command.return_value = {"status": "success"}
        MockAbletonConn.return_value = mock_instance

        result = get_ableton_connection()

        assert result is mock_instance
        MockAbletonConn.assert_called_with(host="localhost", port=9877)
        mock_instance.connect.assert_called()
        mock_instance.send_command.assert_called_with("get_session_info")

    @patch("MCP_Server.connections.ableton.AbletonConnection")
    def test_sets_connected_event_on_success(self, MockAbletonConn):
        """Should set ableton_connected_event after successful connection."""
        state.ableton_connection = None
        state.ableton_connected_event = threading.Event()
        assert not state.ableton_connected_event.is_set()

        mock_instance = MagicMock()
        mock_instance.connect.return_value = True
        mock_instance.send_command.return_value = {"status": "success"}
        MockAbletonConn.return_value = mock_instance

        get_ableton_connection()

        assert state.ableton_connected_event.is_set()

    @patch("MCP_Server.connections.ableton.time.sleep")
    @patch("MCP_Server.connections.ableton.AbletonConnection")
    def test_raises_after_all_attempts_fail(self, MockAbletonConn, mock_sleep):
        """Should raise an exception if all 3 connection attempts fail."""
        state.ableton_connection = None
        state.ableton_connected_event = threading.Event()

        mock_instance = MagicMock()
        mock_instance.connect.return_value = False
        MockAbletonConn.return_value = mock_instance

        with pytest.raises(Exception, match="Could not connect to Ableton"):
            get_ableton_connection()

    def test_replaces_stale_connection(self):
        """Should discard a connection whose socket is no longer valid."""
        stale_conn = MagicMock(spec=AbletonConnection)
        stale_conn.sock = MagicMock()
        stale_conn.sock.getpeername.side_effect = OSError("not connected")
        state.ableton_connection = stale_conn

        # After detecting the stale connection, it will try to create a new one.
        # Patch AbletonConnection so the new connection succeeds.
        with patch("MCP_Server.connections.ableton.AbletonConnection") as MockAbletonConn:
            new_instance = MagicMock()
            new_instance.connect.return_value = True
            new_instance.send_command.return_value = {"status": "success"}
            MockAbletonConn.return_value = new_instance

            state.ableton_connected_event = threading.Event()
            result = get_ableton_connection()

        assert result is new_instance
        stale_conn.disconnect.assert_called_once()

    def test_returns_existing_when_sock_is_none_triggers_reconnect(self):
        """If existing connection has sock=None, it should be discarded and rebuilt."""
        dead_conn = MagicMock(spec=AbletonConnection)
        dead_conn.sock = None
        state.ableton_connection = dead_conn

        with patch("MCP_Server.connections.ableton.AbletonConnection") as MockAbletonConn:
            new_instance = MagicMock()
            new_instance.connect.return_value = True
            new_instance.send_command.return_value = {"status": "success"}
            MockAbletonConn.return_value = new_instance

            state.ableton_connected_event = threading.Event()
            result = get_ableton_connection()

        assert result is new_instance


# ---------------------------------------------------------------------------
# 4. SLOW_COMMAND_TIMEOUTS
# ---------------------------------------------------------------------------

class TestSlowCommandTimeouts:
    """Verify slow-command timeouts are applied correctly."""

    def test_slow_commands_have_longer_timeouts(self):
        """All entries in SLOW_COMMAND_TIMEOUTS exceed the default 15s."""
        for cmd, timeout in SLOW_COMMAND_TIMEOUTS.items():
            assert timeout > 15.0, (
                f"{cmd} timeout ({timeout}) should exceed default 15s"
            )

    def test_freeze_has_longest_timeout(self):
        """freeze_track should have the longest timeout (60s)."""
        assert SLOW_COMMAND_TIMEOUTS["freeze_track"] == 60.0

    def test_load_instrument_timeout(self):
        """load_instrument_or_effect should get 30s."""
        assert SLOW_COMMAND_TIMEOUTS["load_instrument_or_effect"] == 30.0

    def test_slow_timeout_applied_in_send_command(self):
        """send_command should use SLOW_COMMAND_TIMEOUTS when no caller override."""
        conn = AbletonConnection(host="localhost", port=9877)
        mock_sock = MagicMock()
        conn.sock = mock_sock

        response_bytes = _make_response_bytes(_successful_response())
        mock_sock.recv.return_value = response_bytes

        with patch.object(conn, "receive_full_response", return_value=_successful_response().get("result", {})) as mock_recv:
            mock_recv.return_value = {"status": "success", "result": {}}
            # Monkey-patch to capture the timeout used
            original_recv = conn.receive_full_response
            captured_timeout = []

            def capture_recv(sock, timeout=15.0):
                captured_timeout.append(timeout)
                return {"status": "success", "result": {}}

            conn.receive_full_response = capture_recv
            conn.send_command("load_instrument_or_effect", {"track_index": 0, "uri": "test"})

            assert captured_timeout[0] == 30.0
