#!/bin/sh
# Render assets/*.svg -> assets/*.jpg inside a throwaway container (needs only Docker).
set -e
cd "$(dirname "$0")/.."
docker run --rm -v "$PWD/assets:/assets" python:3.12-slim sh -c '
  apt-get update -qq >/dev/null &&
  apt-get install -y -qq --no-install-recommends libcairo2 fonts-inter fonts-dejavu-core >/dev/null &&
  pip install -q cairosvg==2.9.1 pillow 2>/dev/null &&
  python - <<EOF
import io, cairosvg
from PIL import Image
for name, size in (("avatar", (640, 640)), ("description", (640, 360))):
    png = cairosvg.svg2png(url=f"/assets/{name}.svg", output_width=size[0], output_height=size[1])
    Image.open(io.BytesIO(png)).convert("RGB").save(f"/assets/{name}.jpg", "JPEG", quality=92)
    print("rendered", name, size)
EOF
'
