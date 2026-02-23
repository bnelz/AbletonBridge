"""Tests for MCP_Server.cache.browser — URI map builder, resolver, and disk cache."""

import gzip
import json
import os
import time
import threading

import pytest

import MCP_Server.state as state
from MCP_Server.cache.browser import (
    build_device_uri_map,
    resolve_device_uri,
    save_browser_cache_to_disk,
    load_browser_cache_from_disk,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _item(
    name="Wavetable",
    uri="query:Instruments#Wavetable",
    is_loadable=True,
    is_device=True,
    category="Instruments",
):
    """Convenience factory for a flat-cache item dict."""
    return {
        "name": name,
        "search_name": name.lower(),
        "uri": uri,
        "is_loadable": is_loadable,
        "is_folder": False,
        "is_device": is_device,
        "category": category,
        "path": f"{category.lower()}/{name}",
    }


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_browser_state():
    """Reset the browser-related fields in global state after each test."""
    orig_flat = state.browser_cache_flat
    orig_by_cat = state.browser_cache_by_category
    orig_uri_map = state.device_uri_map
    orig_ts = state.browser_cache_timestamp
    orig_ready = state.browser_cache_ready.is_set()
    yield
    state.browser_cache_flat = orig_flat
    state.browser_cache_by_category = orig_by_cat
    state.device_uri_map = orig_uri_map
    state.browser_cache_timestamp = orig_ts
    if orig_ready:
        state.browser_cache_ready.set()
    else:
        state.browser_cache_ready.clear()


# ═══════════════════════════════════════════════════════════════════════════
# 1. build_device_uri_map
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildDeviceUriMap:
    """Tests for the URI lookup builder."""

    def test_excludes_items_without_uri(self):
        items = [_item(name="NoUri", uri="", is_loadable=True)]
        assert build_device_uri_map(items) == {}

    def test_excludes_items_not_loadable(self):
        items = [_item(name="Folder", uri="query:x#y", is_loadable=False)]
        assert build_device_uri_map(items) == {}

    def test_excludes_items_missing_both(self):
        items = [_item(name="Bad", uri="", is_loadable=False)]
        assert build_device_uri_map(items) == {}

    def test_basic_mapping(self):
        items = [_item(name="Wavetable", uri="query:Instruments#Wavetable")]
        result = build_device_uri_map(items)
        assert result == {"wavetable": "query:Instruments#Wavetable"}

    def test_keys_are_lowercase(self):
        items = [_item(name="AutoFilter", uri="query:Audio Effects#AutoFilter")]
        result = build_device_uri_map(items)
        assert "autofilter" in result
        assert "AutoFilter" not in result

    def test_duplicate_names_prefer_is_device_true(self):
        """When two items share a name, the one with is_device=True wins."""
        preset = _item(
            name="Compressor",
            uri="query:Sounds#Compressor",
            is_device=False,
            category="Sounds",
        )
        device = _item(
            name="Compressor",
            uri="query:Audio Effects#Compressor",
            is_device=True,
            category="Audio Effects",
        )
        # device added second — should still win via quality tuple
        result = build_device_uri_map([preset, device])
        assert result["compressor"] == "query:Audio Effects#Compressor"

        # reversed insertion order — device first, preset second should NOT overwrite
        result2 = build_device_uri_map([device, preset])
        assert result2["compressor"] == "query:Audio Effects#Compressor"

    def test_duplicate_names_use_category_priority(self):
        """Among same is_device value, lower CATEGORY_PRIORITY number wins."""
        instr = _item(
            name="Delay",
            uri="query:Instruments#Delay",
            is_device=True,
            category="Instruments",  # priority 0
        )
        fx = _item(
            name="Delay",
            uri="query:Audio Effects#Delay",
            is_device=True,
            category="Audio Effects",  # priority 1
        )
        # Instruments (0) beats Audio Effects (1)
        result = build_device_uri_map([fx, instr])
        assert result["delay"] == "query:Instruments#Delay"

    def test_unknown_category_gets_low_priority(self):
        """Items with an unrecognized category get priority 99 (lowest)."""
        known = _item(
            name="Limiter",
            uri="query:Audio Effects#Limiter",
            is_device=True,
            category="Audio Effects",  # priority 1
        )
        unknown = _item(
            name="Limiter",
            uri="query:Mystery#Limiter",
            is_device=True,
            category="Mystery",  # not in CATEGORY_PRIORITY → 99
        )
        result = build_device_uri_map([unknown, known])
        assert result["limiter"] == "query:Audio Effects#Limiter"

    def test_empty_list(self):
        assert build_device_uri_map([]) == {}

    def test_multiple_distinct_items(self):
        items = [
            _item(name="Reverb", uri="uri:reverb"),
            _item(name="Chorus", uri="uri:chorus"),
        ]
        result = build_device_uri_map(items)
        assert len(result) == 2
        assert result["reverb"] == "uri:reverb"
        assert result["chorus"] == "uri:chorus"


# ═══════════════════════════════════════════════════════════════════════════
# 2. resolve_device_uri
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveDeviceUri:
    """Tests for URI resolution."""

    def test_uri_with_colon_returned_as_is(self):
        """Input that already looks like a URI (contains ':') is returned verbatim."""
        uri = "query:Instruments#Wavetable"
        assert resolve_device_uri(uri) == uri

    def test_uri_with_hash_returned_as_is(self):
        """Input that contains '#' is returned verbatim."""
        uri = "Instruments#Wavetable"
        assert resolve_device_uri(uri) == uri

    def test_cache_ready_device_found(self):
        """When the cache is populated and the name matches, return the URI."""
        state.device_uri_map = {"wavetable": "query:Instruments#Wavetable"}
        state.browser_cache_ready.set()
        assert resolve_device_uri("Wavetable") == "query:Instruments#Wavetable"

    def test_cache_ready_device_found_case_insensitive(self):
        """Lookup is case-insensitive."""
        state.device_uri_map = {"autofilter": "query:Audio Effects#AutoFilter"}
        state.browser_cache_ready.set()
        assert resolve_device_uri("AUTOFILTER") == "query:Audio Effects#AutoFilter"

    def test_cache_ready_device_not_found_returns_input(self):
        """When device name is not in the map or flat cache, return input as-is."""
        state.device_uri_map = {"reverb": "query:Audio Effects#Reverb"}
        state.browser_cache_flat = []
        state.browser_cache_ready.set()
        result = resolve_device_uri("UnknownPlugin")
        assert result == "UnknownPlugin"

    def test_strips_whitespace_before_lookup(self):
        state.device_uri_map = {"reverb": "query:Audio Effects#Reverb"}
        state.browser_cache_ready.set()
        assert resolve_device_uri("  Reverb  ") == "query:Audio Effects#Reverb"

    def test_fallback_to_flat_cache_scan(self):
        """When device_uri_map misses but flat cache has the item, it is found."""
        state.device_uri_map = {}  # map is empty even after ready
        state.browser_cache_flat = [
            _item(name="Operator", uri="query:Instruments#Operator"),
        ]
        state.browser_cache_ready.set()
        assert resolve_device_uri("Operator") == "query:Instruments#Operator"


# ═══════════════════════════════════════════════════════════════════════════
# 3. Disk cache round-trip
# ═══════════════════════════════════════════════════════════════════════════


class TestDiskCacheRoundTrip:
    """Tests for save_browser_cache_to_disk / load_browser_cache_from_disk."""

    def test_save_writes_gzip_json(self, tmp_path, monkeypatch):
        """save_browser_cache_to_disk() writes valid gzip-compressed JSON."""
        cache_dir = str(tmp_path / "cache")
        cache_path = os.path.join(cache_dir, "browser_cache.json.gz")
        legacy_path = os.path.join(cache_dir, "browser_cache.json")

        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_DIR", cache_dir)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH", cache_path)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH_LEGACY", legacy_path)

        items = [_item(name="Wavetable", uri="query:Instruments#Wavetable")]
        state.browser_cache_flat = items
        state.browser_cache_by_category = {"Instruments": items}
        state.device_uri_map = {"wavetable": "query:Instruments#Wavetable"}
        state.browser_cache_timestamp = time.time()

        result = save_browser_cache_to_disk()
        assert result is True
        assert os.path.exists(cache_path)

        # Verify the file is valid gzip JSON with expected structure
        with gzip.open(cache_path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        assert data["version"] == 1
        assert data["flat"] == items
        assert data["by_category"] == {"Instruments": items}
        assert data["device_uri_map"] == {"wavetable": "query:Instruments#Wavetable"}
        assert isinstance(data["timestamp"], float)

    def test_save_returns_false_when_cache_empty(self, tmp_path, monkeypatch):
        """save returns False if browser_cache_flat is empty."""
        cache_dir = str(tmp_path / "cache")
        cache_path = os.path.join(cache_dir, "browser_cache.json.gz")
        legacy_path = os.path.join(cache_dir, "browser_cache.json")

        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_DIR", cache_dir)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH", cache_path)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH_LEGACY", legacy_path)

        state.browser_cache_flat = []
        assert save_browser_cache_to_disk() is False

    def test_save_removes_legacy_file(self, tmp_path, monkeypatch):
        """After saving the .gz cache, the legacy .json file is removed."""
        cache_dir = str(tmp_path / "cache")
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, "browser_cache.json.gz")
        legacy_path = os.path.join(cache_dir, "browser_cache.json")

        # Create a legacy file
        with open(legacy_path, "w") as f:
            f.write("{}")

        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_DIR", cache_dir)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH", cache_path)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH_LEGACY", legacy_path)

        state.browser_cache_flat = [_item()]
        state.browser_cache_timestamp = time.time()

        save_browser_cache_to_disk()
        assert not os.path.exists(legacy_path)

    def test_load_reads_back_saved_data(self, tmp_path, monkeypatch):
        """Round-trip: save then load restores identical state."""
        cache_dir = str(tmp_path / "cache")
        cache_path = os.path.join(cache_dir, "browser_cache.json.gz")
        legacy_path = os.path.join(cache_dir, "browser_cache.json")

        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_DIR", cache_dir)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH", cache_path)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH_LEGACY", legacy_path)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_MAX_AGE", 999999.0)

        items = [_item(name="Reverb", uri="query:Audio Effects#Reverb")]
        by_cat = {"Audio Effects": items}
        uri_map = {"reverb": "query:Audio Effects#Reverb"}
        ts = time.time()

        state.browser_cache_flat = items
        state.browser_cache_by_category = by_cat
        state.device_uri_map = uri_map
        state.browser_cache_timestamp = ts

        assert save_browser_cache_to_disk() is True

        # Clear state
        state.browser_cache_flat = []
        state.browser_cache_by_category = {}
        state.device_uri_map = {}
        state.browser_cache_timestamp = 0.0
        state.browser_cache_ready.clear()

        assert load_browser_cache_from_disk() is True

        # Verify restored state
        assert state.browser_cache_flat == items
        assert state.browser_cache_by_category == by_cat
        assert state.device_uri_map == uri_map
        assert state.browser_cache_timestamp == ts
        assert state.browser_cache_ready.is_set()

    def test_load_missing_file_returns_false(self, tmp_path, monkeypatch):
        """When neither .gz nor legacy file exists, load returns False."""
        cache_dir = str(tmp_path / "empty_cache")
        cache_path = os.path.join(cache_dir, "browser_cache.json.gz")
        legacy_path = os.path.join(cache_dir, "browser_cache.json")

        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_DIR", cache_dir)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH", cache_path)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH_LEGACY", legacy_path)

        assert load_browser_cache_from_disk() is False

    def test_load_rejects_stale_cache(self, tmp_path, monkeypatch):
        """Disk cache older than BROWSER_DISK_CACHE_MAX_AGE is ignored."""
        cache_dir = str(tmp_path / "cache")
        cache_path = os.path.join(cache_dir, "browser_cache.json.gz")
        legacy_path = os.path.join(cache_dir, "browser_cache.json")

        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_DIR", cache_dir)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH", cache_path)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH_LEGACY", legacy_path)
        # Set max age to 1 second so the cache is immediately stale
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_MAX_AGE", 1.0)

        items = [_item()]
        state.browser_cache_flat = items
        state.browser_cache_by_category = {"Instruments": items}
        state.device_uri_map = {"wavetable": "query:Instruments#Wavetable"}
        state.browser_cache_timestamp = time.time() - 100  # 100 seconds ago

        save_browser_cache_to_disk()

        # Clear state
        state.browser_cache_flat = []
        state.browser_cache_ready.clear()

        assert load_browser_cache_from_disk() is False
        # State should NOT have been restored
        assert state.browser_cache_flat == []

    def test_load_rejects_empty_flat_list(self, tmp_path, monkeypatch):
        """Disk cache with an empty 'flat' list is ignored."""
        cache_dir = str(tmp_path / "cache")
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, "browser_cache.json.gz")
        legacy_path = os.path.join(cache_dir, "browser_cache.json")

        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_DIR", cache_dir)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH", cache_path)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH_LEGACY", legacy_path)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_MAX_AGE", 999999.0)

        data = {
            "version": 1,
            "timestamp": time.time(),
            "flat": [],
            "by_category": {},
            "device_uri_map": {},
        }
        with gzip.open(cache_path, "wt", encoding="utf-8") as f:
            json.dump(data, f)

        assert load_browser_cache_from_disk() is False

    def test_load_rejects_bad_version(self, tmp_path, monkeypatch):
        """Disk cache with version != 1 is ignored."""
        cache_dir = str(tmp_path / "cache")
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, "browser_cache.json.gz")
        legacy_path = os.path.join(cache_dir, "browser_cache.json")

        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_DIR", cache_dir)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH", cache_path)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH_LEGACY", legacy_path)

        data = {
            "version": 99,
            "timestamp": time.time(),
            "flat": [{"name": "x"}],
        }
        with gzip.open(cache_path, "wt", encoding="utf-8") as f:
            json.dump(data, f)

        assert load_browser_cache_from_disk() is False

    def test_load_falls_back_to_legacy_path(self, tmp_path, monkeypatch):
        """When .gz file is missing but legacy .json exists, it is loaded."""
        cache_dir = str(tmp_path / "cache")
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, "browser_cache.json.gz")
        legacy_path = os.path.join(cache_dir, "browser_cache.json")

        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_DIR", cache_dir)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH", cache_path)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH_LEGACY", legacy_path)
        monkeypatch.setattr("MCP_Server.cache.browser.BROWSER_DISK_CACHE_MAX_AGE", 999999.0)

        items = [_item(name="Chorus", uri="query:Audio Effects#Chorus")]
        data = {
            "version": 1,
            "timestamp": time.time(),
            "flat": items,
            "by_category": {"Audio Effects": items},
            "device_uri_map": {"chorus": "query:Audio Effects#Chorus"},
        }
        # Write as plain JSON (not gzip) to the legacy path
        with open(legacy_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        assert load_browser_cache_from_disk() is True
        assert state.browser_cache_flat == items
        assert state.browser_cache_ready.is_set()
