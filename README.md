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
   - `TWITCH_TOKEN` — OAuth token with `chat:read` and `chat:edit`  
     Create one at [Twitch Token Generator](https://twitchtokengenerator.com/) (custom scope: those two) or via the [Twitch CLI](https://dev.twitch.tv/docs/cli/). Prefix with `oauth:` if it is missing.
   - Follows / subs / raids: see **IRC vs EventSub** below.

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

1. Open [Twitch Token Generator](https://twitchtokengenerator.com/), choose **Custom Scope**, tick all four:
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

These optional feeds are **off** in `config.yaml`. Turn one on only when the event token includes its scope. Hype train uses EventSub version 2 (`channel.hype_train.*`); version 1 was withdrawn.

| Feed | Scope |
|---|---|
| Bits | `bits:read` |
| Channel points | `channel:read:redemptions` or `channel:manage:redemptions` |
| Hype train | `channel:read:hype_train` |
| Polls | `channel:read:polls` or `channel:manage:polls` |
| Predictions | `channel:read:predictions` or `channel:manage:predictions` |

## On stream

1. Keep Ollama running.
2. In the dashboard, **Connect Twitch**.
3. **Start listening** or hold **Hold to talk** (Space also works as push-to-talk on the dashboard).
4. In OBS, add a **Browser Source**:
   - URL: `http://127.0.0.1:8080/overlay`
   - Width ~1280, height ~300
   - Enable **Shutdown source when not visible** off while live
   - Enable **Transparent background** (Custom CSS can be `body { background-color: rgba(0,0,0,0); margin: 0; }`)
   - Optional query: `?theme=minimal&position=bottom&hide=idle&hold=6` (`position` can be `top` or `center`)

Pixel's spoken audio can go to a virtual cable (VB-Audio, VoiceMeeter). The device list labels those outputs; pick one in the booth. The default output is not changed until you pick it.

Music ducking is off. Preferred setup: music on its own OBS track, sidechained to the cohost cable. `audio.duck_enabled: true` lowers other apps' per-session volume (pycaw) while Pixel talks, and only when `speaker_device` is a different device than the default output. With `speaker_device: null` the booth shows a note and does not change the volume. The pre-duck levels are stored in `data/duck.json` and restored on the next launch, and again if the process exits while ducked.

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

`requirements-dev.txt` is those same pins plus `pytest` and `ruff`:

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests
```

Optional extras are not installed by either file. Add them yourself when you turn the feature on:

- `pycaw` — Windows endpoint ducking when `audio.duck_enabled: true`
- `openwakeword` — local wake word when `audio.wake_word: true`

To bump a pin: upgrade that package in the project venv (`python -m pip install --upgrade some-package`), run `python -m pip freeze`, and copy the new `name==version` into `requirements.txt` if the app imports it, or into `requirements-dev.txt` if it is only `pytest` or `ruff`. Leave `pytest` and `ruff` out of `requirements.txt`. Run the tests before you commit. Do not commit `.env`.

## Config

- [`config.yaml`](config.yaml) — models, devices, cooldowns
- [`persona.yaml`](persona.yaml) — name, tone, quirks
- [`.env`](.env) — Twitch secrets (never commit)

`audio.mic_device` / `audio.speaker_device` can be integer device indexes from the dashboard health payload (`/api/health` → `devices`) or left `null` for Windows defaults.

## How hybrid chat works

- Your mic always wins: a new streamer utterance interrupts pending chat speech.
- Link-only lines and the bot's own messages are ignored. The only commands that get through are `!so`, `!pixel`, and `!ask`.
- Mentions, greetings, and questions are spoken. Other chat is typed only.
- Replies to a specific viewer are posted as `@username …`. TTS speaks the name without the @, and strips links and emotes.
- Conversation memory is per person: the streamer (mic + their Twitch login) vs each chatter. Set **Display name** and **Twitch login** on the dashboard. Stored in `data/streamer.json`. **Clear memory** resets threads.
- Follows, new subs, resubs, gifted subs, and incoming raids can be announced (EventSub). Follows are batched so a follow train is one line. Gift bombs are one thank-you, not twenty. Alerts wait if you are talking on mic.
- Other chat is sampled on an interval with a global cooldown plus a per-user cooldown.
- Twitch IRC auto-reconnects if the socket drops. EventSub reconnects separately; chat still works if EventSub is missing scopes.
- Mentions, greetings, questions, and `!pixel` / `!ask` are spoken. Ambient chat is typed only. `!so` shouts out the last raider. Other `!` commands stay ignored. Chat vs speak is decided in code, not by asking the model for JSON.

Voice replies can be echoed to chat (`chat.post_voice_replies_to_chat`). Edit `persona.yaml` for Pixel's voice; **Reload persona** picks it up without a restart.

## Troubleshooting

- **Ollama down** on the dashboard: start Ollama from the system tray, then `ollama pull qwen2.5:3b`.
- **Twitch connect error**: token scopes, nick mismatch, or `.env` not saved in the project root.
- **EventSub error / no alerts**: add `TWITCH_CLIENT_ID`. A bot chat token cannot subscribe to the streamer's subs — use `TWITCH_EVENT_TOKEN` from the broadcaster with `moderator:read:followers` and `channel:read:subscriptions`. Chat still works without this.
- **No mic**: another app has exclusive WASAPI access; close it or pick another input index.
- **Whisper CUDA failed** / **`cublas64_12.dll` not found**: the NVIDIA *driver* is not the CUDA 12 math libraries. Re-run `start.bat` so it installs `nvidia-cublas-cu12` (and friends) into the venv. If a brand-new GPU (RTX 50-series) still errors, the app will retry Whisper on CPU; you can also set `whisper.device: cpu` in `config.yaml`.
- **Piper missing**: delete `models/piper` and restart so it re-downloads.

## Optional, off by default

In `config.yaml`: `events.bits`, `events.channel_points`, `events.hype_train`, `events.polls`, `events.predictions`, `audio.wake_word`, `audio.duck_enabled`, `safety.allow_links`. Wake word needs `openwakeword` and never replaces hold-to-talk. Channel-point reward titles map to `speak`, `chat`, or `ignore`. Links are stripped from Pixel's chat posts unless `safety.allow_links` is true.

`start.bat` prefers `py -3.11`, then `python`, and requires 3.11 or newer. It reads `server.host` and `server.port`, skips pip when `requirements.txt` has not changed, and opens the booth unless you pass `--no-browser`. If creating the venv, installing dependencies, or reading the config fails, the window stays open and the next launch tries the install again. The install stamp is written only after pip succeeds. Binding anything other than localhost requires `server.allow_remote: true`.

The booth only accepts `Host` values of `127.0.0.1`, `localhost`, and `[::1]`. A page on another site cannot POST into it: writes need `Content-Type: application/json` and the `X-Cohost-Token` from `GET /api/session`, and a foreign `Origin` is rejected. The OBS overlay stays read-only. Each person in the memory panel has **Forget**, which deletes that viewer from `data/memory.json`. Viewer lines are kept for `memory.retention_days` (14). Piper and the three listed voices are checksummed; a partial download stays as a `.part` file until it finishes.

## Out of scope

No avatar/lip-sync, no cloud models, no auto timeouts or bans. Overlay is captions, a speaking nameplate, and an alert flash.
