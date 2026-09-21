# Running Bulle yourself

This is the code that runs my living room, not a product. There is no installer and no Docker image: the brain
talks to an LLM, a speech-to-text endpoint and a text-to-speech endpoint that you host, and to tool servers that
are not in this repo. What follows is the honest path from nothing to a face that answers you, in two stages.

Stage 1 needs **one Linux machine with a GPU, a microphone and a screen**, and takes about half an hour. No
Kinect, no Raspberry Pi. You get the face, the voice and the conversation, without the house.

Stage 2 adds the Kinect, the Pi and Home Assistant, which is what makes it the thing in the video.

---

## Stage 1 — the assistant, on one machine

### What you need running first

| Piece | What Bulle expects | What I use |
|---|---|---|
| LLM | Ollama, `/api/chat` with tool calling | `gpt-oss:20b` with a 32k context |
| Speech to text | any OpenAI-compatible `/v1/audio/transcriptions` | [Speaches](https://github.com/speaches-ai/speaches) with `whisper-large-v3-turbo` |
| Text to speech | any OpenAI-compatible `/v1/audio/speech` returning a WAV | a small adapter in front of [Kyutai TTS](https://github.com/kyutai-labs/delayed-streams-modeling) |

Only the LLM is mandatory. Without speech to text you can still type (`compagnon.py --text`); without text to
speech Bulle writes her answer on a card and says on screen that she has lost her voice.

### Install

```bash
git clone https://github.com/plateforme/bulle.git && cd bulle
python3 -m venv venv && ./venv/bin/pip install -r tests/requirements.txt
./venv/bin/pip install sounddevice onnxruntime        # the microphone client
./venv/bin/python -m pytest                           # 400+ tests, no GPU and no network needed
```

The tests passing on a bare machine is the quickest way to know the tree is intact.

### The shared token

The brain listens on `0.0.0.0` and drives a house: it refuses to start without a token, and every client must
present it.

```bash
mkdir -p ~/.config/bulle && head -c 32 /dev/urandom | base64 > ~/.config/bulle/jeton
chmod 600 ~/.config/bulle/jeton
```

### Start the three processes

```bash
# 1. the brain (adjust the URLs to your own services)
cd cerveau
OLLAMA_URL=http://localhost:11434 \
STT_URL=http://localhost:8793/v1/audio/transcriptions \
TTS_URL=http://localhost:8791/v1/audio/speech \
LLM_MODEL=gpt-oss:20b TZ_NAME=America/Montreal TOOL_SERVERS= \
../venv/bin/uvicorn server:app --host 0.0.0.0 --port 8802

# 2. the face, on the machine plugged into the screen
./venv/bin/python face.py --res 1280x720 --window

# 3. the microphone client
./venv/bin/python compagnon.py --server ws://localhost:8802/ws
```

`TOOL_SERVERS=` empty is deliberate for a first run: Bulle then has no tools, so she talks and nothing else. An
unreachable tool server is not fatal either, it is logged and skipped.

Say her name and then your sentence — "Bulle, what time is it?". She only understands French today: the wake
word, the rules in `config/regles.yaml` and the system prompt are all French, and so is her voice. Making her
speak another language means translating `config/regles.yaml` and pointing the TTS at another voice; nothing in
the code is hard-wired to French, but nobody has done it.

### Useful while you try

- `compagnon.py --list-devices` — pick a microphone with `--in-device`, a speaker with `--out-device`.
- `compagnon.py --text` — type instead of talking, the rest of the chain is identical.
- `face.py --demo` — cycle through the emotions and states without a brain.
- `face.py --grid out.png` — render every emotion to an image.
- `http://localhost:8802/suivi` — what she heard, what she did, what failed.
- `http://localhost:8802/metrics` — Prometheus counters, including the two latencies.

---

## Stage 2 — the house

### Tools

The brain discovers its tools by reading the `openapi.json` of every server listed in `TOOL_SERVERS`, and turns
each operation into a tool the model can call. Any OpenAPI server will do: point `TOOL_SERVERS` at yours,
comma-separated, and the operations appear as tools with their descriptions. Mine (Home Assistant, weather,
calendar, a CRM) are small FastAPI files that live outside this repo, because they hold the keys to my house.

`cerveau/outils_composes.py` is where a tool becomes a sentence: it chains several calls and returns what was
actually done. Read `allumer_tout` first, it's the shortest useful one.

### The Kinect and the Pi

The face and the microphone client run on a Raspberry Pi 3 plugged into the TV over HDMI. The Kinect is an Xbox
360 model (the one with the separate power supply).

- `libfreenect` **0.7.5 or newer, built from source**. The 0.5.3 packaged by Debian fails on this hardware with
  `send_cmd: Input control transfer failed (18)`.
- Blacklist the `gspca_kinect` kernel module, it grabs the camera first.
- The 4-microphone array needs the Kinect audio firmware; `kinect-audio-setup` extracts it from a Microsoft
  installer. Check the signature of what you download.
- `pi/tracker.py` learns the depth of the empty room and sends a gaze direction to the face about twelve times
  per second. It stores no image.

The three systemd units are in `pi/`: `kinectface-face.service`, `kinectface-compagnon.service`,
`kinectface-tracker.service`. They expect the tree in `/home/<user>/kinectface` and a venv beside it; edit the
paths and the user, then `systemctl enable --now`.

`face.py` renders through KMS (`SDL_VIDEODRIVER=kmsdrm`) so it holds 30 fps on a Pi 3 without a desktop. Boot the
Pi to the console, not to a graphical session.

### The night loop

`agent/boucle.py` is the nightly job: it reads the day's incidents, gives them to a local coding model in a git
worktree, and requires a test that fails before the fix and passes after. `outils/systemd/bulle-nuit.{service,timer}`
run it at 3am. It needs a coding agent on the machine and it merges nothing by itself — `agent/revue.py` is the
review step, and it reverts a merge that breaks the bench.

Leave this one off until the rest works.

---

## What will bite you

- **No token**: the brain refuses to start, on purpose. It listens on the network.
- **The client exits with code 75 when the brain goes away** and counts on systemd to bring it back. That is
  deliberate; give the unit `StartLimitIntervalSec=0` or it gives up after five restarts.
- **The model must fit on the card next to Whisper and the TTS.** If it doesn't, Ollama silently moves part of it
  to the CPU and an answer goes from 5 seconds to a minute. `ollama ps` shows a `100% GPU` column; watch it.
- **Two big models evict each other.** If something else on the same GPU loads a large model, Bulle's is unloaded
  and every answer pays a reload. Check `OLLAMA_MAX_LOADED_MODELS` and what else is running.
- **Speaker into microphone**: barge-in is disabled because the speaker leaks into the Kinect mics. If you solve
  echo cancellation on a Pi, I'd like to hear about it.
