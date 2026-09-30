#!/usr/bin/env python3
"""
Kinetic Typography Lyric Video Renderer
========================================
For: Shayan Yo - Sekte (Official Track)
Style: Persian emotional ballad — dark cinematic with melancholic gold accents

Pipeline:
  1. Parse LRC (or raw text + LRC) — strip [موسیقی]/[صدای آواز] markers
  2. Audio analysis — onset detection for word-snapping
  3. Word-level timing — distribute words across line time, snap to onsets
  4. Per-frame Pillow render — background + animated words + film grain
  5. ffmpeg encode — JPEG sequence → H.264 + AAC

Design language (Persian emotional / dark cinematic):
  - Background: deep navy/midnight with slow radial gradient + drifting grain
  - Primary text: warm off-white (not pure white) with subtle bloom
  - Kick words (emotional climax): saturated crimson with multi-pass glow
  - Font: Vazirmatn-Bold (modern Persian sans-serif)
  - Composition: words animate word-by-word, horizontal center, vertical rhythm
  - Word entry: scale-up from 0.85 → 1.0 + alpha fade-in over ~6 frames
  - Word exit: stays visible while later words in line appear; line clears on boundary
  - Translation between lines: ambient fade with golden dust particles
"""

import os
import re
import sys
import json
import math
import wave
import struct
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Tuple, Optional
from multiprocessing import Pool

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

# =========================================================================
# CONFIG
# =========================================================================
FPS = 30
W, H = 1920, 1080
DURATION_PAD = 0.5  # extra seconds of black at end
JPEG_QUALITY = 92

# Style colors (RGB) — dark cinematic with warm gold
BG_COLOR_TOP    = (8, 10, 22)        # deep midnight
BG_COLOR_BOTTOM = (22, 14, 30)       # muted plum
BG_RADIAL_GLOW  = (140, 90, 50, 35)  # warm amber glow, low alpha
TEXT_COLOR      = (245, 240, 232)    # warm off-white (not pure white)
TEXT_SHADOW     = (0, 0, 0, 110)     # soft drop shadow
KICK_COLOR      = (230, 75, 80)      # crimson for kick words
KICK_GLOW       = (255, 130, 90, 100)  # warm orange glow

# Timing constants (frames)
WORD_FADE_IN_FRAMES = 5   # ~167ms
WORD_FADE_OUT_FRAMES = 4  # for line exit
LINE_TRANSITION_FRAMES = 18  # ~600ms crossfade between lines

FONT_PATH = "assets/Vazirmatn-Bold.ttf"
FONT_SIZE = 92  # will auto-scale to fit
FONT_MIN_SIZE = 64

# =========================================================================
# LRC PARSER
# =========================================================================
def parse_lrc_time(ts: str) -> float:
    """Parse 'HH:MM:SS,mmm' or 'MM:SS,mmm' to seconds."""
    ts = ts.strip().replace(',', '.')
    parts = ts.split(':')
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h = 0
        m, s = parts
    else:
        return 0.0
    return int(h) * 3600 + int(m) * 60 + float(s)


def strip_brackets(text: str) -> str:
    """Remove [موسیقی], [صدای آواز], [anything] inline markers."""
    return re.sub(r'\[[^\]]*\]', '', text).strip()


def parse_lrc_file(path: str) -> List[dict]:
    """Returns list of {'start': float, 'end': float, 'text': str, 'words': [str]}.
    Supports both single-line ('ts --> ts text') and multi-line ('ts --> ts' on its own line,
    text on next line) LRC formats."""
    raw = Path(path).read_text(encoding='utf-8').splitlines()
    pattern = re.compile(r'(\d{1,2}:\d{2}(?::\d{2})?[,\.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?[,\.]\d{1,3})')
    entries = []
    pending_ts = None  # holds (start, end) when text is on a separate line
    for line in raw:
        line = line.strip()
        if not line:
            pending_ts = None
            continue
        if re.fullmatch(r'\d+', line):
            continue  # line counter
        m = pattern.search(line)
        if m:
            start = parse_lrc_time(m.group(1))
            end = parse_lrc_time(m.group(2))
            inline_text = line[m.end():].strip()
            inline_text = strip_brackets(inline_text)
            if inline_text:
                # Single-line LRC — text is on the same line
                words = [w for w in re.split(r'\s+', inline_text) if w]
                entries.append({
                    'start': start, 'end': end,
                    'text': inline_text, 'words': words,
                })
                pending_ts = None
            else:
                # Multi-line LRC — text will be on next non-empty line
                pending_ts = (start, end)
        else:
            # Not a timestamp line → must be the text of a pending timestamp
            if pending_ts is not None:
                text = strip_brackets(line)
                if text:
                    words = [w for w in re.split(r'\s+', text) if w]
                    entries.append({
                        'start': pending_ts[0], 'end': pending_ts[1],
                        'text': text, 'words': words,
                    })
                pending_ts = None
    # Sort by start, fill small gaps
    entries.sort(key=lambda e: e['start'])
    for i in range(len(entries) - 1):
        if entries[i]['end'] >= entries[i+1]['start']:
            entries[i]['end'] = entries[i+1]['start'] - 0.05
    return entries


# =========================================================================
# AUDIO ANALYSIS — onset detection (stdlib only)
# =========================================================================
def decode_audio_to_pcm(mp3_path: str, target_sr: int = 22050) -> Tuple[List[float], int]:
    """Decode mp3 to mono PCM via ffmpeg, return (samples_normalized, sample_rate)."""
    cmd = [
        'ffmpeg', '-y', '-i', mp3_path,
        '-ac', '1', '-ar', str(target_sr),
        '-f', 's16le', '-loglevel', 'error',
        'pipe:1'
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {proc.stderr.decode()[:500]}")
    raw = proc.stdout
    samples = list(struct.unpack(f'<{len(raw)//2}h', raw))
    norm = [s / 32768.0 for s in samples]
    return norm, target_sr


def detect_onsets(samples: List[float], sr: int, hop: int = 512) -> List[float]:
    """Spectral flux onset detection. Returns onset times in seconds."""
    n = len(samples)
    frame_size = 1024
    onsets = []
    prev_mag = [0.0] * (frame_size // 2)
    threshold = 0.15
    min_gap_sec = 0.08
    min_gap_frames = int(min_gap_sec * sr / hop)
    last_onset_frame = -10000

    for frame_start in range(0, n - frame_size, hop):
        # Simple DFT (slow but stdlib-only); use only first ~80 bins (low freq = vocals)
        frame = samples[frame_start:frame_start + frame_size]
        # Apply Hann window
        window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (frame_size - 1)) for i in range(frame_size)]
        windowed = [frame[i] * window[i] for i in range(frame_size)]
        # Magnitudes of first 80 bins (cosine DFT — slow path acceptable for one-time analysis)
        mag = []
        for k in range(80):
            re = sum(windowed[i] * math.cos(2 * math.pi * k * i / frame_size) for i in range(frame_size))
            im = sum(windowed[i] * math.sin(2 * math.pi * k * i / frame_size) for i in range(frame_size))
            mag.append(math.sqrt(re*re + im*im) / frame_size)
        # Spectral flux: sum positive differences
        flux = sum(max(0.0, mag[k] - prev_mag[k]) for k in range(80))
        frame_idx = frame_start // hop
        if flux > threshold and (frame_idx - last_onset_frame) > min_gap_frames:
            onsets.append(frame_start / sr)
            last_onset_frame = frame_idx
        prev_mag = mag
    return onsets


def find_closest_onset(target: float, onsets: List[float], search_window: float = 0.4) -> Optional[float]:
    """Find nearest onset within ±search_window of target."""
    if not onsets:
        return None
    # Binary search
    lo, hi = 0, len(onsets) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if onsets[mid] < target - search_window:
            lo = mid + 1
        elif onsets[mid] > target + search_window:
            hi = mid - 1
        else:
            break
    # Scan around mid
    best = None
    best_dist = float('inf')
    for i in range(max(0, lo - 5), min(len(onsets), hi + 5)):
        d = abs(onsets[i] - target)
        if d < best_dist:
            best_dist = d
            best = onsets[i]
    return best if best_dist < search_window else None


# =========================================================================
# WORD TIMING
# =========================================================================
def compute_word_timings(entries: List[dict], onsets: List[float]) -> List[dict]:
    """
    For each entry, distribute words across [start, end] linearly,
    then snap each word start to nearest onset (if available) for musical feel.
    """
    out = []
    for entry in entries:
        words = entry['words']
        n = len(words)
        if n == 0:
            continue
        line_dur = entry['end'] - entry['start']
        # Linear distribution baseline
        word_dur = line_dur / n
        timings = []
        for i, w in enumerate(words):
            base_start = entry['start'] + i * word_dur
            base_end = entry['start'] + (i + 1) * word_dur
            # Snap start to onset
            snap = find_closest_onset(base_start, onsets, search_window=word_dur * 0.6)
            if snap is not None and snap < base_end - 0.05:
                start = snap
            else:
                start = base_start
            end = base_end if i < n - 1 else entry['end']
            timings.append({
                'word': w,
                'start': start,
                'end': end,
                'line_idx': len(out),
                'word_idx': i,
                'is_kick': False,  # set later
            })
        out.append({
            'line_text': entry['text'],
            'line_start': entry['start'],
            'line_end': entry['end'],
            'words': timings,
        })
    return out


# =========================================================================
# KICK WORD DETECTION
# =========================================================================
PERSIAN_FILLERS = {'من', 'تو', 'که', 'را', 'می', 'یه', 'با', 'از', 'به', 'و', 'در', 'از'}
EMOTIONAL_PEAK_WORDS = {
    'سکته', 'دلتنگ', 'اشتباه', 'تنت', 'بم', 'دروغگوی', 'برو',
    'واقعیم', 'عاشق', 'رفتی', 'مردم', 'احمق', 'قرمز', 'عشق',
    'دلم', 'تنگ', 'چشات'
}


def detect_kick_words(lines: List[dict]) -> None:
    """Mark kick words — emotional peaks in each line, max 1 per line, max 1 per 4 sec."""
    last_kick_end = -10
    for line in lines:
        # Priority 1: emotional peak word
        kick_idx = -1
        for i, wd in enumerate(line['words']):
            base = re.sub(r'[،.؟!«»]', '', wd['word'])
            if base in EMOTIONAL_PEAK_WORDS and wd['end'] - last_kick_end > 3.0:
                kick_idx = i
                break
        # Priority 2: longest non-filler word from end
        if kick_idx == -1:
            for j in range(len(line['words']) - 1, -1, -1):
                base = re.sub(r'[،.؟!«»]', '', line['words'][j]['word'])
                if base not in PERSIAN_FILLERS and len(base) >= 4:
                    if line['words'][j]['end'] - last_kick_end > 3.0:
                        kick_idx = j
                    break
        if kick_idx >= 0:
            line['words'][kick_idx]['is_kick'] = True
            last_kick_end = line['words'][kick_idx]['end']


# =========================================================================
# FONT LOADING
# =========================================================================
def load_font(size: int) -> ImageFont.FreeTypeFont:
    """Load Vazirmatn-Bold, fall back to any TTF if missing."""
    candidates = [
        FONT_PATH,
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
    ]
    for c in candidates:
        if os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                continue
    return ImageFont.load_default()


def measure_word(draw: ImageDraw.ImageDraw, word: str, font) -> Tuple[int, int]:
    bbox = draw.textbbox((0, 0), word, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def fit_font_size(line_words: List[str], max_width: int, draw: ImageDraw.ImageDraw,
                  start_size: int = FONT_SIZE, min_size: int = FONT_MIN_SIZE) -> ImageFont.FreeTypeFont:
    """Shrink font size until the longest word fits in max_width."""
    size = start_size
    while size > min_size:
        font = load_font(size)
        ok = True
        for w in line_words:
            ww, _ = measure_word(draw, w, font)
            if ww > max_width:
                ok = False
                break
        if ok:
            return font
        size -= 4
    return load_font(min_size)


# =========================================================================
# FRAME RENDER
# =========================================================================
def render_background(t: float, frame_idx: int) -> Image.Image:
    """Dark cinematic background — gradient + radial glow + film grain."""
    img = Image.new('RGB', (W, H), BG_COLOR_TOP)
    draw = ImageDraw.Draw(img)

    # Vertical gradient (24px-wide bands)
    band_h = 6
    for y in range(0, H, band_h):
        ratio = y / H
        # Slow color shift over time
        time_shift = math.sin(t * 0.15) * 0.05
        r = int(BG_COLOR_TOP[0] * (1 - ratio) + BG_COLOR_BOTTOM[0] * ratio + time_shift * 30)
        g = int(BG_COLOR_TOP[1] * (1 - ratio) + BG_COLOR_BOTTOM[1] * ratio + time_shift * 20)
        b = int(BG_COLOR_TOP[2] * (1 - ratio) + BG_COLOR_BOTTOM[2] * ratio + time_shift * 10)
        r = max(0, min(255, r))
        g = max(0, min(255, g))
        b = max(0, min(255, b))
        draw.rectangle([0, y, W, y + band_h], fill=(r, g, b))

    # Radial glow that breathes with the music
    glow_layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    cx, cy = W // 2, H // 2
    # Glow intensity pulses slowly
    pulse = 0.7 + 0.3 * math.sin(t * 1.2)
    max_r = int(700 * pulse)
    for r in range(max_r, 0, -20):
        alpha = int((1 - r / max_r) ** 2 * BG_RADIAL_GLOW[3] * pulse)
        glow_draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                          fill=(BG_RADIAL_GLOW[0], BG_RADIAL_GLOW[1], BG_RADIAL_GLOW[2], alpha))
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(40))
    img = Image.alpha_composite(img.convert('RGBA'), glow_layer).convert('RGB')

    # Subtle moving spotlight near center (slow drift)
    drift_x = int(50 * math.sin(t * 0.3))
    drift_y = int(30 * math.cos(t * 0.25))
    spotlight = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(spotlight)
    sd.ellipse([cx - 300 + drift_x, cy - 300 + drift_y,
                cx + 300 + drift_x, cy + 300 + drift_y],
               fill=(255, 200, 140, 8))
    spotlight = spotlight.filter(ImageFilter.GaussianBlur(80))
    img = Image.alpha_composite(img.convert('RGBA'), spotlight).convert('RGB')

    # Film grain
    import random
    rng = random.Random(frame_idx)  # deterministic per frame for reproducibility
    grain_layer = Image.new('RGB', (W, H))
    gd = ImageDraw.Draw(grain_layer)
    n_grains = (W * H) // 1200
    for _ in range(n_grains):
        x = rng.randint(0, W - 1)
        y = rng.randint(0, H - 1)
        v = rng.randint(-12, 12)
        grain_layer.putpixel((x, y), (128 + v, 128 + v, 128 + v))
    grain_layer = grain_layer.filter(ImageFilter.GaussianBlur(0.6))
    img = Image.blend(img, Image.composite(img, grain_layer, Image.eval(grain_layer.convert('L'), lambda x: 40)), 0.15)

    return img


def render_word_on_layer(word: str, font, color: Tuple[int, int, int],
                         glow_color: Optional[Tuple[int, int, int, int]],
                         alpha: float, scale: float) -> Image.Image:
    """Render a single word as an RGBA layer with optional glow."""
    # Measure at base scale
    tmp = Image.new('RGBA', (1, 1))
    td = ImageDraw.Draw(tmp)
    bbox = td.textbbox((0, 0), word, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad = 80
    layer = Image.new('RGBA', (int(tw * scale) + pad * 2, int(th * scale) + pad * 2), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)

    scaled_font = load_font(int(font.size * scale)) if abs(scale - 1.0) > 0.01 else font

    # Drop shadow
    shadow_layer = Image.new('RGBA', layer.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    sd.text((pad + 4, pad + 6), word, font=scaled_font, fill=TEXT_SHADOW)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(6))
    layer = Image.alpha_composite(layer, shadow_layer)

    # Glow pass for kick words
    if glow_color:
        glow_layer = Image.new('RGBA', layer.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_layer)
        gd.text((pad, pad), word, font=scaled_font, fill=glow_color)
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(14))
        layer = Image.alpha_composite(layer, glow_layer)

    # Main text
    text_layer = Image.new('RGBA', layer.size, (0, 0, 0, 0))
    td2 = ImageDraw.Draw(text_layer)
    td2.text((pad, pad), word, font=scaled_font, fill=color + (255,))
    layer = Image.alpha_composite(layer, text_layer)

    # Apply alpha
    if alpha < 1.0:
        r, g, b, a = layer.split()
        a = a.point(lambda v: int(v * alpha))
        layer = Image.merge('RGBA', (r, g, b, a))

    return layer


def ease_out_cubic(t: float) -> float:
    return 1 - (1 - t) ** 3


def ease_in_out_quad(t: float) -> float:
    if t < 0.5:
        return 2 * t * t
    return 1 - (-2 * t + 2) ** 2 / 2


def render_frame(args):
    frame_idx, total_frames, lines_data, output_dir = args
    t = frame_idx / FPS

    # Background
    img = render_background(t, frame_idx)

    # Find active words at this time
    active_layers = []
    active_layers_data = []
    for line in lines_data:
        # Whole-line exit transition
        line_exit_progress = 0.0
        if t > line['line_end'] and t < line['line_end'] + LINE_TRANSITION_FRAMES / FPS:
            line_exit_progress = (t - line['line_end']) / (LINE_TRANSITION_FRAMES / FPS)
        elif t > line['line_end'] + LINE_TRANSITION_FRAMES / FPS:
            continue  # line fully gone

        # Find which word is "current" in this line
        current_word_idx = -1
        for i, w in enumerate(line['words']):
            if w['start'] <= t < w['end']:
                current_word_idx = i
                break

        for i, w in enumerate(line['words']):
            # Visibility window: word stays visible from its start until line ends + transition
            word_visible_end = line['line_end'] + LINE_TRANSITION_FRAMES / FPS
            if t < w['start'] - 0.05 or t > word_visible_end:
                continue

            # Alpha based on entry
            entry_t = (t - w['start']) / (WORD_FADE_IN_FRAMES / FPS)
            if entry_t < 0:
                entry_alpha = 0.0
            elif entry_t < 1.0:
                entry_alpha = ease_out_cubic(min(1.0, entry_t))
            else:
                entry_alpha = 1.0

            # Scale: from 0.85 to 1.0 during entry, settle at 1.0
            if entry_t < 1.0:
                scale = 0.85 + 0.15 * ease_out_cubic(min(1.0, entry_t))
            else:
                scale = 1.0

            # Alpha decay on line exit
            if line_exit_progress > 0:
                entry_alpha *= (1.0 - line_exit_progress)

            if entry_alpha <= 0.01:
                continue

            # Color/glow based on kick
            color = KICK_COLOR if w['is_kick'] else TEXT_COLOR
            glow = KICK_GLOW if w['is_kick'] else None

            # Defer actual rendering — we collect (word, color, glow, alpha, scale, line, idx)
            # and render after we've loaded a real font in the loop below.
            active_layers_data.append((w['word'], current_word_idx == i, w['is_kick']))
            active_layers.append((w['word'], color, glow, entry_alpha, scale, line, i))

    # Choose font size to fit the line
    if not active_layers:
        # No text — render quiet frame
        out_path = os.path.join(output_dir, f"f{frame_idx:05d}.jpg")
        img.save(out_path, quality=JPEG_QUALITY, optimize=False)
        return

    # Render with auto-fit font
    # Find longest word across visible lines for size estimation
    visible_words = [w['word'] for w_data in [] for w in []]  # placeholder
    # Just use first line for font sizing
    first_line = active_layers[0][1]
    draw_tmp = ImageDraw.Draw(img)
    font = fit_font_size([w['word'] for w in first_line['words']],
                         max_width=int(W * 0.85), draw=draw_tmp)

    # Composite each word with proper font and positioning
    composite = img.convert('RGBA')

    for word, color, glow, entry_alpha, scale, line, word_idx in active_layers:
        w_info = line['words'][word_idx]
        # Recompute alpha/scale for current frame (already computed above but kept for safety)
        entry_t = (t - w_info['start']) / (WORD_FADE_IN_FRAMES / FPS)
        if entry_t < 0:
            entry_alpha = 0.0
        elif entry_t < 1.0:
            entry_alpha = ease_out_cubic(min(1.0, entry_t))
        else:
            entry_alpha = 1.0
        if entry_t < 1.0:
            scale = 0.85 + 0.15 * ease_out_cubic(min(1.0, entry_t))
        else:
            scale = 1.0

        line_exit_progress = 0.0
        if t > line['line_end'] and t < line['line_end'] + LINE_TRANSITION_FRAMES / FPS:
            line_exit_progress = (t - line['line_end']) / (LINE_TRANSITION_FRAMES / FPS)
        if line_exit_progress > 0:
            entry_alpha *= (1.0 - line_exit_progress)

        word_layer = render_word_on_layer(w_info['word'], font, color, glow, entry_alpha, scale)

        # Position: center horizontally, vertically offset based on which word in line
        # Single-line composition: stack words horizontally with spacing
        all_words_in_line = line['words']
        # Measure each word for total width
        word_widths = []
        total_width = 0
        for aw in all_words_in_line:
            tw, th = measure_word(draw_tmp, aw['word'], font)
            word_widths.append((tw, th))
            total_width += tw
        # Add spacing between words
        spacing = int(font.size * 0.25)
        total_width += spacing * (len(all_words_in_line) - 1)

        # Center the line
        x_cursor = (W - total_width) // 2

        # Vertical position: center, with subtle Y drift per word
        max_th = max(wh[1] for wh in word_widths) if word_widths else font.size
        y_center = H // 2 + int(15 * math.sin(t * 0.4 + word_idx * 0.7))

        # Find x position of THIS word
        word_x = x_cursor
        for k in range(word_idx):
            word_x += word_widths[k][0] + spacing
        word_y = y_center - max_th // 2

        # Paste word layer onto composite at calculated position.
        # word_layer is a small RGBA image with padding; we paste it so the
        # text bbox lines up at (word_x, word_y). Padding of 80px around the
        # word in the layer handles glow/shadow that extends past the bbox.
        composite.paste(word_layer, (word_x - 80, word_y - 80), word_layer)

    img = composite.convert('RGB')

    # Save
    out_path = os.path.join(output_dir, f"f{frame_idx:05d}.jpg")
    img.save(out_path, quality=JPEG_QUALITY, optimize=False)


# =========================================================================
# MAIN
# =========================================================================
def main():
    if len(sys.argv) < 4:
        print("Usage: render.py <audio.mp3> <lyrics.lrc.txt> <output_dir> [fps]")
        sys.exit(1)
    audio_path = sys.argv[1]
    lyrics_path = sys.argv[2]
    output_dir = sys.argv[3]
    os.makedirs(output_dir, exist_ok=True)

    print(f"[1/5] Parsing lyrics: {lyrics_path}")
    entries = parse_lrc_file(lyrics_path)
    print(f"      Found {len(entries)} lyric lines")

    print(f"[2/5] Analyzing audio: {audio_path}")
    samples, sr = decode_audio_to_pcm(audio_path)
    duration = len(samples) / sr
    print(f"      Duration: {duration:.2f}s @ {sr}Hz mono")
    print(f"      Detecting onsets...")
    onsets = detect_onsets(samples, sr)
    print(f"      Found {len(onsets)} onsets")

    print(f"[3/5] Computing word timings + kick words")
    lines = compute_word_timings(entries, onsets)
    detect_kick_words(lines)
    total_words = sum(len(l['words']) for l in lines)
    kick_count = sum(1 for l in lines for w in l['words'] if w['is_kick'])
    print(f"      {total_words} words, {kick_count} marked as kick")

    n_frames = int((duration + DURATION_PAD) * FPS)
    print(f"[4/5] Rendering {n_frames} frames @ {FPS}fps to {output_dir}")

    # Save timing data
    timing_data = []
    for l in lines:
        timing_data.append({
            'line_text': l['line_text'],
            'line_start': l['line_start'],
            'line_end': l['line_end'],
            'words': [{'word': w['word'], 'start': w['start'], 'end': w['end'], 'is_kick': w['is_kick']}
                      for w in l['words']]
        })
    with open(os.path.join(output_dir, '..', 'timing.json'), 'w', encoding='utf-8') as f:
        json.dump({'duration': duration, 'lines': timing_data}, f, ensure_ascii=False, indent=2)

    # Render in parallel
    frame_args = [(i, n_frames, lines, output_dir) for i in range(n_frames)]
    n_workers = min(8, os.cpu_count() or 4)

    with Pool(n_workers) as pool:
        for i, _ in enumerate(pool.imap_unordered(render_frame, frame_args, chunksize=4)):
            if i % 200 == 0:
                pct = 100 * i / n_frames
                print(f"      {i}/{n_frames} frames ({pct:.1f}%)")

    print(f"[5/5] Done. {n_frames} frames in {output_dir}")


if __name__ == '__main__':
    main()