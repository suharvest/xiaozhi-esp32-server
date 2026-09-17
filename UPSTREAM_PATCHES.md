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
| `core/utils/face_sync.py` | greeting-switch sync (warehouse→device), fail-safe. Face *library* push was removed 2026-08 — warehouse pushes faces directly via `POST /api/mcp/connections/{c}/devices/{d}/push-faces` |
| `plugins_func/functions/sync_face_library.py` | voice-trigger plugin for the above (name kept for back-compat; it now syncs the greeting switch only) |

**Merge action:** none. Just confirm they still exist.

---

## B. Modified upstream files (conflict points)

Grouped by theme. Each row: what we changed + why + how to verify after merge.

### B1. OVS / EdgeLLM integration

| File | Change | Verify after merge |
|---|---|---|
| `config.yaml` | Added `OpenVoiceStream` ASR/TTS provider blocks, `EdgeLLM` LLM block; default `selected_module` may reference them | `grep -E "OpenVoiceStream\|EdgeLLM" config.yaml` present; provider blocks intact |
| `core/providers/asr/sherpa_onnx_local.py` | Moved `modelscope` import inside the function (lazy) for macOS compat | import is inside the method, not module top |

### B1b. ASR fallback reply (2026-09-14)

**What**: `core/providers/asr/base.py` + `core/providers/asr/openvoicestream.py`
+ `config.yaml`. When ASR produces no text (backend unreachable / exception /
final-timeout with no partial / empty recognition), the server now speaks a
fallback phrase via TTS (FIRST+LAST single sentence, copied from
`intentHandler.speak_txt`) so the device finishes playback and returns to
standby instead of being stuck in "listening" until power-cycle.

**Why**: on the CM5 field box, an unreachable OVS backend made the server send
nothing at all — `handle_voice_stop()` only replied `if text_len > 0`, the
`else` branch was empty.

**How**: providers set `self.asr_failed_reason` (`backend_unreachable` /
`timeout` / `backend_error`); base picks `asr_failure_reply` (default
"识别服务暂时不可用，请稍后再试") when a reason is set, else
`asr_empty_reply` (default "没听清，请再说一遍"). Guarded by
`asr_fallback_enabled` (default true), `conn.client_abort`,
`conn.stop_event`; whole branch wrapped in try/except (warning only).
Explicit empty string for a reply key disables that branch.

**Rollback**: redeploy image tag `arm64-fix20260806` (edit compose `image:`
line back + `docker compose up -d xiaozhi-server`), or set
`asr_fallback_enabled: false` in `data/.config.yaml`.

**Verify after merge**: `grep -n "_maybe_speak_asr_fallback"
core/providers/asr/base.py` present; `grep -n "asr_failed_reason"
core/providers/asr/openvoicestream.py` ≥ 6 hits; `grep -n "asr_fallback_enabled" config.yaml` present.

### B1d. 工具结果上下文压缩（保真窗口 2 轮 / 历史压缩 / 上报不受影响）+ 上下文超限裁剪重试 + LLM 异常 FIRST+LAST 播报 (2026-09-16)

**What**: `core/connection.py` + `core/providers/asr/base.py` + `config.yaml`。
EdgeLLM 上下文上限 8192（可用输入 ~7064 token）不可上调，历史轮次里
`query_stock`/`search` 等工具返回的大 JSON 把上下文撑爆，此后该会话每句
都 400（`input_too_long`），用户感受「老是不回」；且 LLM 异常分支原来用
单句 MIDDLE 播报，设备通常不播、也不回待命（静默）。

**A1 工具结果压缩**（只作用于「喂给 LLM 的 `role="tool"` 消息内容」，
挂点仅限 `_handle_function_result()` 的 RECORD / REQLLM 两个写 tool 消息
分支；`enqueue_tool_report()` 仍收到**原始**工具结果，控制台历史/记忆
不受影响）：
- **保真窗口 = 最近 `tool_result_raw_turns` 轮**（默认 **2**，user→assistant
  为一轮）：窗口内 tool 内容**逐字保留**；单条超宽松上限
  `tool_result_latest_max_chars`（默认 **4000**）时只剥离与回答无关的
  巨型数组（`data`/`candidates`），`say` 全文保留；
- **窗口外的历史 tool 结果一律激进压缩**：保留 `ok`/`say`/`message` 等
  短字段 + 候选摘要（名字+库存+库位，最多
  `tool_result_candidate_limit`=**5** 条），套
  `tool_result_max_chars`（默认 **600**，仅作用于窗口外历史）；
- `stock_in`/`stock_out` 等写操作的 `say`/`message` **逐字保留（无论新旧）**；
- 划分方式（确定性，不用时间戳猜）：每次新一轮（`chat()` depth==0）开始
  时，以 dialogue 中**倒数第 N 个 user 消息**为界，把界前的 tool 消息就地
  压缩一次（带 `_tool_ctx_compressed` 标记，不重复压缩）。选「新轮开始时
  就地压缩」而非「拼装请求时压缩」，是因为检索路径（`get_llm_dialogue*`）
  有多个调用方，就地压缩只做一次、对所有调用方生效且不碰 dialogue.py；
- 只截内容、不删消息，OpenAI 工具协议（tool_calls/tool_call_id）完整。

**A2 上下文超限裁剪重试**：LLM 调用/流式消费遇 `input_too_long` →
可 grep WARNING「上下文超限」（含 got_tokens/max 数字）→ 确定性裁剪
（只留 system + 最近 2 轮 user + 最近一次工具交互，重对齐 tool_calls/tool
配对，不依赖 tokenizer）→ **最多重试一次**；仍失败 → 清空会话上下文
（保留 system 与 few-shot）+ FIRST+LAST 播 `llm_context_overflow_reply`
（默认「信息太多，我先清一下，请再说一遍」）。

**A3 LLM 异常分支改 FIRST+LAST**：非超限 LLM 异常用
`_speak_llm_fallback()`（写法同 `asr/base.py::_speak_fallback_phrase`，
已验证单句 MIDDLE 设备不播），话术取 `llm_error_reply`（默认沿用
`get_system_error_response()`）；守卫 `client_abort`/`stop_event`，
整个分支 try/except 只打 warning。

**A4 兜底去重**：`asr/base.py::_speak_fallback_phrase` 记录
`conn._last_fallback_ts`，8 秒内不重复播（避免 listen 超时兜底与 ASR 空
结果兜底连播两句）。

**Config**（默认值，无需现场改动）：`tool_result_raw_turns: 2`、
`tool_result_latest_max_chars: 4000`、`tool_result_max_chars: 600`、
`tool_result_candidate_limit: 5`、`llm_context_overflow_reply`
（`llm_error_reply` 注释掉，默认沿用 system_error_response）。

**Tests**: `main/xiaozhi-server/test/test_context_overflow.py`（pytest 或容器内
stdin 运行）：(a) 窗口外历史压缩到上限内且含候选摘要；(b) 窗口内逐字保留、
超限只剥巨型数组；(c) `enqueue_tool_report` 收到原始串；(d)(e)
input_too_long 裁剪重试/清空兜底；(f) 裁剪协议完整性；(g) 兜底 8 秒去重。

**Rollback**: 镜像回退 `arm64-allpatch-20260914c`（compose image 行 +
`up -d --no-deps xiaozhi-server`）。

**Verify after merge**: `grep -n "_compress_tool_results_outside_raw_window\|_cap_latest_tool_result_for_context\|_run_llm_stream_with_overflow_retry\|_speak_llm_fallback" core/connection.py` ≥ 6 hits；
`grep -n "tool_result_raw_turns" config.yaml` present；
`grep -n "_last_fallback_ts" core/providers/asr/base.py` present。

### B1c. Listen-timeout fallback（listen 超时兑底）(2026-09-16)

**What**: `core/providers/asr/base.py` +
`core/handle/textHandler/listenMessageHandler.py` + `config.yaml`. After a
`listen start`, if for N seconds (default 15) the server gets **no ASR text
and no voice_stop** (device WiFi jitter / firmware hang → audio never uploaded,
so `handle_voice_stop()` is never reached), the server now proactively speaks
a fallback phrase (FIRST+LAST, reusing the B1b speech path, now extracted into
`_speak_fallback_phrase()` — B1b behavior/logic unchanged) and resets this
round's audio state, so the device finishes playback and returns to standby
instead of hanging with a red LED.

**Hook points**: `ListenTextMessageHandler.handle()` state=="start" →
`conn.asr._start_listen_timeout(conn)` (asyncio task, ref kept on
`conn._listen_timeout_task`, cancellable); state=="stop" →
`_cancel_listen_timeout`; `ASRProviderBase.handle_voice_stop()` entry →
`_cancel_listen_timeout` (covers ASR text for both stream & non-stream
providers since both funnel through it). New listen start cancels the old
timer; `client_abort` / `stop_event` are guarded inside the waiter.

**Config** (defaults, no field change required): `asr_listen_timeout_enabled`
(true), `asr_listen_timeout_sec` (15), `asr_listen_timeout_reply` (defaults to
`asr_empty_reply`, i.e. "没听清，请再说一遍"; explicit empty string = silent).
Feature off → behavior identical to pre-patch.

**Log**: WARNING containing the fixed grep marker `listen 超时兜底` plus
session_id, waited seconds, audio-frame count received so far.

**Tests**: `main/xiaozhi-server/test/test_listen_timeout.py`（pytest 或容器内
stdin 运行）：(a) 零音频零文本到点播兜底话术（FIRST+LAST）+ 「listen 超时兜底」
WARNING + 本轮状态复位；(b) 窗口内到达 voice_stop/ASR 文本 → 定时器取消、不播；
(c) `client_abort=True` → 不播。

**Rollback**: `git checkout -- main/xiaozhi-server/core/providers/asr/base.py
main/xiaozhi-server/core/handle/textHandler/listenMessageHandler.py` (and
revert the config.yaml block), or runtime-disable by writing
`asr_listen_timeout_enabled: false` into `data/.config.yaml` and restarting.

**Verify after merge**: `grep -n "_start_listen_timeout\|_cancel_listen_timeout\|_listen_timeout_waiter" core/providers/asr/base.py` ≥ 6 hits; `grep -n "_start_listen_timeout" core/handle/textHandler/listenMessageHandler.py` = 1 hit; `grep -n "listen 超时兜底" core/providers/asr/base.py` present; `grep -n "asr_listen_timeout" config.yaml` = 3 keys.

### B1e. OVS TTS 会话槽节流（2026-09-16）

**What**: `core/providers/tts/openvoicestream_tts.py` + `config.yaml`。首句之后
按最小字数合并 TTS 分段、断开/换轮即中止在途 HTTP 流、429 重试收敛到有界等待。

**Why**: 现场 OVS 会话池是**全局计数器 2**（ASR 后端 1 + TTS 后端 1 相加，
`session_limiter.py` 不分模态）。每轮问答的 LLM 回答被切成 3~5 条 TTS HTTP
流，每条各抢一次槽；多设备并发或设备重连时 429（`too_many_sessions`）→
ASR 拿不到槽 → 走 B1b 兜底播「识别服务暂时不可用」→ 设备回待命，用户感受
「问一句不理人」。

**How**:
- **(A) 分段合并**：`_get_segment_text()` 覆盖 base 实现。首句沿用 base 规则
  （`first_sentence_max_chars`，首音频延迟不变）；之后攒够
  `subsequent_sentence_min_chars`（默认 **32**）字或收到 LAST 才发一条流，
  余文由 `_process_remaining_text_stream` 排空，不会丢字。
  配套：`client_abort` / sentence_id 换轮丢弃文本时一并清
  `processed_chars`/`tts_text_buff`；`SentenceType.FIRST` 分支补
  `is_first_sentence = True`（与 `tts/base.py:408` 对齐），否则子类会把整轮
  都当后续句缓冲、首音频被拖慢。
- **(B) 在途流可中止**：`text_to_speak` 的音频读取从 `async for
  resp.content.iter_any()` 改成 `wait_for(resp.content.readany(), 0.25)` 轮询，
  每次轮询前用 `_make_stop_checker(sid)` 检查 `_closed` / `conn.client_abort` /
  `conn.stop_event` / sentence_id 是否换轮；命中即 break，**不 flush 尾音、不
  推 LAST**，直接退出 `async with resp` 关连接，OVS 侧随即释放槽。
  `close()` 先置 `self._closed = True`，在途流在下一个 0.25s 轮询点自行退出。
- **(C) 429 重试收敛**：`_post_with_retry(..., stopped=)` 用
  `max_retries`（**2**）、单次退避与 `Retry-After` 都夹到 `retry_max_delay`
  （**1.5s**）、整个重试阶段共享 `retry_budget_seconds`（**3.0s**）deadline；
  退避改成 0.1s 分片睡，睡的过程中命中 `stopped()` 立即放弃。超预算即放弃
  这一句交回上层兜底，而不是把整轮堵死。
- **顺带修**：`_process_remaining_text_stream` 里 `processed_chars += len(full_text)`
  应为 `=`（它是「已消费字符数」不是增量），原写法多次调用后越界，后续文本
  被整段跳过。

**Config**（`TTS.OpenVoiceStream` 块，默认值即现场值）：
`subsequent_sentence_min_chars: 32`、`max_retries: 2`、`retry_max_delay: 1.5`、
`retry_budget_seconds: 3.0`。

**Tests**: `main/xiaozhi-server/test/test_tts_slot_saving.py`（a–g，pytest 或
容器内 stdin 运行）：(a) 首句仍按 base 规则出；(b) 后续句不足 32 字不发流；(c) 攒够即发；
(d) LAST 排空余文且 `processed_chars` 不越界；(e) 换轮/abort 清缓冲；
(f) `stopped()` 命中时中途退出且不推 LAST；(g) 重试预算耗尽返回 None。

**Deployed**: 现场镜像 `arm64-fix-20260916e`（瘦镜像）/ registry
`arm64-20260916`。切换后 10 分钟真实流量：`ovs_sessions_rejected_total` **0**，
每轮 TTS 流条数从 **3~5 降到 2**。

**Rollback**: 镜像回退 `arm64-fix-20260916d`（compose `image:` 行 +
`docker compose up -d xiaozhi-server`）。

**Verify after merge**: `grep -n "_make_stop_checker\|subsequent_sentence_min_chars\|retry_budget_seconds" core/providers/tts/openvoicestream_tts.py` ≥ 3 hits。

> **SUPERSEDED — VAD ONNX patch (commit `0ad7cf4a`).** We used to carry a
> `core/providers/vad/silero_onnx_wrapper.py` shim so Silero VAD ran on
> onnxruntime instead of torch. Upstream has since rewritten
> `core/providers/vad/silero.py` to use onnxruntime itself, our wrapper file no
> longer exists, and `core/providers/vad/` is **zero-diff vs `origin/main`**.
> No merge action needed — dropped from the maintenance list.

> Note: live ASR/TTS/LLM endpoints (orin-nx IPs) live in **`data/.config.yaml`**
> (gitignored), not `config.yaml`. See "Console mode" below for how these
> survive the manager/智控台 switch.

### B2. ~~Removed upstream call_device / address-book / device-call feature~~ — **ENTRY IS WRONG, DO NOT ACT ON IT**

> **This section was recorded in error.** The `call_device` / address-book /
> device-call feature does **not** exist in our merge-base — upstream added it
> in 2026, *after* the fork point. We never deleted it, so there is nothing
> here to re-apply or re-remove on merge. The table below is kept only so the
> next person doesn't "restore" a patch that never existed. If we later decide
> we don't want upstream's call_device, that becomes a new, deliberate removal.

| File | Change | Verify after merge |
|---|---|---|
| `plugins_func/functions/call_device.py` | **Deleted** entire file | file absent |
| `config/config_loader.py` | Removed `lookup_address_book` import | no `lookup_address_book` reference |
| `config/manage_api_client.py` | Removed `lookup_address_book()` func | same |
| `core/handle/sendAudioHandle.py` | Dropped `conn.calling` speaking-state branch | `if sentenceType == SentenceType.LAST:` (no `conn.calling`) |
| `core/handle/textHandler/listenMessageHandler.py` | Dropped `[device_call]` command handling + its imports | no `[device_call]` branch |

### B3. Conversation quality / performance (session 2026-05)

| File | Change | Verify after merge |
|---|---|---|
| `core/connection.py` | (1) thread `current_sentence_id` through `chat()` recursion + `_handle_function_result` to stop cross-turn TTS pollution; (2) `llm_history_turns` sliding window passed to dialogue; (3) emoji toggle `features.emoji` guard | `chat(self, query, depth=0, current_sentence_id=None)` signature; `max_history_turns=` passed |
| `core/utils/dialogue.py` | `_apply_history_window()` (slice at user-msg boundary) + symmetric `_ensure_tool_calls_complete` (drops orphan tool responses) | both helpers present; `get_llm_dialogue_with_memory(..., max_history_turns=None)` |
| `core/providers/tts/base.py` | (1) `current_sentence_id` init + stale-turn audio drop in `_audio_play_priority_thread`; (2) `subsequent_sentence_max_chars` soft-cap split | `self.current_sentence_id` in `__init__`; soft-cap branch in `_get_segment_text` |
| `core/providers/tools/unified_tool_manager.py` | `get_function_descriptions()` sorts tool names (stable prefix for edge-llm KV-cache) + refresh_tools cache note | `for name in sorted(tools.keys())` |

### B4. Face pipeline — greeting sync only (as of 2026-08)

| File | Change | Verify after merge |
|---|---|---|
| `core/providers/tools/device_mcp/mcp_handler.py` | After device-MCP set_ready, fire one `sync_face_state` auto-sync if `face_sync` configured + `self.face.add` exists; best-effort | hook calling `from core.utils.face_sync import sync_face_state` after `set_ready(True)` |

> **REMOVED 2026-08 — runtime face/speaker injection.** We used to patch
> `server_mcp/mcp_client.py` (discover `_meta.requires_face` / `requires_speaker`
> tools, cache `tools_requiring_face` / `tools_requiring_speaker`) and
> `server_mcp/mcp_manager.py` (`_inject_device_face` / `_inject_embedding` /
> `_inject_image` / `_inject_session_speaker` before forwarding such tools).
> Warehouse moved to an "option 3" architecture on 2026-07-18: the backend is
> the sole authority and pulls face identity from the device itself, so it no
> longer emits `requires_face`, and never emitted `requires_speaker`. With no
> trigger source left, this was dead code and has been deleted — both files are
> back to upstream shape for these hunks. **Do not re-apply.**

> **REMOVED 2026-08 — face-library push from `core/utils/face_sync.py`.**
> Warehouse pushes the library directly via
> `POST /api/mcp/connections/{c}/devices/{d}/push-faces` (with model_tag
> filtering, subject_id passthrough, 20-face cap — none of which our pusher
> had). `face_sync.py` now only syncs the greeting switch
> (`/api/face/config` → device `self.vision.mode`), which warehouse has no
> push channel for.

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
