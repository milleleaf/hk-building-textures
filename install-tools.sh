#!/usr/bin/env bash
# The tools a runner needs, both pinned and checked against a sha256 written here:
#   KTX-Software 4.4.2 (toktx, ktx) from its GitHub release, and a static ffmpeg + ffprobe from BtbN/FFmpeg-Builds (dated autobuild).
# apt's ffmpeg took 31 min on one run (a slow Ubuntu mirror); this takes seconds. The ffmpeg tarball is kept in $FFMPEG_CACHE (the
# workflows cache that folder by sha, so it survives BtbN pruning old autobuild releases while ticks keep it warm).
# Sets KTX_BIN, LD_LIBRARY_PATH (GITHUB_ENV) and puts ffmpeg on PATH (GITHUB_PATH).
set -euo pipefail
KTX_URL=https://github.com/KhronosGroup/KTX-Software/releases/download/v4.4.2/KTX-Software-4.4.2-Linux-x86_64.tar.bz2
KTX_SHA=a8781bad05f9624edbf910b7f258cd0a4ba7d3e63b49ecc0a0ab440bf6a0a245
FF_FILE=ffmpeg-n8.1.3-24-g92611900bb-linux64-gpl-8.1.tar.xz
FF_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-24-13-02/$FF_FILE
FF_SHA=3c2d43d951c1d7936240ec77dbc274d6214fc64973e9993f63e16f03ac908ff1
T=${RUNNER_TEMP:-/tmp}; C=${FFMPEG_CACHE:-$HOME/.cache/hkbt-ffmpeg}; mkdir -p "$T/ktx" "$T/ffmpeg" "$C"

curl -fsSL -o "$T/ktx.tar.bz2" "$KTX_URL"; echo "$KTX_SHA  $T/ktx.tar.bz2" | sha256sum -c
tar -xjf "$T/ktx.tar.bz2" -C "$T/ktx" --strip-components=1; rm "$T/ktx.tar.bz2"

if ! { [ -f "$C/$FF_FILE" ] && echo "$FF_SHA  $C/$FF_FILE" | sha256sum -c --quiet; }; then
  rm -f "$C/$FF_FILE"; curl -fsSL -o "$C/$FF_FILE" "$FF_URL"
fi
echo "$FF_SHA  $C/$FF_FILE" | sha256sum -c
tar -xJf "$C/$FF_FILE" -C "$T/ffmpeg" --strip-components=1

export LD_LIBRARY_PATH="$T/ktx/lib" PATH="$T/ffmpeg/bin:$PATH"
"$T/ktx/bin/toktx" --version; ffmpeg -version | head -1; ffprobe -version | head -1
n=$(ffmpeg -v error -f lavfi -i testsrc=size=64x64 -frames:v 1 -f rawvideo -pix_fmt rgba - | wc -c)   # what lib.mjs does with an image
[ "$n" = 16384 ] || { echo "ffmpeg decode test gave $n bytes, expected 16384"; exit 1; }
if [ -n "${GITHUB_ENV:-}" ]; then
  echo "KTX_BIN=$T/ktx/bin" >> "$GITHUB_ENV"; echo "LD_LIBRARY_PATH=$T/ktx/lib" >> "$GITHUB_ENV"; echo "$T/ffmpeg/bin" >> "$GITHUB_PATH"
fi
