# Upstream Patches — Seeed fork of xiaozhi-esp32-server

This fork (`mine/main`, suharvest) tracks upstream `origin/main`
(xinnan-tech). It carries local changes for: OpenVoiceStream (OVS) ASR/TTS +
EdgeLLM integration, conversation-quality fixes, and an on-device **face**
pipeline (out-of-stock face auth + passive-greeting library sync).

**Purpose of this file:** every change we make to an *upstream-owned* file is
a merge conflict point. When you `git merge origin/main`, walk this list and
re-confirm each patch survived (or re-apply it). New files we added never
conflict and are listed separately for completeness.

Regenerate the change set anytime with:
```
git diff --name-status origin/main HEAD -- main/xiaozhi-server/
```

---

## A. New files (merge-safe — never conflict)

These are wholly ours; upstream doesn't touch them.

| File | Purpose |
|---|---|
| `core/providers/asr/openvoicestream.py` | OVS streaming ASR provider |
| `core/providers/tts/openvoicestream_tts.py` | OVS streaming TTS provider |
| `core/providers/tts/remote_tts.py`, `remote_tts_stream.py` | remote TTS base/stream |
| `core/providers/tts/sherpa_onnx_tts.py` | sherpa-onnx local TTS |
| `core/utils/face_sync.py` | face library + greeting-switch sync (warehouse→device), fail-safe |
| `plugins_func/functions/sync_face_library.py` | voice-trigger plugin for the above |

**Merge action:** none. Just confirm they still exist.

---

## B. Modified upstream files (conflict points)

Grouped by theme. Each row: what we changed + why + how to verify after merge.

### B1. OVS / EdgeLLM integration

| File | Change | Verify after merge |
|---|---|---|
| `config.yaml` | Added `OpenVoiceStream` ASR/TTS provider blocks, `EdgeLLM` LLM block; default `selected_module` may reference them | `grep -E "OpenVoiceStream\|EdgeLLM" config.yaml` present; provider blocks intact |
| `core/providers/asr/sherpa_onnx_local.py` | Moved `modelscope` import inside the function (lazy) for macOS compat | import is inside the method, not module top |

> Note: live ASR/TTS/LLM endpoints (orin-nx IPs) live in **`data/.config.yaml`**
> (gitignored), not `config.yaml`. See "Console mode" below for how these
> survive the manager/智控台 switch.

### B2. Removed upstream call_device / address-book / device-call feature

We don't use the device-to-device calling feature; removed it and its hooks.

| File | Change | Verify after merge |
|---|---|---|
| `plugins_func/functions/call_device.py` | **Deleted** entire file | file absent |
| `config/config_loader.py` | Removed `lookup_address_book` import | no `lookup_address_book` reference |
| `config/manage_api_client.py` | Removed `lookup_address_book()` func | same |
| `core/handle/sendAudioHandle.py` | Dropped `conn.calling` speaking-state branch | `if sentenceType == SentenceType.LAST:` (no `conn.calling`) |
| `core/handle/textHandler/listenMessageHandler.py` | Dropped `[device_call]` command handling + its imports | no `[device_call]` branch |

> If upstream evolves call_device, decide per-merge whether to keep removing it
> or adopt their version. Currently: keep removed.

### B3. Conversation quality / performance (session 2026-05)

| File | Change | Verify after merge |
|---|---|---|
| `core/connection.py` | (1) thread `current_sentence_id` through `chat()` recursion + `_handle_function_result` to stop cross-turn TTS pollution; (2) `llm_history_turns` sliding window passed to dialogue; (3) emoji toggle `features.emoji` guard | `chat(self, query, depth=0, current_sentence_id=None)` signature; `max_history_turns=` passed |
| `core/utils/dialogue.py` | `_apply_history_window()` (slice at user-msg boundary) + symmetric `_ensure_tool_calls_complete` (drops orphan tool responses) | both helpers present; `get_llm_dialogue_with_memory(..., max_history_turns=None)` |
| `core/providers/tts/base.py` | (1) `current_sentence_id` init + stale-turn audio drop in `_audio_play_priority_thread`; (2) `subsequent_sentence_max_chars` soft-cap split | `self.current_sentence_id` in `__init__`; soft-cap branch in `_get_segment_text` |
| `core/providers/tools/unified_tool_manager.py` | `get_function_descriptions()` sorts tool names (stable prefix for edge-llm KV-cache) + refresh_tools cache note | `for name in sorted(tools.keys())` |

### B4. Face pipeline (session 2026-05/06) — out-of-stock auth + greeting sync

| File | Change | Verify after merge |
|---|---|---|
| `core/providers/tools/server_mcp/mcp_client.py` | Discover `_meta.requires_face` tools at list_tools; cache `tools_requiring_face`; `requires_face()` query | `self.tools_requiring_face` set populated from `meta` |
| `core/providers/tools/server_mcp/mcp_manager.py` | `_inject_device_face` dual-topology (B: device embedding via `self.face.capture_embedding`; A: image via `self.camera.capture_raw`) before forwarding requires_face tools; fail-closed | `_inject_device_face` + `_inject_embedding` + `_inject_image` methods |
| `core/providers/tools/device_mcp/mcp_handler.py` | After device-MCP set_ready, fire one `sync_face_state` auto-sync if `face_sync` configured + `self.face.add` exists; best-effort | hook calling `from core.utils.face_sync import sync_face_state` after `set_ready(True)` |

### B5. Misc

| File | Change | Verify after merge |
|---|---|---|
| `docker-compose.yml` | `TZ=UTC` → `Asia/Shanghai` | TZ value |
| `agent-base-prompt.txt` | local prompt tweaks | diff vs upstream |

---

## C. Console mode (manager / 智控台) — how face survives the switch

When `read_config_from_api=true` (manager console), `config_loader.py`'s
`get_config_from_api_async` pulls server config from the Java manager-api DB —
which does NOT know about our `face_sync` block or the OVS/EdgeLLM provider
configs. Without intervention those would be lost in console mode.

Our mitigation (see `config/local_overrides.py` + the 1-line hook in
`config_loader.py`): after manager config is fetched, merge back the
**locally-authoritative** sections from `data/.config.yaml`:
`ASR`, `TTS`, `LLM`, `selected_module`, `face_sync`. This keeps custom
providers + face config on the local file while the manager only owns
agent-private config (role prompt, device binding, chat records).

**The hook is the single most merge-sensitive line** — if upstream rewrites
`get_config_from_api_async`, re-add the `apply_local_overrides(config_data,
config)` call at its end. See B-table note.

---

## Merge procedure

```
git fetch origin
git merge origin/main
# resolve conflicts; for each upstream-owned file in section B,
# confirm the patch survived using the "Verify after merge" column
git diff --name-status origin/main HEAD -- main/xiaozhi-server/   # sanity
# run: python -m py_compile on touched files; smoke test ASR/TTS/LLM + face
```

Keep this file updated whenever you patch a new upstream-owned file.
