import pytest
from MCP_Server.tools.creative import _generate_drum_notes, DRUM_PATTERNS


EXPECTED_STYLES = [
    "basic_rock", "house", "hiphop", "dnb",
    "halftime", "jazz_ride", "latin", "trap",
]

# Valid GM drum pitches used in DRUM_PATTERNS (36-51 inclusive).
VALID_GM_DRUM_MIN = 36
VALID_GM_DRUM_MAX = 51

REQUIRED_NOTE_KEYS = {"pitch", "start_time", "duration", "velocity"}


# ---------------------------------------------------------------------------
# DRUM_PATTERNS dict — basic structure tests
# ---------------------------------------------------------------------------

class TestDrumPatternsStructure:
    def test_all_eight_styles_present(self):
        assert set(DRUM_PATTERNS.keys()) == set(EXPECTED_STYLES)

    def test_each_pattern_is_list(self):
        for style, pattern in DRUM_PATTERNS.items():
            assert isinstance(pattern, list), f"{style} pattern is not a list"

    def test_each_pattern_is_nonempty(self):
        for style, pattern in DRUM_PATTERNS.items():
            assert len(pattern) > 0, f"{style} pattern is empty"

    def test_each_entry_is_tuple_with_four_elements(self):
        for style, pattern in DRUM_PATTERNS.items():
            for i, entry in enumerate(pattern):
                assert isinstance(entry, tuple), (
                    f"{style}[{i}] is {type(entry).__name__}, expected tuple"
                )
                assert len(entry) == 4, (
                    f"{style}[{i}] has {len(entry)} elements, expected 4 "
                    "(pitch, positions, vel_ratio, duration)"
                )

    def test_entry_types(self):
        """pitch=int, positions=list, vel_ratio=float/int, duration=float/int."""
        for style, pattern in DRUM_PATTERNS.items():
            for i, (pitch, positions, vel_ratio, duration) in enumerate(pattern):
                assert isinstance(pitch, int), f"{style}[{i}] pitch not int"
                assert isinstance(positions, list), f"{style}[{i}] positions not list"
                assert isinstance(vel_ratio, (int, float)), (
                    f"{style}[{i}] vel_ratio not numeric"
                )
                assert isinstance(duration, (int, float)), (
                    f"{style}[{i}] duration not numeric"
                )


# ---------------------------------------------------------------------------
# _generate_drum_notes() — pure function tests
# ---------------------------------------------------------------------------

class TestGenerateDrumNotesBasic:
    """Each style produces a non-empty list with correctly-structured notes."""

    @pytest.mark.parametrize("style", EXPECTED_STYLES)
    def test_returns_nonempty_list(self, style):
        notes = _generate_drum_notes(style)
        assert isinstance(notes, list)
        assert len(notes) > 0

    @pytest.mark.parametrize("style", EXPECTED_STYLES)
    def test_all_notes_have_required_keys(self, style):
        notes = _generate_drum_notes(style)
        for note in notes:
            assert REQUIRED_NOTE_KEYS.issubset(note.keys()), (
                f"Missing keys in note: {REQUIRED_NOTE_KEYS - note.keys()}"
            )

    @pytest.mark.parametrize("style", EXPECTED_STYLES)
    def test_all_pitches_are_valid_gm_drum_values(self, style):
        notes = _generate_drum_notes(style)
        for note in notes:
            assert VALID_GM_DRUM_MIN <= note["pitch"] <= VALID_GM_DRUM_MAX, (
                f"pitch {note['pitch']} out of GM drum range "
                f"[{VALID_GM_DRUM_MIN}, {VALID_GM_DRUM_MAX}]"
            )

    @pytest.mark.parametrize("style", EXPECTED_STYLES)
    def test_all_velocities_in_valid_range(self, style):
        notes = _generate_drum_notes(style)
        for note in notes:
            assert 1 <= note["velocity"] <= 127, (
                f"velocity {note['velocity']} out of range [1, 127]"
            )

    @pytest.mark.parametrize("style", EXPECTED_STYLES)
    def test_start_times_respect_clip_length(self, style):
        clip_length = 4.0
        notes = _generate_drum_notes(style, clip_length=clip_length)
        for note in notes:
            assert note["start_time"] < clip_length, (
                f"start_time {note['start_time']} >= clip_length {clip_length}"
            )

    @pytest.mark.parametrize("style", EXPECTED_STYLES)
    def test_durations_are_positive(self, style):
        notes = _generate_drum_notes(style)
        for note in notes:
            assert note["duration"] > 0, (
                f"duration {note['duration']} is not positive"
            )


class TestGenerateDrumNotesUnknownStyle:
    def test_unknown_style_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown style"):
            _generate_drum_notes("nonexistent_style")

    def test_error_message_lists_available_styles(self):
        with pytest.raises(ValueError, match="Available:"):
            _generate_drum_notes("bad_style")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="Unknown style"):
            _generate_drum_notes("")


class TestGenerateDrumNotesClipLength:
    """clip_length parameter limits notes correctly."""

    @pytest.mark.parametrize("style", EXPECTED_STYLES)
    def test_shorter_clip_produces_fewer_or_equal_notes(self, style):
        notes_long = _generate_drum_notes(style, clip_length=4.0)
        notes_short = _generate_drum_notes(style, clip_length=2.0)
        assert len(notes_short) <= len(notes_long), (
            f"{style}: shorter clip ({len(notes_short)} notes) should not have "
            f"more notes than longer clip ({len(notes_long)} notes)"
        )

    @pytest.mark.parametrize("style", EXPECTED_STYLES)
    def test_very_short_clip_excludes_late_notes(self, style):
        clip_length = 1.0
        notes = _generate_drum_notes(style, clip_length=clip_length)
        for note in notes:
            assert note["start_time"] < clip_length

    def test_clip_length_zero_produces_empty(self):
        # No notes can start at or beyond 0.0
        notes = _generate_drum_notes("basic_rock", clip_length=0.0)
        assert notes == []

    def test_fractional_clip_length(self):
        notes = _generate_drum_notes("basic_rock", clip_length=0.5)
        for note in notes:
            assert note["start_time"] < 0.5


class TestGenerateDrumNotesVelocity:
    """velocity parameter scales note velocities."""

    def test_default_velocity(self):
        notes = _generate_drum_notes("basic_rock", velocity=100)
        # All velocities should be > 0 and <= 127
        for note in notes:
            assert 1 <= note["velocity"] <= 127

    def test_low_velocity_clamps_to_one(self):
        notes = _generate_drum_notes("basic_rock", velocity=1)
        for note in notes:
            assert note["velocity"] >= 1

    def test_max_velocity_stays_within_bounds(self):
        notes = _generate_drum_notes("basic_rock", velocity=127)
        for note in notes:
            assert note["velocity"] <= 127

    def test_higher_base_velocity_produces_louder_notes(self):
        notes_quiet = _generate_drum_notes("basic_rock", velocity=50)
        notes_loud = _generate_drum_notes("basic_rock", velocity=120)
        avg_quiet = sum(n["velocity"] for n in notes_quiet) / len(notes_quiet)
        avg_loud = sum(n["velocity"] for n in notes_loud) / len(notes_loud)
        assert avg_loud > avg_quiet


class TestGenerateDrumNotesSwing:
    """Swing parameter shifts offbeat notes."""

    def test_no_swing_is_default(self):
        notes_default = _generate_drum_notes("basic_rock")
        notes_no_swing = _generate_drum_notes("basic_rock", swing=0.0)
        assert notes_default == notes_no_swing

    def test_swing_shifts_offbeat_notes(self):
        # Use "house" because its hi-hat positions (0.25, 0.75, …) satisfy the
        # offbeat condition (pos * 4) % 2 == 1.  "basic_rock" only has
        # positions at multiples of 0.5 so (pos * 4) is always even and no
        # notes would be shifted.
        notes_straight = _generate_drum_notes("house", swing=0.0)
        notes_swung = _generate_drum_notes("house", swing=0.5)

        # Build lookup: (pitch, original_position) -> start_time for easy comparison
        straight_times = {
            (n["pitch"], i): n["start_time"]
            for i, n in enumerate(notes_straight)
        }
        swung_times = {
            (n["pitch"], i): n["start_time"]
            for i, n in enumerate(notes_swung)
        }

        # At least one note should have a different start_time due to swing
        differences = [
            key for key in straight_times
            if key in swung_times and straight_times[key] != swung_times[key]
        ]
        assert len(differences) > 0, (
            "Expected at least one note to shift with swing=0.5"
        )

    def test_swing_only_affects_offbeat_positions(self):
        """Downbeat notes (those on integer/half-integer beats that are not
        offbeat per the (pos * 4) % 2 == 1 check) should remain unchanged."""
        notes_straight = _generate_drum_notes("basic_rock", swing=0.0)
        notes_swung = _generate_drum_notes("basic_rock", swing=0.5)

        for ns, nw in zip(notes_straight, notes_swung):
            pos = ns["start_time"]
            is_offbeat = (pos * 4) % 2 == 1
            if not is_offbeat:
                assert ns["start_time"] == nw["start_time"], (
                    f"On-beat note at {pos} should not be shifted by swing"
                )

    def test_swing_offset_direction_is_positive(self):
        """Swung offbeat notes should be shifted later (positive offset)."""
        notes_straight = _generate_drum_notes("basic_rock", swing=0.5)
        notes_no_swing = _generate_drum_notes("basic_rock", swing=0.0)

        for ns, nw in zip(notes_no_swing, notes_straight):
            pos = ns["start_time"]
            is_offbeat = (pos * 4) % 2 == 1
            if is_offbeat:
                assert nw["start_time"] >= ns["start_time"], (
                    f"Swung note at {pos} should be shifted later, not earlier"
                )

    def test_higher_swing_produces_larger_offset(self):
        """swing=1.0 should shift offbeat notes more than swing=0.25."""
        notes_low = _generate_drum_notes("house", swing=0.25)
        notes_high = _generate_drum_notes("house", swing=1.0)
        notes_base = _generate_drum_notes("house", swing=0.0)

        max_shift_low = 0.0
        max_shift_high = 0.0
        for nb, nl, nh in zip(notes_base, notes_low, notes_high):
            shift_l = abs(nl["start_time"] - nb["start_time"])
            shift_h = abs(nh["start_time"] - nb["start_time"])
            max_shift_low = max(max_shift_low, shift_l)
            max_shift_high = max(max_shift_high, shift_h)

        assert max_shift_high > max_shift_low, (
            "Higher swing should produce larger timing offsets"
        )


class TestGenerateDrumNotesNoteConsistency:
    """Cross-style consistency checks."""

    @pytest.mark.parametrize("style", EXPECTED_STYLES)
    def test_note_values_are_correct_types(self, style):
        notes = _generate_drum_notes(style)
        for note in notes:
            assert isinstance(note["pitch"], int)
            assert isinstance(note["start_time"], float)
            assert isinstance(note["duration"], float)
            assert isinstance(note["velocity"], int)

    @pytest.mark.parametrize("style", EXPECTED_STYLES)
    def test_no_duplicate_notes(self, style):
        """No two notes should have the same pitch and start_time."""
        notes = _generate_drum_notes(style)
        seen = set()
        for note in notes:
            key = (note["pitch"], note["start_time"])
            assert key not in seen, (
                f"Duplicate note at pitch={note['pitch']}, "
                f"start_time={note['start_time']}"
            )
            seen.add(key)
