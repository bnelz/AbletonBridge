# PLAN.md Completeness Review

**Date:** 2026-02-23
**Reviewed against:** ADVERSARIAL_ANALYSIS.md (49 issues) and current codebase state

---

## Executive Summary

**The plan is outdated.** It describes the codebase as a monolithic 11,839-line `server.py`, but the codebase has already been refactored into the modular structure the plan proposes. Phases 0, 1, and 2 are fully implemented. Phases 3, 4, and 6 are partially done. Phase 5 is mostly not started.

The plan should be updated to reflect current state and focus remaining effort on: tool count consolidation, expanded test coverage, Phase 5 feature gaps, and ~13 adversarial analysis items that were never addressed in any phase.

---

## Phase-by-Phase Status

### Phase 0: Critical Fixes — COMPLETE

| Item | Status | Evidence |
|---|---|---|
| 0.1 Fix Device URI blocking (60s) | Done | `cache/browser.py` uses `state.browser_cache_ready` event; `server.py:165` uses `ableton_connected_event.wait(timeout=30.0)` |
| 0.2 `get_server_capabilities` tool | Done | `tools/session.py:17` — returns version, connections, M4L status, cache state, feature flags |
| 0.3 Fix stale version fallback | Done | `__init__.py` defines `__version__ = "3.2.0"`, matches `pyproject.toml` |

### Phase 1: Performance — Latency Reduction — COMPLETE

| Item | Status | Evidence |
|---|---|---|
| 1.1 Tiered command delays | Done | `constants.py` defines Tier 0/1/2 command sets; `connections/ableton.py:152-157` applies per-tier delays |
| 1.2 Event-driven browser warmup | Done | `server.py:165` waits on `ableton_connected_event` with 30s timeout; loads disk cache first |
| 1.3 Async tool handlers | Done | `tools/_base.py:22-24` wraps all tools with `asyncio.to_thread()` |

### Phase 2: Architecture — Modularize — COMPLETE

| Item | Status | Evidence |
|---|---|---|
| 2.1 Module structure | Done | `connections/`, `cache/`, `dashboard/`, `tools/` directories; `server.py` is 360 lines |
| 2.2 Extract connections | Done | `connections/ableton.py` (270 lines), `connections/m4l.py` (728 lines) |
| 2.3 Extract cache | Done | `cache/browser.py` (432 lines) with `resolve_device_uri()` |
| 2.4 Extract dashboard | Done | `dashboard/server.py` (191 lines), `dashboard/html.py` (178 lines) |
| 2.5 Extract tool handlers | Done | 14 tool modules under `tools/`, each with `register_tools(mcp)` pattern |
| 2.6 Fix global mutable state | Done | `state.py` centralizes state with `threading.Lock()` instances |

### Phase 3: Compound Tools & Consolidation — PARTIALLY COMPLETE

| Item | Status | Evidence |
|---|---|---|
| 3.1 Compound workflow tools | Done | `tools/workflows.py` has 9 compound tools (create_instrument_track, create_clip_with_notes, etc.) |
| 3.2 Consolidate overlapping tools | **NOT DONE** | Still **334 tools** (was 331). No volume/pan/mute/solo consolidation by track_type |
| 3.3 Grid notation tools | Done | `tools/grid.py` has `clip_to_grid` and `grid_to_clip` |

**Missing compound tools from plan table:**
- `create_drum_track` — not in `workflows.py`
- `create_arrangement_section` — not in `workflows.py`

### Phase 4: Reliability & Testing — PARTIALLY COMPLETE

| Item | Status | Evidence |
|---|---|---|
| 4.1 Unit test suite | Partial | 5 test files exist. Missing: `test_connections.py`, `test_browser_cache.py`, `test_creative_tools.py`, `test_compound_tools.py` |
| 4.2 M4L command retry | Done | `connections/m4l.py:471` has `send_command_with_retry()` with backoff |
| 4.3 Error consistency | Partial | `tool_success()`/`tool_error()` defined in `_base.py` but most tools still use raw `json.dumps()` |
| 4.4 Input size limits | Done | `validation.py` defines limits and validation helpers |
| 4.5 Idempotency guards | Done | `NON_IDEMPOTENT_COMMANDS` in `connections/ableton.py` prevents retry on create/delete |

### Phase 5: Feature Gaps — MOSTLY NOT STARTED

| Item | Status |
|---|---|
| 5.1 Plugin info tool | Not done |
| 5.2 Preset browser tools | Not done |
| 5.3 Effect chain templates | Done (in `workflows.py`), but **no disk persistence** (plan specifies `~/.ableton-bridge/chain_templates.json`) |
| 5.4 Sidechain routing by name | Not done |
| 5.5 Spectral/loudness analysis | Not done |
| 5.6 VST/AU limitations doc | Not done |

### Phase 6: MCP Protocol Enrichment — MOSTLY COMPLETE

| Item | Status | Evidence |
|---|---|---|
| 6.1 MCP Resources | Done | `server.py:269-306` — `ableton://session`, `ableton://tracks`, `ableton://capabilities` |
| 6.2 MCP Prompts | Done | `prompts.py` — `create_beat`, `mix_track`, `sound_design`, `arrange_section` |
| 6.3 Version compatibility check | Done | `connections/m4l.py` has `_check_bridge_version()` |

---

## Adversarial Analysis Items NOT Addressed in Plan

The plan covers 23 of 49 issues from the analysis. These items have no corresponding plan work item:

### Performance (unaddressed)
- **1.6** M4L connection pooling — 50-200ms latency on first M4L call after idle (ping cache TTL kept but not improved)
- **1.7** Brute-force parameter display resolution — 10,000-iteration loop blocking Ableton's UI thread
- **1.8** Dashboard heavy imports — starlette/uvicorn still imported at startup (dashboard separated but not lazy-loaded)
- **1.10** JSON serialization overhead for large payloads (500+ notes, 6,400 browser items)
- **1.11** Tool call tracking lock contention between tool calls and dashboard refresh

### MCP Protocol (unaddressed)
- **2.3** Inconsistent tool descriptions — no quality pass on docstrings planned
- **2.4** No streaming/progress for long operations (MCP progress notifications not implemented)
- **2.5** Tool count still excessive — 334 tools (consolidation planned but not executed)

### Architecture (unaddressed)
- **4.4** Remote Script fixed 10s timeout — no command-specific timeout adjustment
- **4.5** UDP fire-and-forget has no backpressure or rate limiting
- **4.9** Unused `ctx: Context` parameter on every tool handler (FastMCP Context provides logging/progress)

### Security (unaddressed)
- **5.2** Dashboard XSS — HTML split to separate file but sanitization not enhanced

### Reliability (unaddressed)
- **6.1** No state tracking for partially-completed command sequences after connection loss
- **6.2** M4L chunked response reassembly fragility — no sequence validation, lost chunks block until timeout

### Feature Gaps (acknowledged as out-of-scope but worth tracking)
- 3.4 Instrument Rack deep access (nested racks, key/velocity zones)
- 3.5 Max for Live device editing
- 3.9 Freeze/flatten workflow (flatten not exposed)
- 3.10 Plugin CPU load monitoring (not in scripting API)
- 3.11 MPE support
- 3.12 Clip envelope follower
- 3.13 Tuning systems (Live 12 feature)
- 3.14 Multi-track clip launching strategies
- 3.15 Export/render capability (not in scripting API)

---

## Structural Issues with the Plan Document

1. **No status tracking.** The plan reads as if nothing has been done. It needs completion markers or should be rewritten to only cover remaining work.

2. **Dependency graph is stale.** Phase 2 is listed as a prerequisite for Phase 4, but both are already partially implemented. The remaining work items have different dependency relationships.

3. **Effort estimates are misleading.** "1-2 days" for Phase 0, "2-3 days" for Phase 1, etc. are all past tense. Remaining effort should be re-estimated for the actual remaining items.

4. **Success metrics table is outdated.** Some targets are already met (latency reduction), others haven't changed (tool count).

5. **Effect chain disk persistence is specified in Phase 5.3 but not implemented.** The in-memory `state.effect_chain_store` loses templates on restart, contradicting the plan's spec of persisting to `~/.ableton-bridge/chain_templates.json`.

6. **Risk assessment didn't materialize.** "Phase 2 module split introduces regressions" was marked Medium probability, but the split appears to have gone cleanly.

---

## Recommended Next Steps (Prioritized)

### High Priority
1. **Update PLAN.md** to reflect current state — mark completed items, create a "Remaining Work" section
2. **Tool consolidation (3.2)** — reduce 334 tools toward ~200 by merging track_type variants
3. **Expand test coverage (4.1)** — add `test_connections.py`, `test_browser_cache.py`, `test_creative_tools.py`

### Medium Priority
4. **Effect chain disk persistence (5.3)** — persist `effect_chain_store` to `~/.ableton-bridge/chain_templates.json`
5. **Standardize error responses (4.3)** — migrate remaining tools to `tool_success()`/`tool_error()`
6. **Missing compound tools (3.1)** — add `create_drum_track` and `create_arrangement_section`
7. **Brute-force parameter resolution (1.7)** — cap iterations or use binary search

### Lower Priority
8. Phase 5 feature gap items (plugin info, preset browser, sidechain routing by name)
9. MCP progress notifications for long operations
10. Tool description quality pass
