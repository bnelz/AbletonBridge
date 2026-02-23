"""Tests for compound workflow tools (MCP_Server/tools/workflows.py).

Every test uses the ``patch_ableton`` fixture from conftest.py so that no
real Ableton connection is needed, and ``resolve_device_uri`` is patched to
return a deterministic URI.
"""

import asyncio
import json
import pytest
from unittest.mock import MagicMock, patch, call

import MCP_Server.state as state
from MCP_Server.tools import workflows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_URI = "query:Instruments#Wavetable"


def _make_mock_mcp():
    """Return a mock MCP server whose ``.tool()`` decorator captures funcs.

    ``mock_mcp.tools`` maps function-name -> async-wrapped function so tests
    can look up and call individual tools by name.
    """
    mock_mcp = MagicMock()
    mock_mcp.tools = {}

    def _tool_decorator():
        def decorator(fn):
            mock_mcp.tools[fn.__name__] = fn
            return fn
        return decorator

    mock_mcp.tool = _tool_decorator
    return mock_mcp


@pytest.fixture
def mock_mcp():
    return _make_mock_mcp()


@pytest.fixture
def registered_tools(mock_mcp, patch_ableton):
    """Register workflow tools and yield (tools_dict, ableton_mock).

    The resolve_device_uri patch stays active for the entire test so that
    tool *invocations* (not just registration) see the fake URI.
    """
    with patch(
        "MCP_Server.tools.workflows.resolve_device_uri",
        return_value=FAKE_URI,
    ):
        workflows.register_tools(mock_mcp)
        yield mock_mcp.tools, patch_ableton


@pytest.fixture
def tools(registered_tools):
    """Shortcut: just the tools dict."""
    return registered_tools[0]


@pytest.fixture
def ableton_mock(registered_tools):
    """Shortcut: just the patched Ableton mock (named to avoid collision with conftest)."""
    return registered_tools[1]


# A dummy MCP Context (the tools accept ``ctx`` but never use it meaningfully).
FAKE_CTX = MagicMock()


# ---------------------------------------------------------------------------
# 1. create_instrument_track
# ---------------------------------------------------------------------------

class TestCreateInstrumentTrack:

    @pytest.mark.asyncio
    async def test_calls_send_command_three_times_without_color(
        self, tools, ableton_mock,
    ):
        """Without color_index the tool issues 3 send_command calls:
        create_midi_track, load_instrument_or_effect, set_track_name.
        """
        ableton_mock.send_command.return_value = {"index": 2}

        result_str = await tools["create_instrument_track"](
            FAKE_CTX,
            instrument_name="Wavetable",
            track_name="My Synth",
            index=-1,
            color_index=-1,
        )

        calls = ableton_mock.send_command.call_args_list
        assert len(calls) == 3

        assert calls[0] == call("create_midi_track", {"index": -1})
        assert calls[1] == call(
            "load_instrument_or_effect",
            {"track_index": 2, "uri": FAKE_URI},
        )
        assert calls[2] == call(
            "set_track_name",
            {"track_index": 2, "name": "My Synth"},
        )

        result = json.loads(result_str)
        assert result["track_index"] == 2
        assert result["instrument"] == "Wavetable"
        assert result["name"] == "My Synth"

    @pytest.mark.asyncio
    async def test_calls_send_command_four_times_with_color(
        self, tools, ableton_mock,
    ):
        """With a valid color_index the tool adds a 4th set_track_color call."""
        ableton_mock.send_command.return_value = {"index": 0}

        await tools["create_instrument_track"](
            FAKE_CTX,
            instrument_name="Drift",
            track_name="Lead",
            index=-1,
            color_index=5,
        )

        calls = ableton_mock.send_command.call_args_list
        assert len(calls) == 4

        assert calls[0] == call("create_midi_track", {"index": -1})
        assert calls[1] == call(
            "load_instrument_or_effect",
            {"track_index": 0, "uri": FAKE_URI},
        )
        assert calls[2] == call(
            "set_track_name",
            {"track_index": 0, "name": "Lead"},
        )
        assert calls[3] == call(
            "set_track_color",
            {"track_index": 0, "color_index": 5},
        )

    @pytest.mark.asyncio
    async def test_default_track_name_is_instrument_name(
        self, tools, ableton_mock,
    ):
        """When ``track_name`` is empty the tool falls back to instrument_name."""
        ableton_mock.send_command.return_value = {"index": 1}

        result_str = await tools["create_instrument_track"](
            FAKE_CTX,
            instrument_name="Operator",
            track_name="",
            index=-1,
            color_index=-1,
        )

        result = json.loads(result_str)
        assert result["name"] == "Operator"

        # set_track_name should use the instrument name
        name_call = ableton_mock.send_command.call_args_list[2]
        assert name_call == call(
            "set_track_name",
            {"track_index": 1, "name": "Operator"},
        )

    @pytest.mark.asyncio
    async def test_instrument_load_failure_does_not_raise(
        self, tools, ableton_mock,
    ):
        """If load_instrument_or_effect fails the tool still completes."""
        def _side_effect(cmd, params=None):
            if cmd == "load_instrument_or_effect":
                raise RuntimeError("device not found")
            return {"index": 0}

        ableton_mock.send_command.side_effect = _side_effect

        result_str = await tools["create_instrument_track"](
            FAKE_CTX,
            instrument_name="BadInstrument",
            track_name="Synth",
            index=-1,
            color_index=-1,
        )

        # Should still return a valid JSON result (not an error string)
        result = json.loads(result_str)
        assert result["track_index"] == 0


# ---------------------------------------------------------------------------
# 2. create_clip_with_notes
# ---------------------------------------------------------------------------

SAMPLE_NOTES = [
    {"pitch": 60, "start_time": 0.0, "duration": 0.5, "velocity": 100},
    {"pitch": 64, "start_time": 0.5, "duration": 0.5, "velocity": 80},
]


class TestCreateClipWithNotes:

    @pytest.mark.asyncio
    async def test_calls_create_clip_add_notes_set_name(
        self, tools, ableton_mock,
    ):
        ableton_mock.send_command.return_value = {"status": "success"}

        result_str = await tools["create_clip_with_notes"](
            FAKE_CTX,
            track_index=0,
            clip_index=1,
            length=4.0,
            notes=SAMPLE_NOTES,
            clip_name="My Clip",
        )

        calls = ableton_mock.send_command.call_args_list
        assert len(calls) == 3

        assert calls[0] == call("create_clip", {
            "track_index": 0, "clip_index": 1, "length": 4.0,
        })
        assert calls[1] == call("add_notes_to_clip", {
            "track_index": 0, "clip_index": 1, "notes": SAMPLE_NOTES,
        })
        assert calls[2] == call("set_clip_name", {
            "track_index": 0, "clip_index": 1, "name": "My Clip",
        })

        result = json.loads(result_str)
        assert result["note_count"] == 2
        assert result["name"] == "My Clip"

    @pytest.mark.asyncio
    async def test_no_set_clip_name_when_name_empty(
        self, tools, ableton_mock,
    ):
        """When ``clip_name`` is empty the tool skips set_clip_name."""
        ableton_mock.send_command.return_value = {"status": "success"}

        result_str = await tools["create_clip_with_notes"](
            FAKE_CTX,
            track_index=0,
            clip_index=0,
            length=2.0,
            notes=SAMPLE_NOTES,
            clip_name="",
        )

        calls = ableton_mock.send_command.call_args_list
        assert len(calls) == 2  # create_clip + add_notes_to_clip only

        result = json.loads(result_str)
        assert result["name"] == "(unnamed)"

    @pytest.mark.asyncio
    async def test_returns_correct_length_and_note_count(
        self, tools, ableton_mock,
    ):
        ableton_mock.send_command.return_value = {"status": "success"}
        three_notes = SAMPLE_NOTES + [
            {"pitch": 67, "start_time": 1.0, "duration": 0.25, "velocity": 90},
        ]

        result_str = await tools["create_clip_with_notes"](
            FAKE_CTX,
            track_index=1,
            clip_index=2,
            length=8.0,
            notes=three_notes,
            clip_name="",
        )

        result = json.loads(result_str)
        assert result["length"] == 8.0
        assert result["note_count"] == 3


# ---------------------------------------------------------------------------
# 3. get_full_session_state
# ---------------------------------------------------------------------------

class TestGetFullSessionState:

    @pytest.mark.asyncio
    async def test_calls_four_queries(self, tools, ableton_mock):
        """The tool must call get_session_info, get_all_tracks_info,
        get_return_tracks, and get_scenes — in that order.
        """
        session_data = {"tempo": 120, "name": "Test Project"}
        tracks_data = {"tracks": [{"name": "Track 1"}]}
        returns_data = {"tracks": [{"name": "Return A"}]}
        scenes_data = {"scenes": [{"name": "Scene 1"}]}

        ableton_mock.send_command.side_effect = [
            session_data,
            tracks_data,
            returns_data,
            scenes_data,
        ]

        result_str = await tools["get_full_session_state"](FAKE_CTX)

        calls = ableton_mock.send_command.call_args_list
        assert len(calls) == 4
        assert calls[0] == call("get_session_info")
        assert calls[1] == call("get_all_tracks_info")
        assert calls[2] == call("get_return_tracks")
        assert calls[3] == call("get_scenes")

    @pytest.mark.asyncio
    async def test_returns_combined_json(self, tools, ableton_mock):
        """The result JSON must combine all four queries under known keys."""
        session_data = {"tempo": 128}
        tracks_data = {"tracks": []}
        returns_data = {"tracks": []}
        scenes_data = {"scenes": []}

        ableton_mock.send_command.side_effect = [
            session_data,
            tracks_data,
            returns_data,
            scenes_data,
        ]

        result_str = await tools["get_full_session_state"](FAKE_CTX)
        result = json.loads(result_str)

        assert result["session"] == session_data
        assert result["tracks"] == tracks_data
        assert result["return_tracks"] == returns_data
        assert result["scenes"] == scenes_data


# ---------------------------------------------------------------------------
# 4. apply_effect_chain
# ---------------------------------------------------------------------------

class TestApplyEffectChain:

    @pytest.mark.asyncio
    async def test_loads_each_effect_in_order(self, tools, ableton_mock):
        """Each effect name should produce one load_instrument_or_effect call."""
        ableton_mock.send_command.return_value = {"status": "success"}
        effects = ["EQ Eight", "Compressor", "Limiter"]

        result_str = await tools["apply_effect_chain"](
            FAKE_CTX,
            track_index=0,
            effects=effects,
            track_type="track",
        )

        calls = ableton_mock.send_command.call_args_list
        assert len(calls) == 3
        for i, effect_name in enumerate(effects):
            assert calls[i] == call(
                "load_instrument_or_effect",
                {"track_index": 0, "uri": FAKE_URI, "track_type": "track"},
            )

        result = json.loads(result_str)
        assert result["loaded"] == effects
        assert result["failed"] == []

    @pytest.mark.asyncio
    async def test_handles_partial_failure(self, tools, ableton_mock):
        """If one effect fails the others are still attempted."""
        call_count = 0

        def _side_effect(cmd, params=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Compressor not found")
            return {"status": "success"}

        ableton_mock.send_command.side_effect = _side_effect

        result_str = await tools["apply_effect_chain"](
            FAKE_CTX,
            track_index=1,
            effects=["EQ Eight", "Compressor", "Limiter"],
            track_type="track",
        )

        result = json.loads(result_str)
        assert result["loaded"] == ["EQ Eight", "Limiter"]
        assert len(result["failed"]) == 1
        assert result["failed"][0]["effect"] == "Compressor"
        assert "Compressor not found" in result["failed"][0]["error"]

    @pytest.mark.asyncio
    async def test_all_effects_fail(self, tools, ableton_mock):
        ableton_mock.send_command.side_effect = RuntimeError("load error")

        result_str = await tools["apply_effect_chain"](
            FAKE_CTX,
            track_index=0,
            effects=["BadFX1", "BadFX2"],
            track_type="track",
        )

        result = json.loads(result_str)
        assert result["loaded"] == []
        assert len(result["failed"]) == 2


# ---------------------------------------------------------------------------
# 5. save_effect_chain / load_effect_chain — round-trip
# ---------------------------------------------------------------------------

class TestSaveLoadEffectChain:

    DEVICES = [
        {"name": "EQ Eight", "class_name": "Eq8", "index": 0},
        {"name": "Compressor", "class_name": "Compressor", "index": 1},
    ]

    @pytest.mark.asyncio
    async def test_save_captures_devices_from_track(
        self, tools, ableton_mock,
    ):
        """save_effect_chain should call get_track_info + get_device_parameters
        for each device and populate state.effect_chain_store.
        """
        ableton_mock.send_command.side_effect = self._save_side_effect()

        result_str = await tools["save_effect_chain"](
            FAKE_CTX,
            track_index=0,
            template_name="my_chain",
            track_type="track",
        )

        result = json.loads(result_str)
        assert result["template_name"] == "my_chain"
        assert result["device_count"] == 2

        # state should be populated
        assert "my_chain" in state.effect_chain_store
        template = state.effect_chain_store["my_chain"]
        assert len(template["devices"]) == 2
        assert template["devices"][0]["name"] == "EQ Eight"
        assert template["devices"][1]["name"] == "Compressor"

    @pytest.mark.asyncio
    async def test_load_loads_devices_back(self, tools, ableton_mock):
        """load_effect_chain reads from state and issues
        load_instrument_or_effect for each device.
        """
        # Pre-populate state as if save_effect_chain was called
        state.effect_chain_store["my_chain"] = {
            "name": "my_chain",
            "devices": [
                {"name": "EQ Eight", "class_name": "Eq8", "parameters": []},
                {"name": "Compressor", "class_name": "Compressor", "parameters": []},
            ],
            "source_track_type": "track",
        }

        ableton_mock.send_command.return_value = {"status": "success"}

        result_str = await tools["load_effect_chain"](
            FAKE_CTX,
            track_index=3,
            template_name="my_chain",
            track_type="track",
        )

        calls = ableton_mock.send_command.call_args_list
        assert len(calls) == 2
        for c in calls:
            assert c[0][0] == "load_instrument_or_effect"

        result = json.loads(result_str)
        assert result["loaded"] == ["EQ Eight", "Compressor"]
        assert result["failed"] == []

    @pytest.mark.asyncio
    async def test_round_trip(self, tools, ableton_mock):
        """Save then load produces the same device list."""
        # --- save ---
        ableton_mock.send_command.side_effect = self._save_side_effect()

        await tools["save_effect_chain"](
            FAKE_CTX,
            track_index=0,
            template_name="roundtrip",
            track_type="track",
        )

        assert "roundtrip" in state.effect_chain_store

        # --- load ---
        ableton_mock.send_command.reset_mock()
        ableton_mock.send_command.side_effect = None
        ableton_mock.send_command.return_value = {"status": "success"}

        result_str = await tools["load_effect_chain"](
            FAKE_CTX,
            track_index=5,
            template_name="roundtrip",
            track_type="track",
        )

        result = json.loads(result_str)
        saved_names = [
            d["name"]
            for d in state.effect_chain_store["roundtrip"]["devices"]
        ]
        assert result["loaded"] == saved_names

    @pytest.mark.asyncio
    async def test_load_missing_template_returns_error(
        self, tools, ableton_mock,
    ):
        """Loading a template that doesn't exist should return an error
        (ValueError caught by _tool_handler).
        """
        result_str = await tools["load_effect_chain"](
            FAKE_CTX,
            track_index=0,
            template_name="nonexistent",
            track_type="track",
        )

        # _tool_handler catches ValueError and returns "Invalid input: ..."
        assert "Invalid input" in result_str
        assert "nonexistent" in result_str

    # -- helper --

    def _save_side_effect(self):
        """Return a side_effect callable for the save workflow.

        Expected call sequence:
          1. get_track_info  -> returns devices list
          2. get_device_parameters (device 0)
          3. get_device_parameters (device 1)
        """
        responses = iter([
            # get_track_info
            {"devices": self.DEVICES},
            # get_device_parameters for device 0
            {"parameters": [{"name": "Frequency", "value": 1000}]},
            # get_device_parameters for device 1
            {"parameters": [{"name": "Threshold", "value": -10}]},
        ])

        def _side_effect(cmd, params=None):
            return next(responses)

        return _side_effect


# ---------------------------------------------------------------------------
# 6. create_drum_track
# ---------------------------------------------------------------------------

class TestCreateDrumTrack:

    @pytest.mark.asyncio
    async def test_calls_expected_commands_in_sequence(
        self, tools, ableton_mock,
    ):
        """create_drum_track should call create_midi_track,
        load_instrument_or_effect (Drum Rack), create_clip,
        add_notes_to_clip, set_track_name — in that order.
        """
        ableton_mock.send_command.return_value = {"index": 3}

        result_str = await tools["create_drum_track"](
            FAKE_CTX,
            style="basic_rock",
            track_name="Drums",
            clip_length=4.0,
            index=-1,
            color_index=-1,
            velocity=100,
            swing=0.0,
        )

        calls = ableton_mock.send_command.call_args_list
        cmd_names = [c[0][0] for c in calls]

        assert cmd_names[0] == "create_midi_track"
        assert cmd_names[1] == "load_instrument_or_effect"
        assert cmd_names[2] == "create_clip"
        assert cmd_names[3] == "add_notes_to_clip"
        assert cmd_names[4] == "set_track_name"

        # Verify the track index flows through
        result = json.loads(result_str)
        assert result["track_index"] == 3
        assert result["style"] == "basic_rock"
        assert result["name"] == "Drums"

    @pytest.mark.asyncio
    async def test_drum_rack_uri_resolved(self, tools, ableton_mock):
        """The Drum Rack device should be resolved via resolve_device_uri."""
        ableton_mock.send_command.return_value = {"index": 0}

        await tools["create_drum_track"](
            FAKE_CTX,
            style="house",
            track_name="",
            clip_length=4.0,
            index=-1,
            color_index=-1,
            velocity=100,
            swing=0.0,
        )

        load_call = ableton_mock.send_command.call_args_list[1]
        assert load_call == call(
            "load_instrument_or_effect",
            {"track_index": 0, "uri": FAKE_URI},
        )

    @pytest.mark.asyncio
    async def test_default_track_name_derived_from_style(
        self, tools, ableton_mock,
    ):
        """When track_name is empty the name is derived from the style."""
        ableton_mock.send_command.return_value = {"index": 0}

        result_str = await tools["create_drum_track"](
            FAKE_CTX,
            style="basic_rock",
            track_name="",
            clip_length=4.0,
            index=-1,
            color_index=-1,
            velocity=100,
            swing=0.0,
        )

        result = json.loads(result_str)
        assert result["name"] == "Basic Rock"

    @pytest.mark.asyncio
    async def test_notes_are_added_to_clip(self, tools, ableton_mock):
        """add_notes_to_clip should receive a non-empty notes list."""
        ableton_mock.send_command.return_value = {"index": 0}

        result_str = await tools["create_drum_track"](
            FAKE_CTX,
            style="house",
            track_name="House Beat",
            clip_length=4.0,
            index=-1,
            color_index=-1,
            velocity=100,
            swing=0.0,
        )

        add_notes_call = ableton_mock.send_command.call_args_list[3]
        assert add_notes_call[0][0] == "add_notes_to_clip"
        notes_sent = add_notes_call[0][1]["notes"]
        assert isinstance(notes_sent, list)
        assert len(notes_sent) > 0

        result = json.loads(result_str)
        assert result["note_count"] == len(notes_sent)

    @pytest.mark.asyncio
    async def test_color_index_adds_extra_call(self, tools, ableton_mock):
        """When color_index >= 0 an extra set_track_color call is made."""
        ableton_mock.send_command.return_value = {"index": 0}

        await tools["create_drum_track"](
            FAKE_CTX,
            style="basic_rock",
            track_name="Drums",
            clip_length=4.0,
            index=-1,
            color_index=10,
            velocity=100,
            swing=0.0,
        )

        cmd_names = [c[0][0] for c in ableton_mock.send_command.call_args_list]
        assert "set_track_color" in cmd_names

    @pytest.mark.asyncio
    async def test_clip_length_passed_through(self, tools, ableton_mock):
        ableton_mock.send_command.return_value = {"index": 0}

        result_str = await tools["create_drum_track"](
            FAKE_CTX,
            style="trap",
            track_name="",
            clip_length=8.0,
            index=-1,
            color_index=-1,
            velocity=100,
            swing=0.0,
        )

        create_clip_call = ableton_mock.send_command.call_args_list[2]
        assert create_clip_call[0][1]["length"] == 8.0

        result = json.loads(result_str)
        assert result["clip_length"] == 8.0
