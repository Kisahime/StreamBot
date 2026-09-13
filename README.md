# Local Twitch Cohost

A free, fully local stream cohost: you talk into a mic, it talks back with Piper TTS, and it reads (and sometimes answers) Twitch chat in character. The LLM, speech-to-text, and text-to-speech stay on your PC. **Twitch chat is the only piece that uses the internet** (Twitch IRC).

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

3. Double-click `start.bat` (creates `.venv`, installs deps, starts the app).

4. Open [http://127.0.0.1:8080](http://127.0.0.1:8080)

First launch downloads **faster-whisper `small`** and **Piper + the Amy English female voice** into `models/` (one-time, needs network). After that, inference is local.

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
- Conversation memory (who said what, what Pixel answered) is kept in `data/memory.json` and shown on the dashboard. **Clear memory** resets it.
- Other chat is sampled on an interval with a global cooldown plus a per-user cooldown.
- Twitch IRC auto-reconnects if the socket drops.
- The model returns `ignore`, `chat`, or `speak`. `speak` also posts the line in Twitch chat.

Voice replies can be echoed to chat (`chat.post_voice_replies_to_chat`).

## Troubleshooting

- **Ollama down** on the dashboard: start Ollama from the system tray, then `ollama pull qwen2.5:3b`.
- **Twitch connect error**: token scopes, nick mismatch, or `.env` not saved in the project root.
- **No mic**: another app has exclusive WASAPI access; close it or pick another input index.
- **Whisper CUDA failed** / **`cublas64_12.dll` not found**: the NVIDIA *driver* is not the CUDA 12 math libraries. Re-run `start.bat` so it installs `nvidia-cublas-cu12` (and friends) into the venv. If a brand-new GPU (RTX 50-series) still errors, the app will retry Whisper on CPU; you can also set `whisper.device: cpu` in `config.yaml`.
- **Piper missing**: delete `models/piper` and restart so it re-downloads.

## Out of scope (v1)

No avatar/lip-sync, no wake word, no bits/subs EventSub, no auto music ducking. Overlay is captions + status only.
