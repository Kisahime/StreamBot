# Local Twitch Cohost

A free, fully local stream cohost: you talk into a mic, it talks back with Piper TTS, and it reads (and sometimes answers) Twitch chat in character. The LLM, speech-to-text, and text-to-speech stay on your PC. **Twitch is the only piece that uses the internet** (IRC chat plus optional EventSub for follows/subs/raids).

Built for an **8 GB NVIDIA GPU**. Default model is `qwen2.5:3b` so Whisper can still sit on CUDA. If you move Whisper to CPU in `config.yaml`, you can try `qwen2.5:7b-instruct-q4_K_M`.

## What you get

- Listen toggle and hold-to-talk
- Spoken replies (Piper, CPU, so VRAM stays with the LLM)
- Twitch chat read + optional replies; spicy/direct lines can be spoken too
- Persona file (`persona.yaml`) with quirks and banned topics
- Operator dashboard at `/` and an OBS overlay at `/overlay`

## Requirements

- Windows 10/11
- Python 3.11+
- NVIDIA GPU + a working `nvidia-smi`
- [Ollama](https://ollama.com) (local LLM runtime)
- A Twitch account for chat (a separate **bot** account is recommended)
- A microphone and speakers (or a virtual cable into OBS)

## Setup

1. Install Ollama, then pull the default model:

   ```bat
   ollama pull qwen2.5:3b
   ```

2. Copy environment variables:

   ```bat
   copy .env.example .env
   ```

   Edit `.env`:

   - `TWITCH_NICK` — bot account login
   - `TWITCH_CHANNEL` — the channel to join (your stream, no `#`)
   - `TWITCH_TOKEN` — OAuth token. The scopes depend on who the token belongs to. The full list is in **Twitch scopes** below.
     Create one at [Twitch Token Generator](https://twitchtokengenerator.com/) (Custom Scope) or via the [Twitch CLI](https://dev.twitch.tv/docs/cli/). Prefix with `oauth:` if it is missing.
   - Follows / subs / raids: see **IRC vs EventSub** and **Twitch scopes** below.

3. Double-click `start.bat` (creates `.venv`, installs the pinned runtime deps from `requirements.txt`, starts the app).

4. Open [http://127.0.0.1:8080](http://127.0.0.1:8080)

First launch downloads **faster-whisper `small`** and **Piper + the Amy English female voice** into `models/` (one-time, needs network). After that, inference is local.

## IRC vs EventSub

These are **not** the same login step. The dashboard **Connect Twitch** button starts both, but they use different Twitch APIs.

| | Chat (IRC) | Alerts (EventSub) |
|---|---|---|
| What it does | Read and send chat | Follows, subs, resubs, gifts, raids |
| `.env` | `TWITCH_NICK`, `TWITCH_CHANNEL`, `TWITCH_TOKEN` | `TWITCH_CLIENT_ID` (+ optional `TWITCH_EVENT_TOKEN`) |
| Token scopes | `chat:read` `chat:edit` | `moderator:read:followers` `channel:read:subscriptions` |
| Who must authorize | The account Pixel chats as (often a bot) | The **channel owner** (you), not a bot |

**If Pixel chats as your streamer account** (`TWITCH_NICK` is the same as `TWITCH_CHANNEL`):

1. Open [Twitch Token Generator](https://twitchtokengenerator.com/), choose **Custom Scope**, and tick every scope in the **Twitch scopes** table that you want this account to do. The minimum for chat plus follows and subs is:
   - `chat:read`
   - `chat:edit`
   - `moderator:read:followers`
   - `channel:read:subscriptions`
2. Authorize as **your streamer account**.
3. Put the token in `TWITCH_TOKEN` (prefix `oauth:` if it is missing).
4. Copy the page’s **Client ID** into `TWITCH_CLIENT_ID`. That ID must be from the same generate step as the token.
5. Leave `TWITCH_EVENT_TOKEN` empty. The app reuses `TWITCH_TOKEN` for alerts.

**If Pixel chats as a separate bot account:**

- `TWITCH_NICK` + `TWITCH_TOKEN` = bot, scopes `chat:read` `chat:edit` only.
- Generate a **second** token while logged into **your streamer account**, with the follower + subscription scopes, and put that in `TWITCH_EVENT_TOKEN`.
- `TWITCH_CLIENT_ID` must match the app that issued **that second token**.

Chat still works if EventSub fails (wrong Client ID, missing scopes, bot token used for alerts). On the dashboard, **Twitch** = IRC, **EventSub** = alerts. The Systems panel lists each feed (`ok`, `off`, or `missing scope …`). A missing scope skips that feed only.

`!so` is for mods and the broadcaster. Viewers are ignored. A second shoutout inside 120 seconds is skipped locally.

`!pixel mute`, `!pixel unmute`, `!pixel chill`, `!pixel hype`, and `!pixel stop` are also mod and broadcaster only. A second one inside 4 seconds is skipped. `!pixel` with anything else, and `!ask`, are spoken for anyone in chat.

## Twitch scopes

Tick these on [Twitch Token Generator](https://twitchtokengenerator.com/) (Custom Scope) when you create the token. Put chat scopes on the account Pixel types as. Put EventSub and Helix scopes on the **channel owner**. One token can carry all of them when Pixel chats as you.

| Feature | Scope | Where it goes |
|---|---|---|
| Read chat | `chat:read` | `TWITCH_TOKEN` |
| Send chat | `chat:edit` | `TWITCH_TOKEN` |
| Follows | `moderator:read:followers` | event token (`TWITCH_TOKEN` or `TWITCH_EVENT_TOKEN`) |
| Subs, resubs, gifts | `channel:read:subscriptions` | event token |
| Bits | `bits:read` | event token |
| Channel points | `channel:read:redemptions` or `channel:manage:redemptions` | event token |
| Hype train | `channel:read:hype_train` | event token |
| Polls | `channel:read:polls` or `channel:manage:polls` | event token |
| Predictions | `channel:read:predictions` or `channel:manage:predictions` | event token |
| `!so` / shoutout button | `moderator:manage:shoutouts` | event token |
| Shoutout received and sent | `moderator:read:shoutouts` | event token |
| Ad breaks | `channel:read:ads` | event token |
| Remember this (stream marker) | `channel:manage:broadcast` | event token |
| Clip button | `clips:edit` | event token |

Copy the page’s Client ID into `TWITCH_CLIENT_ID`. It has to be the app that issued the event token.

These optional feeds are **off** in the shipped `config.yaml`: bits, channel points, hype train, polls, and predictions. Turn one on only when the event token includes its scope. Hype train uses EventSub version 2 (`channel.hype_train.*`); version 1 was withdrawn. Follows, subs, resubs, gifts, and raids are the feeds that stay available when `events.enabled` is true.

## On stream

1. Keep Ollama running.
2. In the dashboard, **Connect Twitch**.
3. **Start listening** or hold **Hold to talk** (Space also works as push-to-talk on the dashboard).
4. In OBS, add a **Browser Source**:
   - URL: `http://127.0.0.1:8080/overlay`
   - Width ~1280, height ~300
   - Enable **Shutdown source when not visible** off while live
   - Enable **Transparent background** (Custom CSS can be `body { background-color: rgba(0,0,0,0); margin: 0; }`)
   - Optional query: `?theme=minimal&position=bottom&hide=idle&hold=6&alerts=1&fontsize=32` (`position` can be `top` or `center`)
   - `?alerts=0` hides alert banners. `?fontsize=32` sets the caption size in pixels.
   - Each alert kind (raid, follow, sub, and the rest) has its own color. The banner fades in, and the caption types out while Pixel is speaking. Both animations turn off when the browser asks for reduced motion.

Pixel's spoken audio can go to a virtual cable (VB-Audio, VoiceMeeter). The device list labels those outputs; pick one in the booth. The default output is not changed until you pick it.

Music ducking is off. Preferred setup: music on its own OBS track, sidechained to the cohost cable. `audio.duck_enabled: true` is per-app session ducking: on Windows, pycaw lowers other apps' session volume while Pixel talks. This is other apps' session volume, not Windows endpoint (device) volume. `speaker_device` may stay null. Pixel's own process is never ducked. `audio.duck_skip` (default `obs64.exe` and `Discord.exe`) is also left alone. An empty `audio.duck_apps` list ducks every other app; a non-empty list ducks only those process names. The pre-duck levels are stored in `data/duck.json` and restored on the next launch, and again if the process exits while ducked. Without pycaw the booth stays on the OBS sidechain and does not change the volume. Health reports `extras.pycaw` as `installed` or `not installed`.

Edit `persona.yaml` anytime, then click **Reload persona**. No restart needed.

Set your **display name** and **Twitch login** in the booth so Pixel knows mic audio and your chat are you, not a viewer.

## 8 GB VRAM tips

`vram.profile` in `config.yaml` is the switch:

| Profile | Whisper | LLM |
|---|---|---|
| `snappy` (default) | CUDA when available, else CPU | `qwen2.5:3b` |
| `quality` | forced to CPU | `qwen2.5:7b-instruct-q4_K_M` if you were still on the 3B default |

The booth warns when GPU memory used crosses `vram.warn_mb` (7000). On the snappy profile that warning means: set `vram.profile: quality` or `whisper.device: cpu`.

Default split:

| Piece | Where | Notes |
|---|---|---|
| Ollama `qwen2.5:3b` | GPU | Fast enough to speak in near-real time |
| Whisper `small` | CUDA if available | Set `whisper.device: cpu` if you OOM |
| Piper TTS | CPU | On purpose |

If the PC hitching:

- Set `whisper.device: cpu` and optionally `whisper.model_size: base`
- Keep the 3B model; 7B Q4 plus Whisper CUDA will often exceed 8 GB
- Raise `audio.energy_threshold` if it transcribes room noise
- Increase `chat.cooldown_seconds` if it talks too much

Optional 7B (Whisper on CPU):

```yaml
ollama:
  model: qwen2.5:7b-instruct-q4_K_M
whisper:
  device: cpu
```

Then: `ollama pull qwen2.5:7b-instruct-q4_K_M`

## Dependencies

`requirements.txt` is the pinned runtime install (`fastapi==…`, not `>=`). `start.bat` installs that file and nothing else. `pytest` and `ruff` are not in it.

`requirements-dev.txt` is those same pins plus `pytest`, `pytest-cov`, and `ruff`:

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests
```

GitHub Actions runs that lint and the test suite on `windows-latest` with Python 3.11. The tests do not need Twitch credentials, a GPU, models, or an audio device.

Logs go to `logs/cohost.log` (rotating, about 2 MB, three backups). That directory is gitignored.

`requirements-extras.txt` pins `pycaw` and `openwakeword`. `start.bat` does not install it unless you pass `--extras`. Without that flag, nothing from the extras file is installed. `--extras` and `--no-browser` can be used together, in either order.

```bat
start.bat --extras
```

- `pycaw` — per-app session ducking when `audio.duck_enabled: true` (Windows)
- `openwakeword` — local wake word when `audio.wake_word: true`

`/api/health` reports each extra as `installed` or `not installed`.

To bump a pin: upgrade that package in the project venv (`python -m pip install --upgrade some-package`), run `python -m pip freeze`, and copy the new `name==version` into `requirements.txt` if the app imports it, or into `requirements-dev.txt` if it is only `pytest` or `ruff`. Leave `pytest` and `ruff` out of `requirements.txt`. Run the tests before you commit. Do not commit `.env`.

## Config

- [`config.yaml`](config.yaml) — models, devices, cooldowns
- [`persona.yaml`](persona.yaml) — name, tone, quirks
- [`.env`](.env) — Twitch secrets (never commit)

`audio.mic_device` / `audio.speaker_device` can be integer device indexes from the dashboard health payload (`/api/health` → `devices`) or left `null` for Windows defaults.

## How hybrid chat works

- Your mic always wins: a new streamer utterance interrupts pending chat speech.
- Link-only lines and the bot's own messages are ignored. IRC keeps `chat.commands`, the control command, and every `chat.custom_commands` name. Other `!` lines are dropped.
- Mentions, greetings, and questions are spoken. Other chat is typed only.
- Replies to a specific viewer are posted as `@username …`. TTS speaks the name without the @, and strips links and emotes.
- Conversation memory is per person: the streamer (mic + their Twitch login) vs each chatter. Set **Display name** and **Twitch login** on the dashboard. Stored in `data/streamer.json`. **Clear memory** resets threads.
- Follows, new subs, resubs, gifted subs, and incoming raids can be announced (EventSub). Follows are batched so a follow train is one line. Gift bombs are one thank-you, not twenty. Alerts wait if you are talking on mic.
- Other chat is sampled on an interval with a global cooldown plus a per-user cooldown.
- Twitch IRC auto-reconnects if the socket drops. EventSub reconnects separately; chat still works if EventSub is missing scopes.
- Mentions, greetings, questions, and `!pixel` / `!ask` are spoken. Ambient chat is typed only. `!so some_login` shouts that channel out. `!so` with no name shouts out the last raider. A `chat.custom_commands` entry posts its fixed text and does not call the model. Chat vs speak is decided in code, not by asking the model for JSON.

Voice replies can be echoed to chat (`chat.post_voice_replies_to_chat`). Edit `persona.yaml` for Pixel's voice; **Reload persona** picks it up without a restart.

## Troubleshooting

- **Ollama down** on the dashboard: start Ollama from the system tray, then `ollama pull qwen2.5:3b`.
- **Twitch connect error**: token scopes, nick mismatch, or `.env` not saved in the project root.
- **EventSub error / no alerts**: add `TWITCH_CLIENT_ID`. A bot chat token cannot subscribe to the streamer's subs — use `TWITCH_EVENT_TOKEN` from the broadcaster with `moderator:read:followers` and `channel:read:subscriptions`. Chat still works without this.
- **No mic**: another app has exclusive WASAPI access; close it or pick another input index.
- **Whisper CUDA failed** / **`cublas64_12.dll` not found**: the NVIDIA *driver* is not the CUDA 12 math libraries. Re-run `start.bat` so it installs `nvidia-cublas-cu12` (and friends) into the venv. If a brand-new GPU (RTX 50-series) still errors, the app will retry Whisper on CPU; you can also set `whisper.device: cpu` in `config.yaml`.
- **Piper missing**: delete `models/piper` and restart so it re-downloads.

## What the shipped config turns on

These match `config.yaml` as it is in the repo:

| Key | Shipped value |
|---|---|
| `events.enabled`, `events.speak`, `events.post_to_chat` | true |
| `chat.post_voice_replies_to_chat` | true |
| `chat.welcome_first_time` | true |
| `audio.barge_in`, `audio.echo_gate` | true |
| `events.bits`, `events.channel_points`, `events.hype_train`, `events.polls`, `events.predictions` | false |
| `audio.wake_word`, `audio.duck_enabled` | false |
| `safety.allow_links` | false |

Wake word needs `openwakeword` and never replaces hold-to-talk. Channel-point reward titles map to `speak`, `chat`, or `ignore`. Links are stripped from Pixel's chat posts unless `safety.allow_links` is true.

## Profiles

`config.yaml` has two booth presets. **Just chatting** is hype, a short cooldown, events spoken, voice replies posted, 12 replies a minute. **Focus game** is chill, a long cooldown, events typed, voice replies not posted, 3 replies a minute. A profile may also set `tts_voice`, `length_scale`, and `persona_file`. The dashboard dropdown calls `POST /api/profile` and stores the name in `data/booth.json`. Switching voice reuses the usual download progress line. An unknown `tts_voice` is rejected when the config loads.

## Wake word

`audio.wake_word` stays false until you install the extras (`start.bat --extras`) and set it true. `audio.wake_models` lists openwakeword names; the shipped model is `hey_jarvis`. `audio.wake_threshold` is a score from 0 to 1 (shipped `0.5`). Raise it if normal speech false-triggers, and lower it if a clear "hey jarvis" is missed. Wake arms one utterance. Hold-to-talk and **Start listening** still work. The booth broadcasts the wake score when the model is loaded, including while listen is off. Detections are also written to `logs/cohost.log`.

## Custom commands

`chat.custom_commands` maps a command to fixed text, for example `"!lurk": "enjoy the lurk {user}"`. `{user}` is the chatter. Pixel posts that text, waits `chat.custom_command_cooldown_seconds` before the same command can fire again, and does not call the model or shout anyone out. The command is added to the IRC allowlist. The shipped map is empty.

## Sessions and recaps

Going live (`stream.online`) rotates the memory session id. Going offline writes `data/recaps/YYYY-MM-DD-HHMM.md` for the session that just ended. **Build recap** writes `data/recaps/YYYY-MM-DD.md` from the current session. **Add vibe** asks the model for one paragraph, bounded by `ollama.chat_timeout_seconds`. Every remembered chat, transcript, and alert line is also appended to `data/sessions/<session>.jsonl`. **Download transcript** saves that file. **Forget** removes that viewer from memory and from the session logs. `memory.retention_days` drops old memory and old session files.

## Owner checklist

These still need your machine, your Twitch login, and a live or test stream. They are not covered by the automated suite.

1. Turn on GitHub secret scanning and push protection. `git log --all -- .env` should be empty.
2. Connect alerts and confirm each feed you use says `ok`. Chat needs `chat:read` and `chat:edit`. Alerts need the scopes in the table above.
3. With speakers, talk over Pixel. She should stop. She should not stop from speaker bleed alone. Adjust `audio.echo_gate_ratio` if she does.
4. With the wake extras installed, say "hey jarvis" and then talk normally. Count hits and false triggers in the wake score and `logs/cohost.log`.
5. Install `pycaw`, play music in another app, and enable **Duck output** with a speaker device only if you want one. Music should dip and return. Kill the booth mid-sentence and relaunch: volume comes back from `data/duck.json`.
6. Add the overlay in OBS (`?hide=idle&position=bottom`, plus `?alerts=0`, `?theme=`, and `?fontsize=`). Check captions, the nameplate, and an alert flash.
7. Turn on channel points, polls, and predictions one at a time. `hydrate` should speak. An unmapped reward should skip as `reward ignored`. A poll and a prediction should get one line at the start and one at the end. A first-time chatter should get a typed welcome.
8. Shout out the last raider, and press **Remember this** while live. A missing scope should show on the button.

## Security

The booth binds to localhost. `server.allow_remote` stays false unless you set it on purpose. Writes need `Content-Type: application/json` and the `X-Cohost-Token` from `GET /api/session` (same browser tab). A foreign `Origin` is rejected. The OBS overlay does not get that token and cannot change anything.

Optional `COHOST_CONTROL_KEY` in `.env` is a stable token for a Stream Deck or hotkey. When it is set, that value is accepted as `X-Cohost-Token` only on `POST /api/interrupt`, `/api/tts/mute`, `/api/ptt`, `/api/marker`, `/api/mood`, and `/api/profile`. `/api/persona` and every other write still need the per-launch session token. Leave the key empty and nothing changes.

```powershell
curl.exe -X POST http://127.0.0.1:8080/api/interrupt -H "Content-Type: application/json" -H "X-Cohost-Token: YOUR_CONTROL_KEY" -d "{}"
curl.exe -X POST http://127.0.0.1:8080/api/tts/mute -H "Content-Type: application/json" -H "X-Cohost-Token: YOUR_CONTROL_KEY" -d "{\"mute\":true}"
curl.exe -X POST http://127.0.0.1:8080/api/ptt -H "Content-Type: application/json" -H "X-Cohost-Token: YOUR_CONTROL_KEY" -d "{\"down\":true}"
curl.exe -X POST http://127.0.0.1:8080/api/marker -H "Content-Type: application/json" -H "X-Cohost-Token: YOUR_CONTROL_KEY" -d "{}"
curl.exe -X POST http://127.0.0.1:8080/api/mood -H "Content-Type: application/json" -H "X-Cohost-Token: YOUR_CONTROL_KEY" -d "{\"mood\":\"hype\"}"
curl.exe -X POST http://127.0.0.1:8080/api/profile -H "Content-Type: application/json" -H "X-Cohost-Token: YOUR_CONTROL_KEY" -d "{\"name\":\"Focus game\"}"
```

On a Stream Deck Website button, set the URL to `http://127.0.0.1:8080/api/interrupt`, method POST, header `Content-Type: application/json`, header `X-Cohost-Token` to the control key, and body `{}`.

## Backup

`scripts/backup.ps1` zips `data/`, `persona.yaml`, and `config.yaml` into `backups/YYYYMMDD-HHMM.zip` and keeps the last 10. It never puts `.env` in the zip. `backups/` is gitignored.

```powershell
powershell -File scripts\backup.ps1
```

`start.bat` prefers `py -3.11`, then `python`, and requires 3.11 or newer. It reads `server.host` and `server.port`, skips pip when `requirements.txt` has not changed, and opens the booth unless you pass `--no-browser`. Pass `--extras` to also install `requirements-extras.txt`. If creating the venv, installing dependencies, or reading the config fails, the window stays open and the next launch tries the install again. The install stamp is written only after pip succeeds. Binding anything other than localhost requires `server.allow_remote: true`.

The booth only accepts `Host` values of `127.0.0.1`, `localhost`, `::1`, and `[::1]`, including the configured port. Starlette 1.6 split `Host` on the first colon, so `[::1]:8080` was rejected; the allow list keeps both the bare IPv6 names and the same names with the port. A page on another site cannot POST into it: writes need `Content-Type: application/json` and the `X-Cohost-Token` from `GET /api/session`, and a foreign `Origin` is rejected. The OBS overlay stays read-only. Each person in the memory panel has **Forget**, which deletes that viewer from `data/memory.json`. Viewer lines are kept for `memory.retention_days` (14). Piper and the three listed voices are checksummed; a partial download stays as a `.part` file until it finishes.

## Config reference

Every key `settings.py` reads. Shipped values live in `config.yaml`.

| Key | What it does |
|---|---|
| `server.host` | Bind address. Stay on localhost. |
| `server.port` | Booth port. |
| `server.allow_remote` | Must be true before a non-loopback bind is allowed. |
| `ollama.host` | Local Ollama URL. |
| `ollama.model` | Chat and voice model. |
| `ollama.timeout_seconds` | Overall Ollama request timeout. |
| `ollama.num_ctx` | Context length sent to Ollama. |
| `ollama.num_predict_chat` | Max tokens for a chat reply. |
| `ollama.num_predict_voice` | Max tokens for a spoken reply. |
| `ollama.keep_alive` | How long Ollama keeps the model loaded. |
| `ollama.chat_timeout_seconds` | Bound for chat replies, alert replies, and the recap vibe. |
| `whisper.model_size` | faster-whisper size. |
| `whisper.device` | `auto`, `cuda`, or `cpu`. |
| `whisper.language` | Transcription language. |
| `whisper.compute_type_cuda` | Compute type when Whisper is on CUDA. |
| `whisper.compute_type_cpu` | Compute type when Whisper is on CPU. |
| `vram.profile` | `snappy` or `quality`. |
| `vram.warn_mb` | GPU memory used before the booth warns. |
| `vram.quality_model` | Model used when quality mode replaces the 3B default. |
| `tts.enabled` | Piper on or off. |
| `tts.mute` | Start muted. |
| `tts.voice` | Piper voice id. |
| `tts.length_scale` | Piper speaking rate. |
| `tts.piper_dir` | Where Piper and the voices are stored. |
| `tts.max_sentence_chars` | Longest sentence sent to Piper at once. |
| `tts.voices` | Voices the booth will download and offer. |
| `audio.sample_rate` | Mic sample rate. |
| `audio.mic_device` | Input index, or null for the default. |
| `audio.speaker_device` | Output index, or null for the default. |
| `audio.mic_device_name` | Input name, re-resolved if the index moves. |
| `audio.speaker_device_name` | Output name, re-resolved if the index moves. |
| `audio.silence_ms` | Trailing silence that ends an utterance. |
| `audio.min_utterance_ms` | Shortest utterance that is kept. |
| `audio.energy_threshold` | RMS level that counts as speech. |
| `audio.max_utterance_seconds` | Longest utterance before it is cut. |
| `audio.duck_enabled` | Per-app session ducking. Off unless you opt in. |
| `audio.duck_gain` | Volume multiplier while ducked. |
| `audio.duck_apps` | Allow-list of process names. Empty ducks every other app. |
| `audio.duck_skip` | Process names never ducked. |
| `audio.barge_in` | Talking over Pixel stops her. |
| `audio.echo_gate` | Ignore speaker bleed unless you are louder or holding PTT. |
| `audio.echo_gate_ratio` | How much louder than the bleed counts as you. |
| `audio.wake_word` | Local wake word. Off by default. |
| `audio.wake_models` | openwakeword model names. |
| `audio.wake_threshold` | Score from 0 to 1 that counts as a wake. |
| `chat.buffer_size` | Recent chat lines kept for context. |
| `chat.cooldown_seconds` | Global pause between chat replies. |
| `chat.max_replies_per_minute` | Cap on chat replies. |
| `chat.scan_interval_seconds` | How often pending chat is reconsidered. |
| `chat.mention_boost` | Mentions use a shorter cooldown. |
| `chat.post_voice_replies_to_chat` | Echo spoken replies into chat. |
| `chat.chat_max_chars` | Longest chat post. |
| `chat.per_user_cooldown_seconds` | Pause before answering the same viewer again. |
| `chat.auto_reconnect` | Reconnect IRC after a drop. |
| `chat.min_send_gap_seconds` | Minimum gap between outgoing chat lines. |
| `chat.commands` | IRC commands that are not dropped. |
| `chat.mod_commands` | Commands only mods and the broadcaster may run. |
| `chat.custom_commands` | Command to fixed text. No model. |
| `chat.custom_command_cooldown_seconds` | Per-command pause for those fixed replies. |
| `chat.shoutout_cooldown_seconds` | Local pause between shoutouts. |
| `chat.shoutout_line` | Chat text for `!so`. `{user}` is the login. |
| `chat.shoutout_speak` | Also speak the shoutout line. |
| `chat.control_command` | Extra booth command. Empty uses the persona's first name. `!pixel` stays. |
| `chat.max_age_seconds` | Drop stale viewer lines. Streamer lines and commands stay. |
| `chat.welcome_first_time` | Type a welcome for a first-time chatter. |
| `chat.control_cooldown_seconds` | Pause between mute, mood, and stop commands. |
| `chat.ask_queue` | Hold `!ask` in the Questions list. |
| `chat.clip_cooldown_seconds` | Pause between clip commands. |
| `chat.ambient_context_lines` | Recent lines shown to the model for ambient chat. |
| `safety.allow_links` | Keep links in Pixel's chat posts. |
| `memory.chat_context_lines` | Chat lines recalled into a prompt. |
| `memory.events` | Event rows kept in memory. |
| `memory.viewers` | Viewer rows kept in memory. |
| `memory.thread_turns` | Turns kept per thread. |
| `memory.catchphrase_seconds` | How long before a catchphrase may repeat. |
| `memory.retention_days` | How long viewer lines and session files are kept. |
| `events.enabled` | EventSub alerts on or off. |
| `events.announce_follows` | Speak or type follows. |
| `events.follow_batch_seconds` | Window that collapses a follow train into one line. |
| `events.speak` | Speak alerts. False types them. |
| `events.post_to_chat` | Also post alerts in chat. |
| `events.bits` | Cheer feed. Off until you opt in. |
| `events.channel_points` | Redemption feed. Off until you opt in. |
| `events.hype_train` | Hype train feed. Off until you opt in. |
| `events.polls` | Poll feed. Off until you opt in. |
| `events.predictions` | Prediction feed. Off until you opt in. |
| `events.ad_breaks` | Ad break feed. Off until you opt in. |
| `events.shoutouts` | Shoutout and raid-out lines. Off until you opt in. |
| `events.idle_on_offline` | Stop listening when the stream goes offline. |
| `events.channel_point_rewards` | Reward title to `speak`, `chat`, or `ignore`. |
| `profiles` | Named booth presets. |
| `overlay.alert_hold_seconds` | How long an overlay alert stays up. |
| `logging.level` | Root log level. |
| `logging.debug_modules` | Logger names that stay at DEBUG. |

## Out of scope

No avatar/lip-sync, no cloud models, no auto timeouts or bans. Overlay is captions, a speaking nameplate, and an alert flash.
