# Bulle

Bulle is the voice assistant that lives in my living room. It shows up as a face on the TV (two eyes, no mouth, one
orange accent), listens through an old Xbox 360 Kinect, follows you with its eyes when you walk around, and controls
the house through Home Assistant, Spotify, a calendar and a CRM.

Everything runs on hardware I own. There is no account, no cloud service and no API key. Speech recognition, the
LLM and the voice all stay in the house: if I block its internet access it still understands me and still turns
the lights off. Weather and Spotify do need the internet, of course.

This repo is not a framework. It's the actual code that runs my house, published as it is. **It's written in
French**: variable names, comments, commit messages, everything. That's the language we speak at home and I decided
to keep it. The original French README is in [README.fr.md](README.fr.md).

I design, review and merge. A local model proposes fixes every night and the test bench decides whether they
live — that loop is described further down, and it's a real part of how this thing is built.

> The IP addresses, hostnames and contact details in this tree were rewritten to documentation ranges
> (`192.0.2.x`, `example.org`). The architecture is the real one, the addresses are not.

## What runs where

| Where | What | Files |
|---|---|---|
| **Raspberry Pi 3** | the face (GLES2 shader, 30 fps), the audio client (Kinect 4 mic array, Silero VAD), person tracking with the Kinect depth camera | `face.py`, `compagnon.py`, `pi/tracker.py` |
| | the cards displayed next to the face | `carte.py` |
| **A PC with one RTX 3090** | the brain: Whisper, then `gpt-oss:20b` with tools, then an emotion tag, then Kyutai TTS | `cerveau/server.py` |
| | turns a tool result into card data | `cerveau/cartes.py` |
| | behaviour rules, reloaded without restart | `config/regles.yaml` |
| | composed tools (music, lights, places, long term memory...) | `cerveau/outils_composes.py` |
| | SQLite journal, a `/suivi` review page, `/metrics` for Prometheus | `cerveau/journal.py`, `cerveau/suivi.py`, `cerveau/mesures.py` |
| **Home Assistant** | lights, amplifier, TV | |

The tracking doesn't use the Kinect skeleton. `pi/tracker.py` learns the depth of the empty room, takes the biggest
shape in front of it on an 80x60 grid, and sends a gaze direction to the face about twelve times per second. No
image of the room is stored or sent anywhere.

## Speed

I picked the model by replaying the same bench of real sentences against every model that fits on the card next
to Whisper and the TTS. The 3090 is shared with other things, so that's about 15 GB.

| | `gpt-oss:20b` | second best |
|---|---|---|
| bench, when I compared (51 cases then) | **51/51** | 50/51 |
| GPU time per exchange (LLM only) | **1.50 s** | 2.33 s |
| model time to the first sentence | **1.59 s** | 2.54 s |

The bench has grown since; `gpt-oss:20b` passes **58/58** today.

Careful with that last row: it is the model alone. It starts once Whisper is done and stops when the sentence
enters the speech queue, so it leaves out both ends. What you wait for in the room is longer: about a second for
the Pi to decide I stopped talking, 0.7 s of Whisper, then the model, then about 2 s to synthesise the first
sentence. In the demo video you can count 5 to 8 seconds between the end of my sentence and the first word of the
answer, and the weather is the slow one because it calls a tool first. The rest of the answer is spoken while the
model is still writing, so the whole reply is not much longer than its first sentence.

These numbers come from the `/metrics` endpoint of the brain, except the last one, which you can time on the
video yourself. The brain measures both: `bulle_premiere_phrase` for the model alone, `bulle_premier_son` from
the end of the utterance to the first sound sent to the TV.

## Cards

Some things are painful to listen to: a list, numbers, how a name is spelled. Those are shown next to the face. The
face moves to one third of the screen, looks at the card, and the card goes away by itself after a few seconds.

- The voice still says everything. If you're in the kitchen you don't miss anything, the card just adds detail.
- A card is built from the **result of a tool**, never from the text of the LLM. The model can't invent a layout,
  and the card can be sent while the model is still thinking, which fills the silence.
- The brain sends data, not an image. The Pi does the layout.

## Privacy

It's a microphone in a room where people live. It hears whole conversations that have nothing to do with it, and
the rule is that **nothing that wasn't meant for Bulle is kept**: not in the database, not in `journalctl`, not in
a `print`.

The text of a transcription is only logged once the server has decided that it was addressed to Bulle. For an
ignored sentence, what remains is the time, the length, the reason, and the first word only when it looks like the
name (a badly transcribed "Bulle" is useful to know about, the rest is not). A nightly job runs the erasure again,
so if a bug ever brought text back, it would be gone within a day. `tests/unitaires/test_vie_privee.py` checks all
this, and `test_mesures.py` checks that nothing said in the room can end up in a Prometheus label.

## The night loop

```
3 am (timer)                                                      morning
analyst -> incidents -> local agent -> branch agent/... -> review -> production -> report
 (logs, SQLite, health)  (few files only)   judged by the bench       (the merge runs the bench
                                            on a test instance         again and can revert itself)
```

- **Analyst** (`agent/analyste.py`, no LLM): reads the logs, the SQLite journal and the health data, and produces a
  list of incidents, matched against a file of known failures (`connaissances/pannes.yaml`).
- **Local agent** (`agent/boucle.py`, `agent/consigne_agent.md`): a local coding model working on its own branch.
  It can write to five files only and it never merges.
- **Its one rule**: a fix comes with a test that fails before and passes after. Otherwise there's no way to tell a
  fix from luck.
- **The judge** is the bench (`tests/banc.yaml`). It replays real sentences in simulation mode, so nothing happens
  in the house. The LLM isn't deterministic, so failures are replayed, and a case that flips between pass and fail
  is reported as a bad test instead of blocking the fix.
- **Review** (`agent/revue.py`): I read the branch in the morning. The merge runs the bench again on production and
  reverts itself if something regressed.

## Running the tests

```bash
pip install -r tests/requirements.txt
python outils/valider.py     # the YAML files load and have the expected shape
pytest                       # no network, no GPU, no Kinect, no running brain needed
```

`tests/banc.py` is the end to end bench. It needs a running brain, so it needs a GPU.

## What this repo is not

You can't install it in five minutes. The brain talks to tool servers (Home Assistant, Spotify, calendar, CRM) that
are not in this repo, and the systemd units and paths are the ones of my house. Take it as a worked example of a
fully local assistant, and of a codebase maintained with agents.

If you're curious about what it would take to interrupt the assistant while it talks, there's a design note in
`connaissances/full-duplex.md` (in French).

## License

[MIT](LICENSE). The Geist font in `pi/polices/` has its own license, the SIL Open Font License
(`pi/polices/OFL.txt`).
