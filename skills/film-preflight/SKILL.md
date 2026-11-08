---
name: film-preflight
description: "Check any finished video in one ffmpeg pass before you publish it: hook, black frames, freezes, cuts, loudness. Use when a video (MP4, MOV, WebM) was rendered or edited and is about to be delivered, uploaded or reviewed."
---

# film-preflight

Check any finished video in one ffmpeg pass before you publish it: hook, black frames, freezes, cuts, loudness.

Run the commands from the repository root.

## Run

```bash
python film_preflight.py check <video> --out qa
```

Read `qa/report.txt`. Exit code 1 means at least one FAIL.

## Act on the result

- `FAIL hook`: the first 2 s do not move. Open on the motion itself (camera move or action from frame 1), no still title, no fade from black.
- `FAIL black open`: the film starts on black. Trim the head or start on picture.
- `WARN/FAIL long stills`: a stretch over 3 s barely changes; over 10 s usually means the capture froze. Re-render that range.
- `WARN black gaps`: confirm each black gap is intended.
- `WARN loudness`: normalise to -14 LUFS integrated with true peak at or below -1 dBTP.
- `INFO` lines are measurements to check against the plan (cuts, silences, duplicates, brightness).

Then open `qa/sheet.jpg` (one frame every 2 s, index in `qa/sheet-index.txt`) and look for text overflow, empty frames or wrong shots. `qa/spectrogram.png` shows the soundtrack.

Use `--json` for machine-readable output. Needs Python 3 and ffmpeg/ffprobe on PATH.
