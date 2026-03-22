#!/usr/bin/env python3
"""
Patches SkyReels V3 generate_video.py with proven fixes:
1. Local file path support (not just URLs)
2. UUID-based processed_audio dirs (parallel audio conflict fix)

NOTE: SageAttention removed — tested and not recommended for parallel FP8.
See video-plan/skyreels-v3-reference.md for details.

Usage: python3 patch-skyreels.py /workspace/SkyReels-V3/generate_video.py
"""
import pathlib
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/workspace/SkyReels-V3/generate_video.py"
p = pathlib.Path(path)
t = p.read_text()
patches = 0

# Patch 1: Local file path support
old1 = 'def maybe_download(path_or_url: str, save_dir: str) -> str:'
new1 = 'def maybe_download(path_or_url: str, save_dir: str) -> str:\n    import os\n    if os.path.isfile(path_or_url): return path_or_url'
if old1 in t and 'os.path.isfile' not in t:
    t = t.replace(old1, new1)
    patches += 1
    print("[1/2] Patched: local file path support")
else:
    print("[1/2] Already patched or not found: local file path")

# Patch 2: UUID-based processed_audio dirs
old2 = 'input_data, _ = preprocess_audio(args.model_id, input_data, "processed_audio")'
new2 = 'import uuid; _audio_dir = f"processed_audio_{uuid.uuid4().hex[:8]}"; input_data, _ = preprocess_audio(args.model_id, input_data, _audio_dir)'
if old2 in t:
    t = t.replace(old2, new2)
    patches += 1
    print("[2/2] Patched: UUID-based audio dirs (parallel fix)")
else:
    print("[2/2] Already patched or not found: audio dirs")

p.write_text(t)
print(f"\nDone. {patches} patches applied to {path}")
