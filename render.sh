#!/usr/bin/env bash
# One-shot render script for GitHub Codespace
# Usage: bash render.sh
set -e

echo "=== Kinetic Typography Lyric Video Renderer ==="
echo "For: Shayan Yo - Sekte (Official Track)"
echo ""

# Install deps if missing
python3 -c "import PIL" 2>/dev/null || {
  echo "Installing Pillow..."
  pip install --quiet Pillow 2>/dev/null || python3 -m pip install --quiet Pillow || \
    (echo "Trying uv..." && uv pip install --system Pillow 2>/dev/null) || \
    (echo "Installing via apt..." && sudo apt-get install -y -qq python3-pil)
}

which ffmpeg || {
  echo "Installing ffmpeg..."
  sudo apt-get update -qq
  sudo apt-get install -y -qq ffmpeg
}

# Download assets
mkdir -p assets frames

# Download song
if [ ! -f assets/song.mp3 ]; then
  echo "Downloading song..."
  curl -L -s -o assets/song.mp3 \
    "https://sv5.downloadyarbot.ir/IDM/Shayan_Yo_-_Sekte_-_OFFICIAL_TRACK.mp3"
  ls -lh assets/song.mp3
fi

# Download Vazirmatn font (Persian)
if [ ! -f assets/Vazirmatn-Bold.ttf ]; then
  echo "Downloading Vazirmatn-Bold font..."
  curl -L -s -o assets/Vazirmatn-Bold.ttf \
    "https://github.com/rastikerdar/vazirmatn/raw/master/fonts/ttf/Vazirmatn-Bold.ttf"
fi

# Render frames
echo ""
echo "=== Rendering frames ==="
echo "Expected: ~5,700 frames @ 30fps for 189s song"
echo "This will take 15-30 min depending on CPU cores..."
echo ""

START_TIME=$(date +%s)
python3 render.py assets/song.mp3 lyrics.txt frames/
RENDER_TIME=$(( $(date +%s) - START_TIME ))
echo "Frame rendering took ${RENDER_TIME}s"

# Encode with ffmpeg
echo ""
echo "=== Encoding MP4 ==="
ffmpeg -y -framerate 30 -i 'frames/f%05d.jpg' -i assets/song.mp3 \
  -c:v libx264 -preset medium -crf 20 -pix_fmt yuv420p \
  -c:a aac -b:a 192k -shortest \
  -movflags +faststart \
  output.mp4

ENCODE_TIME=$(( $(date +%s) - START_TIME - RENDER_TIME ))
echo "Encoding took ${ENCODE_TIME}s"

echo ""
echo "=== DONE ==="
ls -lh output.mp4
echo ""
echo "Total time: $((RENDER_TIME + ENCODE_TIME))s"
echo ""
echo "Upload to GitHub Release:"
echo "  gh release create v1.0 output.mp4 --title 'Shayan Yo - Sekte (Lyric Video)' --notes 'Kinetic typography render'"