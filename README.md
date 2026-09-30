# Kinetic Typography Lyric Video — Shayan Yo - Sekte

Lyric video renderer using Pillow + ffmpeg. Style: dark cinematic Persian typography with warm gold glow and crimson kick words.

## Files

- `render.py` — main renderer (parse LRC, detect onsets, render frames)
- `render.sh` — one-shot setup + render + encode
- `lyrics.txt` — LRC-format Persian lyrics (brackets stripped of music/vocal cues)
- `assets/song.mp3` — source audio
- `assets/Vazirmatn-Bold.ttf` — Persian display font (auto-downloaded)

## Run locally (Codespace)

```bash
bash render.sh
```

Output: `output.mp4` (1920×1080, 30fps, H.264+AAC).

## Style notes

- Background: deep midnight gradient with breathing amber radial glow + film grain
- Default text: warm off-white
- Kick words (emotional peaks: سکته، دلتنگ، عاشق، برو، etc.): crimson with multi-pass orange glow
- Word entry: 5-frame scale-up (0.85→1.0) + alpha fade
- Word timing: linear distribution snapped to nearest audio onset (±0.4s window)
- Line exit: 18-frame crossfade with line-wide alpha decay
- Single-line horizontal composition, words centered