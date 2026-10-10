# StreamBot – PLAN v3 (what's next)

Written Sat 10 Oct 2026 against `main` at **933dd35** (PRs #1–#7 merged, all of PLAN-v2 phases 0–5 shipped).
Read-only review of `C:\AI\Bot`: every file in `src/`, `tests/`, `README.md`, `config.yaml`, `persona.yaml`, `requirements*.txt`, `pyproject.toml`, `start.bat`, `scripts/backup.ps1`, `.github/workflows/ci.yml`, plus `git show HEAD:PLAN-v2.md`. `.env` was not opened. No code was changed.

---

## 1. How to use this plan with Cursor

1. Work **one task ID at a time** (e.g. "Do V3-P1-02 from PLAN-v3.md"). Each task lists *Why*, *Do*, *Files*, *Acceptance* and *Depends on*.
2. Start a fresh branch per phase (`cursor/v3-phase-1`), and open one PR per phase as you did for v2.
3. Ask Cursor to **write the test first** where the acceptance criteria name a test, then make it pass. Run `python -m pytest -q` and `python -m ruff check src tests` before every commit.
4. Tasks marked **💡 Suggestion** are new features. They are grounded in what the code already supports, but they are optional: skip any you don't want.
5. Tasks marked **🧑 Owner** are for you, not Cursor (live checks, GitHub settings, Twitch tokens).
6. Line numbers are as of 933dd35 and will drift; search for the quoted function name if they don't match.

Legend: 🔴 broken / risky · 🟠 should fix · 🟡 polish · 💡 suggestion · 🧑 owner-only

---

## 2. Current state (overview)

| Area | Where | State at 933dd35 |
|---|---|---|
| App / API | `src/main.py`, `src/security.py` | FastAPI booth on 127.0.0.1, `BoothGuard` (Origin check, JSON-only writes, per-launch `X-Cohost-Token` from `/api/session`), `TrustedHostMiddleware` |
| Brain | `src/orchestrator.py` (1,696 lines) | Chat worker + event worker queues, priority alerts, follow/cheer batching, rewards with per-reward cooldown/prompt, profiles, queue drop/answer/replay, recap, markers, shoutouts |
| Voice in | `src/audio/mic.py`, `stt.py`, `vad.py`, `wake.py` | Non-blocking mic loop, barge-in, echo gate, optional openwakeword, Whisper with CUDA→CPU fallback |
| Voice out | `src/tts/piper.py`, `src/audio/duck.py` | Persistent Piper with drained pipes, one-shot fallback and retry, pinned SHA-256 downloads, optional per-session ducking via pycaw |
| Twitch | `src/twitch/chat.py`, `eventsub.py`, `auth.py`, `helix.py` | TwitchIO 2.10 IRC; EventSub WebSocket with token validate, per-feed scope filtering, keepalive timeout, reconnect handover, revocation, backoff with jitter; Helix shoutout + marker |
| Memory | `src/memory.py`, `recap.py` | Per-person threads, notes, session-scoped greeting, forget, retention prune, atomic write + `.bak` |
| Safety | `src/safety.py`, `src/policy.py` | Viewer-text fencing, slur/ask block-list, output guard (links, stray @mentions, leading `!`/`/`), mod-gated `!so` and `!pixel` controls |
| UI | `src/web/static/*` | Dashboard (queue, profiles, recap, persona editor, devices, VAD sliders, forget) and read-only OBS overlay with URL options |
| Tooling | CI, `pyproject.toml`, `scripts/backup.ps1`, `start.bat` | Windows CI (ruff + pytest), pinned runtime deps, dev deps split, backup script, start.bat stamp only after pip succeeds |

Local test run on her PC (no caches written): **92 passed, 1 skipped, 1 failed** (`tests/test_phase2.py::test_host_origin_and_token_guard`, see F-01). Ruff: clean. CI on GitHub for 933dd35: green.

---

## 3. What changed since PLAN-v2

Everything in v2 phases 0–5 landed. Highlights:

- **Security:** control token + Origin + Host checks on the booth API; persona size/shape validation; output guard on everything posted to chat; EventSub scope report per feed.
- **Reliability:** mic loop no longer blocks; Piper pipes are drained and the persistent process retries; EventSub handover/keepalive/revocation/backoff; chat staleness drop; LLM `num_ctx`/`num_predict`/`keep_alive`/chat timeout; memory `.bak` recovery.
- **Features:** channel-point reward mapping (speak/chat/ignore, prompt, cooldown), bits/hype train/polls/predictions (off by default), first-time chatter welcome, `!pixel mute|unmute|chill|hype|stop` for mods, queue controls, replay, profiles, recap with optional "vibe", stream markers, wake word, ducking, forget-viewer.
- **Tooling:** Windows CI, pinned requirements, dev requirements, `.gitattributes`, backup script, 94 tests.
- **Removed:** `requirements.lock.txt` (folded into pinned `requirements.txt`).

Still owner-only from v2: **V0-01 secret scanning / push protection** and all the **live checks** (see section 5).

---

## 4. Findings (new, with file references)

### Broken or risky

- **F-01 🔴 IPv6 loopback Host is rejected, and the dependency set has drifted.** `tests/test_phase2.py:54` expects `Host: [::1]:8080` → 200 but gets 400 on her PC. The venv has `starlette==1.6.0`, which `requirements.txt` does not pin (only `fastapi==0.141.1`), so CI and the PC resolve different Starlette versions. `trusted_hosts()` in `src/security.py:20` also always uses port 8080 because `src/main.py:141` never passes `settings.port`. Result: `http://[::1]:<port>/` can be refused, and "green on CI, red at home" will keep happening.
- **F-02 🔴 Any allowlisted command that isn't a "speak" command is treated as a shoutout.** `src/orchestrator.py:1434` runs the shoutout branch for `action == "command"`, and `chat_action()` (`src/policy.py:37`) returns `"command"` for *every* allowlisted command outside `!pixel`/`!ask`. The mod check at line 1435 only applies to commands in `chat.mod_commands`. If `chat.commands` ever gains `!lurk`, then `!lurk someone` from any viewer fires a Helix shoutout to `someone`. Latent today (only `!so`, `!pixel`, `!ask` are allowlisted) but one config line away.
- **F-03 🟠 Conversation history is sent to the model unfenced.** `ConversationMemory.llm_messages()` (`src/memory.py:574-582`) turns stored viewer turns into `{"role": "user", "content": "name (chat): text"}` with no `<<viewer>>` fence. Only the *current* line and the roster are fenced. A prompt-injection line sits in the thread for up to 12 turns and is replayed as plain user content on every reply to that viewer.
- **F-04 🟠 Alert and recap model calls have no timeout.** `_finish_reply()` (`src/orchestrator.py:928`) and `make_recap(vibe=True)` (`:1724`) call `llm.complete()` without `asyncio.wait_for`; only chat uses `chat_timeout_seconds`. A stuck Ollama (default HTTP timeout 120 s, twice with the retry) freezes the event worker, and alerts pile up behind it.
- **F-05 🟠 The wake-word meter is hidden exactly when it matters, and scoring re-feeds audio.** `_level_loop()` (`src/orchestrator.py:1797`) only broadcasts while listening/PTT, but wake scoring only runs when *not* armed (`src/audio/mic.py:186`). The dashboard's `wake 0.xx` therefore never shows during wake-only use. `mic._run()` also re-scores an overlapping 80–320 ms window on every chunk; openwakeword's `Model.predict` is stateful/streaming, so the same frames are fed repeatedly. Needs a live check (owner list) and probably a "feed each new 1,280-sample frame once" fix.
- **F-06 🟠 Token health is a one-shot snapshot.** `EventSubClient.refresh_auth()` (`src/twitch/eventsub.py:402`) validates once per connect; `expires_in` never counts down, so the "expires in under 24 hours" hint (`src/health.py:161`) is stale for long sessions. Twitch expects apps to re-validate hourly. The **IRC** token is never validated at all, and the shoutout (`moderator:manage:shoutouts`) and marker (`channel:manage:broadcast`) scopes are not checked up front, so those buttons only fail at click time.
- **F-07 🟠 Duck refusal rule no longer matches the duck implementation.** `Duck._speaker_is_separate()` (`src/audio/duck.py:87`) refuses unless `speaker_device` is a *different index* from the default output. That rule made sense for endpoint ducking, but the code now ducks **other processes' sessions** and skips its own PID (`:38`), so Pixel can't duck herself on the same device. With the shipped `speaker_device: null` ducking can never turn on. It also ducks *every* other app (OBS monitoring, Discord), with no allow/deny list. README still says "Windows endpoint ducking".

### Should fix

- **F-08 🟡 `booth.json` and `streamer.json` are written non-atomically.** `save_booth()` (`src/booth.py:39`) and `save_identity()` (`src/identity.py:41`) use `write_text` directly; `atomic_write_text()` already exists in `src/settings.py:311`. A crash mid-write silently resets the booth (the loader swallows errors).
- **F-09 🟡 Recap overwrites and under-reports.** `write_recap()` (`src/recap.py:72`) names files by date only, so two streams in one day overwrite each other. Classification is substring-based ("sub" in text), so cheers, redeems, hype trains, polls and predictions never appear, and "noted moments" mixes old notes from previous sessions because notes aren't session-scoped.
- **F-10 🟡 Interrupted Piper requests leak temp WAVs.** In `_synth_persistent()` (`src/tts/piper.py:386-391`) an interrupt unlinks `out_path` and returns, but Piper still writes that file afterwards. Every barge-in can leave a WAV in `%TEMP%`.
- **F-11 🟡 Restart forgets a manual mood when a profile is active.** `Orchestrator.__init__` loads `booth.mood` (line 114), then `_install_profile()` (line 129) overwrites it with the profile/base mood.
- **F-12 🟡 Every shoutout says "X, welcome in." out loud and in chat.** `shoutout()` (`src/orchestrator.py:1628`) uses that line even for `!so friend` when nobody raided, and always speaks it via `say_line()`.
- **F-13 🟡 "Pixel" is still hardcoded.** `src/policy.py:5` (`!pixel`), `src/memory.py:143` fallback, `src/web/static/app.js:214` hint, `config.yaml` comments. Renaming the persona leaves the command and UI text behind.
- **F-14 🟡 Hype-train progress is captured but unused.** `on_stream_event()` patches `hype_level` (`src/orchestrator.py:753`) but neither dashboard nor overlay reads it.
- **F-15 🟡 `orchestrator.py` is 1,696 lines.** Chat, events, voice, Twitch connection management, queue and recap all live in one class, which makes Cursor edits risky and tests slow to write.
- **F-16 🟡 Tests are not fully hermetic.** The test that fails locally depends on the installed Starlette; `tests/conftest.py` builds `Settings` but several modules still import `ROOT` paths. There's also no coverage threshold in CI even though `pytest-cov` is installed.
- **F-17 🟡 Log level is fixed at INFO** (`src/logsetup.py:50`); there's no `logging.level` key and no way to turn on `cohost.mic`/`cohost.tts` debug while diagnosing a live issue.

Checked and fine: Hype Train subscriptions use version `2`, which is correct (v1 was withdrawn on 15 Jan 2026). Non-ASCII characters that show as `�` in PowerShell output (`·`, `…`, `‹‹`) are valid UTF-8 in the files. They only look wrong in the console.

---

## 5. Owner checklist (live and manual checks still outstanding from v2) 🧑

Do these on a test stream or with the booth open before relying on the features. Tick them here when done.

- [ ] **V3-P0-01 Secret scanning (v2 V0-01).** GitHub → Kisahime/StreamBot → Settings → Code security: turn on **Secret scanning** and **Push protection**. The repo is public, so this matters. Confirm `git log --all -- .env` is empty.
- [ ] **V3-P0-02 Token scopes.** Start the booth, click **Connect alerts**, and read the feed list on the dashboard. Every feed you want should say `ok`, not `missing scope …`. Broadcaster token needs: `moderator:read:followers`, `channel:read:subscriptions`, plus `bits:read`, `channel:read:redemptions`, `channel:read:hype_train`, `channel:read:polls`, `channel:read:predictions` for the optional feeds, plus `moderator:manage:shoutouts` (shoutouts) and `channel:manage:broadcast` (markers). Chat token needs `chat:read` and `chat:edit`.
- [ ] **V3-P0-03 Mic barge-in.** With speakers (not headphones), let Pixel talk and speak over her. She should stop within about 300 ms. Then let her talk without speaking. She must **not** cut herself off from speaker bleed. Adjust `audio.echo_gate_ratio` if she does.
- [ ] **V3-P0-04 Wake word.** `pip install openwakeword`, set `audio.wake_word: true`, restart. Say "hey jarvis" ten times at normal volume and count detections; then talk normally for two minutes and count false triggers. Note: the dashboard wake score won't show while not listening (F-05), so watch `logs/cohost.log`.
- [ ] **V3-P0-05 Ducking.** `pip install pycaw`, play music in another app, enable **Duck output**. Expect: with `speaker_device: null` it refuses (F-07). Set a separate speaker device and confirm music dips while Pixel talks and comes back. Kill the app mid-sentence (close the window) and relaunch: the music volume must be restored from `data/duck.json`.
- [ ] **V3-P0-06 OBS overlay.** Add `http://127.0.0.1:8080/overlay?hide=idle&position=bottom` as a Browser Source. Check captions, the speaking nameplate, an alert flash (use a test follow or redeem), `?alerts=0`, `?theme=` and `?fontsize=`. Look for console errors in OBS's browser-source interaction window.
- [ ] **V3-P0-07 Rewards, polls, predictions, first-time chatters.** Turn on `events.channel_points`, `events.polls`, `events.predictions` one at a time. Redeem `hydrate` (should speak), redeem an unmapped reward (should be skipped with reason `reward ignored`), run a short poll and prediction (one line at start, one at end with the winner). Have a fresh account chat for the first time and confirm a typed welcome with no voice.
- [ ] **V3-P0-08 Shoutout and marker.** Click **Shout out last raider** after a real or test raid, and **Remember this** while live. Both should succeed or show a clear scope error.
- [ ] **V3-P0-09 Local test failure.** Run `.venv\Scripts\python.exe -m pip install -r requirements.txt` then `python -m pytest -q`. If `test_host_origin_and_token_guard` still fails, that's F-01. Hand V3-P1-01 to Cursor.

---

## 6. Phased to-do list

### Phase 1: Fix what's broken (do first)

- [x] **V3-P1-01 🔴 Pin the transitive web stack and accept IPv6 loopback on the real port** (F-01)
  - *Do:* add `starlette==<version that CI uses>` and `anyio==…` (and any other resolved transitive deps of fastapi/uvicorn) to `requirements.txt`. Pass `settings.port` into `trusted_hosts()` in `create_app` (read settings before middleware is added, or build the middleware list in `life`). If the installed Starlette strips the port in a way that breaks `[::1]`, add `"::1"` and `"[::1]"` handling (or a small custom Host check in `BoothGuard`) so `Host: [::1]:<port>` passes and `Host: evil.test` still returns 400.
  - *Files:* `requirements.txt`, `src/main.py`, `src/security.py`, `tests/test_phase2.py`, `tests/test_requirements.py`
  - *Acceptance:* `test_host_origin_and_token_guard` passes locally and in CI; a new test with `server.port: 9090` accepts `Host: 127.0.0.1:9090` and `[::1]:9090`; `test_requirements.py` asserts starlette is pinned.
  - *Depends on:* none

- [x] **V3-P1-02 🔴 Only `!so` (and future explicit mod commands) can trigger a shoutout** (F-02)
  - *Do:* in `_perform_chat()` run the shoutout branch only when `target.command == "!so"`. For other allowlisted non-speak commands, either ignore them with skip reason `unhandled command`, or route them to a small command table (see V3-P4-04). Make the mod check apply to any command in `chat.mod_commands`, independent of which branch handles it.
  - *Files:* `src/orchestrator.py`, `src/policy.py`, `tests/test_shoutout.py`
  - *Acceptance:* with `chat.commands: ["!so","!pixel","!ask","!lurk"]`, a viewer's `!lurk bob` never calls `helix.shoutout`; a mod's `!so bob` still does; a viewer's `!so bob` is skipped with `not a mod`.
  - *Depends on:* none

- [x] **V3-P1-03 🟠 Fence viewer turns in conversation history** (F-03)
  - *Do:* in `ConversationMemory.llm_messages()` wrap `turn.text` with `fence_viewer_text()` when `turn.role == "viewer"` (and for `twitch_event` turns that carry user text, e.g. resub messages). Keep streamer turns unfenced. Keep assistant turns as they are.
  - *Files:* `src/memory.py`, `tests/test_memory.py`
  - *Acceptance:* a stored viewer line "ignore previous instructions" comes back from `llm_messages()` inside `<<viewer>> … <</viewer>>`; streamer turns are unchanged.
  - *Depends on:* none

- [x] **V3-P1-04 🟠 Time-box alert and recap completions** (F-04)
  - *Do:* wrap the `llm.complete()` calls in `_finish_reply()` and `make_recap()` with `asyncio.wait_for(…, settings.chat_timeout_seconds)` (or a new `ollama.event_timeout_seconds`, default 20). On timeout: skip with reason `llm timeout`, return the phase to idle, and keep the alert in the log/memory.
  - *Files:* `src/orchestrator.py`, `src/settings.py`, `config.yaml`, `tests/test_phase4.py`
  - *Acceptance:* with a fake LLM that sleeps 60 s, an alert is abandoned after the timeout and the next queued alert is handled; the recap endpoint returns the deterministic recap without a vibe paragraph.
  - *Depends on:* none

- [x] **V3-P1-05 🟠 Show the wake score while idle and feed each frame once** (F-05)
  - *Do:* broadcast `level` from `_level_loop()` whenever the mic stream is open and either listening/PTT **or** the wake model is loaded. In `MicListener._run()` keep a carry-over buffer and pass only *new* complete 1,280-sample frames to `WakeWord.evaluate()` (no overlapping re-scoring). Reset the model's internal buffer (`model.reset()` if available) after a detection.
  - *Files:* `src/orchestrator.py`, `src/audio/mic.py`, `src/audio/wake.py`, `tests/test_phase3.py`
  - *Acceptance:* a unit test with a fake model counts that each sample is fed exactly once across many chunks; a level message with a `wake` value is broadcast while `listen_enabled` is false and the wake model is present. Live: the owner check V3-P0-04 shows detections without excess false triggers.
  - *Depends on:* none

- [x] **V3-P1-06 🟠 Re-validate tokens hourly and check action scopes up front** (F-06)
  - *Do:* add a background task (in the Twitch watchdog) that calls `auth.validate()` for the event token every 60 min while connected, updating `scopes`, `expires_in` and a `validated_at` time. Validate the IRC token once on connect (scopes `chat:read`, `chat:edit`, and login matches `TWITCH_NICK`). Add `moderator:manage:shoutouts` and `channel:manage:broadcast` to a new `ACTION_SCOPES` map and report them in health (`actions: {shoutout: ok|missing scope …, marker: …}`); grey out those dashboard buttons with a tooltip when missing. Compute "expires in" as `expires_in - (now - validated_at)`.
  - *Files:* `src/twitch/auth.py`, `src/twitch/eventsub.py`, `src/twitch/chat.py`, `src/orchestrator.py`, `src/health.py`, `src/web/static/app.js`, `tests/test_eventsub_scopes.py`
  - *Acceptance:* tests with a patched `validate` show the hint appears once the computed remaining time drops below 24 h; a 401 on re-validate flips status to `token rejected`; health lists action scopes; IRC login mismatch is reported clearly.
  - *Depends on:* none

- [x] **V3-P1-07 🟠 Make session ducking usable on one output device, with an app list** (F-07)
  - *Do:* in session mode, drop the "speaker must be a different index" refusal (our own PID is already skipped). Add `audio.duck_apps` (allow-list of process names, e.g. `["Spotify.exe","chrome.exe"]`) and `audio.duck_skip` (default `["obs64.exe","Discord.exe"]`); duck only matching sessions. Update README to say "per-app session ducking".
  - *Files:* `src/audio/duck.py`, `src/settings.py`, `config.yaml`, `README.md`, `tests/` (new `test_duck.py` with a fake backend)
  - *Acceptance:* with a fake backend containing `Spotify.exe`, `obs64.exe` and our own PID, only Spotify is lowered and later restored; `speaker_device: null` no longer forces `obs-sidechain` mode on Windows.
  - *Depends on:* none

### Phase 2: Robustness and code health

- [x] **V3-P2-01 🟡 Atomic writes for booth and identity** (F-08)
  - *Do:* use `atomic_write_text()` in `save_booth()` and `save_identity()`; log (don't swallow silently) a corrupt file and keep a `.bad` copy like memory does.
  - *Files:* `src/booth.py`, `src/identity.py`, tests
  - *Acceptance:* a test that makes `os.replace` raise leaves the old file intact; a corrupt `booth.json` is logged and copied to `booth.json.bad`.
  - *Depends on:* none

- [x] **V3-P2-02 🟡 Clean up Piper temp files after interrupts** (F-10)
  - *Do:* when a persistent request is abandoned, record its `out_path` in a "pending deletes" set; delete it when its ready-line arrives or on the next synth/close. Optionally write temps into `data/tmp/` and clear that folder on start.
  - *Files:* `src/tts/piper.py`, `tests/test_piper_pipes.py`
  - *Acceptance:* a fake Piper that writes the file after the interrupt leaves no stray WAVs after the next synth.
  - *Depends on:* none

- [x] **V3-P2-03 🟡 Keep the manual mood across restarts** (F-11)
  - *Do:* store `mood_override` separately in `BoothState`; profiles set the base mood, and a manual change sets the override until the next profile switch.
  - *Files:* `src/booth.py`, `src/orchestrator.py`, `tests/test_phase4.py`
  - *Acceptance:* profile "Focus game" + manual `hype` → restart → mood is still `hype`; switching profile clears the override.
  - *Depends on:* V3-P2-01

- [x] **V3-P2-04 🟡 Better shoutout lines** (F-12)
  - *Do:* say "go check out X" style lines (configurable `chat.shoutout_line`, with `{user}`), use "welcome in" only when the target is the last raider, and let `chat.shoutout_speak` (default true) control TTS.
  - *Files:* `src/orchestrator.py`, `src/settings.py`, `config.yaml`, `tests/test_shoutout.py`
  - *Acceptance:* `!so friend` with no raid posts the configured line; a raider shoutout keeps the welcome; with `shoutout_speak: false` nothing is spoken.
  - *Depends on:* V3-P1-02

- [x] **V3-P2-05 🟡 Remove hardcoded "Pixel"** (F-13)
  - *Do:* derive the control command from the persona name (`!<name lowercase>`) and keep `!pixel` as an alias via `chat.control_command`; use `personaName` in the dashboard hint; pass the persona name into the smalltalk fallback everywhere.
  - *Files:* `src/policy.py`, `src/orchestrator.py`, `src/settings.py`, `src/web/static/app.js`, `config.yaml`, tests
  - *Acceptance:* with `name: Nova` and `control_command` unset, `!nova mute` works and `!pixel mute` still works; no user-visible "Pixel" remains in the UI when the persona is renamed.
  - *Depends on:* none

- [x] **V3-P2-06 🟡 Split the orchestrator** (F-15)
  - *Do:* move code without changing behaviour into `src/core/chat_flow.py` (pick/rate/decide/perform), `src/core/event_flow.py` (batching, priority, react), `src/core/voice_flow.py` (utterance → reply), `src/core/twitch_link.py` (connect/disconnect/watchdog) and `src/core/queue.py`. Keep `Orchestrator` as a thin façade so `main.py` and tests don't change.
  - *Files:* `src/orchestrator.py`, new `src/core/*`
  - *Acceptance:* all existing tests pass unchanged; `orchestrator.py` is under 500 lines; ruff clean.
  - *Depends on:* Phase 1 done (to avoid merge pain)

- [x] **V3-P2-07 🟡 Hermetic tests and a coverage floor** (F-16)
  - *Do:* make `conftest.py` set `COHOST_SKIP_SECRET_CHECK=1`, point `ROOT`-based paths at `tmp_path` via a fixture, and never read the real `config.yaml`/`persona.yaml`. Add `--cov=src --cov-fail-under=<current-2>` to CI.
  - *Files:* `tests/conftest.py`, `.github/workflows/ci.yml`, `pyproject.toml`
  - *Acceptance:* tests pass with a deliberately broken `config.yaml` in the repo root; CI fails if coverage drops.
  - *Depends on:* V3-P1-01

- [x] **V3-P2-08 🟡 Configurable logging** (F-17)
  - *Do:* add `logging.level` (default INFO) and `logging.debug_modules` (e.g. `["cohost.mic","cohost.tts"]`), and a dashboard "Debug log for 10 min" toggle (token-protected POST).
  - *Files:* `src/logsetup.py`, `src/settings.py`, `config.yaml`, `src/main.py`, `src/web/static/*`
  - *Acceptance:* setting `debug_modules: [cohost.mic]` produces mic debug lines only; the toggle reverts after 10 minutes.
  - *Depends on:* none

### Phase 3: Stream-day features (💡 suggestions grounded in existing code)

- [x] **V3-P3-01 💡 Hype-train meter on the overlay and dashboard** (F-14)
  - *Why:* `hype_progress` events and `hype_level` status already exist; the v2 payload also carries `progress`, `goal`, `expires_at`.
  - *Do:* extend `_hype_extra()` to keep `progress`, `goal`, `total`, `expires_at`; broadcast a `hype` message; overlay draws a bar with level and countdown (`?hype=0` hides it); dashboard shows the same in the Twitch panel.
  - *Files:* `src/twitch/eventsub.py`, `src/orchestrator.py`, `src/web/static/overlay.*`, `src/web/static/app.js`, tests
  - *Acceptance:* a fake progress event updates the bar; an end event hides it after `overlay.alert_hold_seconds`.
  - *Depends on:* owner check V3-P0-02 (hype train scope)

- [x] **V3-P3-02 💡 Stream online/offline awareness with automatic recap**
  - *Why:* EventSub `stream.online` / `stream.offline` need no scope; the recap builder already exists.
  - *Do:* subscribe to both (core, no scope); on online start a new memory "session" (rotate `session_id`, stamp `session_started`); on offline auto-write the recap (`data/recaps/YYYY-MM-DD-HHMM.md`), and optionally set the booth to idle. Scope recap content to the session (fixes F-09).
  - *Files:* `src/settings.py`, `src/twitch/auth.py`, `src/twitch/eventsub.py`, `src/orchestrator.py`, `src/recap.py`, `src/memory.py`, tests
  - *Acceptance:* fake online→events→offline writes one recap file with only that session's items, including cheers/redeems/polls; two sessions on one day make two files.
  - *Depends on:* none

- [x] **V3-P3-03 💡 Ad-break heads-up**
  - *Why:* `channel.ad_break.begin` (scope `channel:read:ads`) fits the existing feed/scope system.
  - *Do:* new optional feed `events.ad_breaks`; Pixel types (not speaks, by default) a short "ad break, back in N seconds" line and the overlay shows a countdown.
  - *Files:* `src/settings.py`, `src/twitch/auth.py`, `src/twitch/eventsub.py`, `src/orchestrator.py`, overlay, `config.yaml`, tests
  - *Acceptance:* a fake event produces one line and a countdown; missing scope shows `missing scope channel:read:ads` in the feed list.
  - *Depends on:* V3-P1-06

- [x] **V3-P3-04 💡 Real Twitch reply threads**
  - *Why:* `ChatLine.id` exists, but replies are plain `@name` messages. Threaded replies look cleaner in chat.
  - *Do:* capture the IRC `id` tag in `event_message`; when sending a viewer reply, send it as a reply (TwitchIO 2.x: raw PRIVMSG with `@reply-parent-msg-id=…`, or Helix *Send Chat Message* with `reply_parent_message_id`, which needs `user:write:chat`). Fall back to the current `@name` line.
  - *Files:* `src/twitch/chat.py`, `src/orchestrator.py`, tests
  - *Acceptance:* a fake bot records a reply-parent id on viewer replies; streamer replies and alerts stay unthreaded.
  - *Depends on:* none

- [x] **V3-P3-05 💡 Clip button next to "Remember this"**
  - *Why:* Helix *Create Clip* (scope `clips:edit`) pairs with the existing marker flow.
  - *Do:* `POST /api/clip` creates a clip, stores the edit URL as a streamer note, and shows it in the log; add a `!pixel clip` mod verb (cooldown 60 s).
  - *Files:* `src/twitch/helix.py`, `src/orchestrator.py`, `src/main.py`, `src/policy.py`, dashboard, tests
  - *Acceptance:* fake Helix returns an id and the note is saved; a missing scope gives a clear message; the mod verb respects its cooldown.
  - *Depends on:* V3-P1-06

- [x] **V3-P3-06 💡 `!ask` question queue**
  - *Why:* `!ask` is already allowlisted and the queue panel exists.
  - *Do:* `!ask …` lines go into a separate "Questions" list (not auto-answered when `chat.ask_queue: true`); the dashboard shows them with **Answer** (spoken) / **Drop**; overlay can optionally show the current question.
  - *Files:* `src/orchestrator.py`, `src/settings.py`, `src/main.py`, `src/web/static/*`, tests
  - *Acceptance:* three `!ask` lines appear in order; **Answer** speaks a reply to that viewer; stale filtering doesn't drop them.
  - *Depends on:* V3-P2-06 (optional, easier after the split)

- [x] **V3-P3-07 💡 Viewer notes you can edit**
  - *Why:* notes are auto-captured by regex (`NOTE_RE`) and shown read-only; they drive "private bookmarks" in prompts.
  - *Do:* add `POST /api/memory/note` (add/remove) and an editable notes list in the People panel; add a "pin" flag that survives retention pruning.
  - *Files:* `src/memory.py`, `src/main.py`, `src/web/static/app.js`, tests
  - *Acceptance:* adding a note shows up in the prompt block for that viewer; a pinned viewer survives `prune()`.
  - *Depends on:* none

- [x] **V3-P3-08 💡 Shoutout-received and raid-out lines**
  - *Why:* EventSub `channel.shoutout.receive` / `channel.shoutout.create` (scope `moderator:read:shoutouts`) and outgoing raids (`channel.raid` with `from_broadcaster_user_id`) slot into the existing parser (outgoing raids are currently dropped at `eventsub.py:650`).
  - *Do:* optional feed `events.shoutouts`; Pixel thanks the channel that shouted you out; on an outgoing raid, Pixel says a goodbye line.
  - *Files:* `src/settings.py`, `src/twitch/auth.py`, `src/twitch/eventsub.py`, `src/orchestrator.py`, tests
  - *Acceptance:* fake events produce one line each; outgoing raid no longer silently disappears when the feed is on.
  - *Depends on:* none

### Phase 4: Booth quality-of-life (💡 suggestions)

- [x] **V3-P4-01 💡 Stream Deck / hotkey friendly API**
  - *Why:* every booth action is already an endpoint, but the per-launch token makes external buttons impossible.
  - *Do:* optional `COHOST_CONTROL_KEY` in `.env`; when set, `BoothGuard` also accepts it in `X-Cohost-Token` for a small allow-list (`/api/interrupt`, `/api/tts/mute`, `/api/ptt`, `/api/marker`, `/api/mood`, `/api/profile`). Document curl/Stream Deck "Website/API" examples.
  - *Files:* `src/security.py`, `src/main.py`, `.env.example`, `README.md`, `tests/test_api.py`
  - *Acceptance:* with the key set, `/api/interrupt` works with it and `/api/persona` still rejects it; without the key nothing changes.
  - *Depends on:* V3-P1-01

- [x] **V3-P4-02 💡 Voice and persona per profile**
  - *Do:* allow `tts_voice`, `length_scale` and an optional `persona_file` in a profile; switching profile swaps them (voice download progress shown as now).
  - *Files:* `src/settings.py`, `src/orchestrator.py`, `config.yaml`, tests
  - *Acceptance:* switching to a profile with `tts_voice: en_US-kristin-medium` changes `health.voice`; invalid voices are rejected at load time.
  - *Depends on:* V3-P2-03

- [x] **V3-P4-03 💡 Session transcript export**
  - *Do:* append every transcript/chat/alert line to `data/sessions/<session>.jsonl` (rotated, honours retention and Forget); dashboard "Download transcript" button.
  - *Files:* `src/orchestrator.py`, `src/memory.py`, `src/main.py`, dashboard, tests
  - *Acceptance:* a session produces a JSONL file; **Forget** removes that viewer's lines from it.
  - *Depends on:* V3-P3-02

- [x] **V3-P4-04 💡 Simple custom text commands**
  - *Why:* fixes the "commands that aren't !so" gap from V3-P1-02 in a useful way.
  - *Do:* `chat.custom_commands: {"!lurk": "enjoy the lurk {user}", "!discord": "…"}` posts fixed text (no model) with per-command cooldown; commands are added to the allowlist automatically.
  - *Files:* `src/settings.py`, `src/orchestrator.py`, `src/policy.py`, `config.yaml`, tests
  - *Acceptance:* `!lurk` posts the filled text once per cooldown; it never triggers a shoutout or a model call.
  - *Depends on:* V3-P1-02

- [x] **V3-P4-05 💡 Latency panel with history**
  - *Why:* `timings` already captures STT, first token, first audio and chat send for the last reply only.
  - *Do:* keep a rolling 50-sample window and show p50/p95 per stage on the dashboard; warn when first-audio p95 > 2.5 s with a link to the VRAM tips.
  - *Files:* `src/orchestrator.py`, `src/health.py`, `src/web/static/app.js`, tests
  - *Acceptance:* health exposes `timings_p50`/`timings_p95`; the warning appears with fake slow samples.
  - *Depends on:* none

### Phase 5: Docs and packaging

- [x] **V3-P5-01 🟡 README refresh**
  - *Do:* document duck app lists (V3-P1-07), wake-word tuning, the action scopes list, IPv6 note, recap/session behaviour, custom commands, the control key, and the owner checklist. Fix "endpoint ducking" and "`!so` shouts out the last raider" (it also takes a name).
  - *Files:* `README.md`, `config.yaml` comments
  - *Acceptance:* every config key in `settings.py` is mentioned in README or `config.yaml` comments (add a test that greps for them).
  - *Depends on:* the features you chose

- [x] **V3-P5-02 🟡 Optional extras file**
  - *Do:* add `requirements-extras.txt` with pinned `pycaw` and `openwakeword`; `start.bat --extras` installs it. Health shows "installed / not installed" for each extra.
  - *Files:* `requirements-extras.txt`, `start.bat`, `src/health.py`, `README.md`, `tests/test_start_bat.py`
  - *Acceptance:* `start.bat --extras` installs both; without the flag nothing extra is installed.
  - *Depends on:* none

- [x] **V3-P5-03 🟡 Release tag and changelog**
  - *Do:* add `CHANGELOG.md` (v0.1 = PLAN-v2 work, v0.2 = this plan), bump `pyproject.toml` version, tag `v0.2.0` after Phase 1–2 merge (owner pushes the tag).
  - *Files:* `CHANGELOG.md`, `pyproject.toml`
  - *Acceptance:* changelog lists each merged PR; version matches the tag.
  - *Depends on:* Phase 1, Phase 2

---

## 7. Summary

- **Phases:** owner checklist (Phase 0) + 5 Cursor phases.
- **Tasks:** 9 owner checks + 31 Cursor tasks (Phase 1: 7, Phase 2: 8, Phase 3: 8, Phase 4: 5, Phase 5: 3) = **40 checkboxes**.
- **Do first:** V3-P0-01 (secret scanning), V3-P1-01 (the local test failure / dependency drift), V3-P1-02 (shoutout gate), V3-P1-03 (history fencing), V3-P1-04 (alert timeouts).
