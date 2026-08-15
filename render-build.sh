#!/usr/bin/env bash
set -euo pipefail

# Render Native Runtime build command: bash render-build.sh
# Keep Deno inside the build output so render-start.sh can put it on PATH.
export DENO_INSTALL="${DENO_INSTALL:-$PWD/.deno}"

python3 -m pip install --no-cache-dir -r requirements.txt

if [ ! -x "$DENO_INSTALL/bin/deno" ]; then
  curl -fsSL https://deno.land/install.sh | sh
fi

"$DENO_INSTALL/bin/deno" --version
python3 -c "from importlib.metadata import version; print('yt-dlp', version('yt-dlp')); print('yt-dlp-ejs', version('yt-dlp-ejs'))"
