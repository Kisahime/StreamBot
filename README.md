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

3. Double-click `start.bat` (creates `.venv`, installs deps, starts the app).

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

Chat still works if EventSub fails (wrong Client ID, missing scopes, bot token used for alerts). On the dashboard, **Twitch** = IRC, **EventSub** = alerts.

## On stream

1. Keep Ollama running.
2. In the dashboard, **Connect Twitch**.
3. **Start listening** or hold **Hold to talk** (Space also works as push-to-talk on the dashboard).
4. In OBS, add a **Browser Source**:
   - URL: `http://127.0.0.1:8080/overlay`
   - Width ~1280, height ~300
   - Enable **Shutdown source when not visible** off while live
   - Enable **Transparent background** (Custom CSS can be `body { background-color: rgba(0,0,0,0); margin: 0; }`)

Edit `persona.yaml` anytime, then click **Reload persona**. No restart needed.

Set your **display name** and **Twitch login** in the booth so Pixel knows mic audio and your chat are you, not a viewer.

## 8 GB VRAM tips

Default `config.yaml` split:

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

## Config

- [`config.yaml`](config.yaml) — models, devices, cooldowns
- [`persona.yaml`](persona.yaml) — name, tone, quirks
- [`.env`](.env) — Twitch secrets (never commit)

`audio.mic_device` / `audio.speaker_device` can be integer device indexes from the dashboard health payload (`/api/health` → `devices`) or left `null` for Windows defaults.

## How hybrid chat works

- Your mic always wins: a new streamer utterance interrupts pending chat speech.
- Chat lines that are commands (`!foo`), link-only, or from the bot itself are ignored.
- Mentions, greetings, and questions are answered promptly (and often spoken).
- Replies to a specific viewer are posted as `@username …`. TTS speaks the name without the @.
- Conversation memory is per person: the streamer (mic + their Twitch login) vs each chatter. Set **Display name** and **Twitch login** on the dashboard. Stored in `data/streamer.json`. **Clear memory** resets threads.
- Follows, new subs, resubs, gifted subs, and incoming raids can be announced (EventSub). Follows are batched so a follow train is one line. Gift bombs are one thank-you, not twenty. Alerts wait if you are talking on mic.
- Other chat is sampled on an interval with a global cooldown plus a per-user cooldown.
- Twitch IRC auto-reconnects if the socket drops. EventSub reconnects separately; chat still works if EventSub is missing scopes.
- Mentions, greetings, and questions are answered promptly (and often spoken). Chat vs speak is decided in code, not by asking the model for JSON.

Voice replies can be echoed to chat (`chat.post_voice_replies_to_chat`). Edit `persona.yaml` for Pixel's voice; **Reload persona** picks it up without a restart.

## Troubleshooting

- **Ollama down** on the dashboard: start Ollama from the system tray, then `ollama pull qwen2.5:3b`.
- **Twitch connect error**: token scopes, nick mismatch, or `.env` not saved in the project root.
- **EventSub error / no alerts**: add `TWITCH_CLIENT_ID`. A bot chat token cannot subscribe to the streamer's subs — use `TWITCH_EVENT_TOKEN` from the broadcaster with `moderator:read:followers` and `channel:read:subscriptions`. Chat still works without this.
- **No mic**: another app has exclusive WASAPI access; close it or pick another input index.
- **Whisper CUDA failed** / **`cublas64_12.dll` not found**: the NVIDIA *driver* is not the CUDA 12 math libraries. Re-run `start.bat` so it installs `nvidia-cublas-cu12` (and friends) into the venv. If a brand-new GPU (RTX 50-series) still errors, the app will retry Whisper on CPU; you can also set `whisper.device: cpu` in `config.yaml`.
- **Piper missing**: delete `models/piper` and restart so it re-downloads.

## Out of scope (for now)

No avatar/lip-sync, no wake word, no bits/channel-points/hype-train, no auto music ducking. Overlay is captions, status, and a short alert flash.
