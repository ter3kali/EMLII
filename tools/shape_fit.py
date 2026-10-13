r"""
EMLII tool plugin: fit a single image as a shape program using the linked
shape_prompt_ai project (shape_fitter.py). Companion to shapegen.py's !shapegen
(text -> picture); this one goes picture -> shape program directly, no
training required.

Setup
    Same project folder as shape_prompt_ai.py — see that file's docstring
    for SHAPE_PROMPT_AI_DIR.

Command
    !shapefit <path to an image> [--shapes N] [--size N]
    !shapefit raw_images/sample_portrait.png --shapes 4000 --size 192
"""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

COMMAND = "shapefit"
HELP = "Fit an image as a shape program (shape_prompt_ai)"


def _project_dir():
    env = os.environ.get("SHAPE_PROMPT_AI_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "shape_prompt_ai"


def run(arg, ctx):
    arg = (arg or "").strip()
    if not arg:
        return {"type": "reply",
                "text": "Usage: !shapefit <image path> [--shapes N] [--size N]\n"
                        "Example: !shapefit raw_images/sample_portrait.png"}

    proj = _project_dir()
    script = proj / "shape_fitter.py"
    if not script.exists():
        return {"type": "reply",
                "text": f"shape_prompt_ai not found at {proj}.\n"
                        "Set SHAPE_PROMPT_AI_DIR, or place the project folder next to EMLII.py."}

    shapes, size = 1500, 128
    m = re.search(r"--shapes\s+(\d+)", arg)
    if m:
        shapes = max(50, min(20000, int(m.group(1))))
        arg = arg[:m.start()] + arg[m.end():]
    m = re.search(r"--size\s+(\d+)", arg)
    if m:
        size = max(32, min(512, int(m.group(1))))
        arg = arg[:m.start()] + arg[m.end():]
    img_arg = arg.strip()
    if not img_arg:
        return {"type": "reply", "text": "Give me an image path, e.g. !shapefit photo.png"}

    # Resolve relative to the shape_prompt_ai folder (so "raw_images/x.png"
    # works out of the box), but allow an absolute path too.
    img_path = Path(img_arg)
    if not img_path.is_absolute():
        candidate = proj / img_arg
        img_path = candidate if candidate.exists() else img_path
    if not img_path.exists():
        return {"type": "reply", "text": f"Image not found: {img_path}"}

    out_name = f"emlii_fit_{int(time.time())}"
    start = time.time()
    try:
        r = subprocess.run(
            [sys.executable, "shape_fitter.py", str(img_path),
             "--shapes", str(shapes), "--size", str(size),
             "--alpha", "--edge", "8", "--strokes", "--out", out_name],
            cwd=proj, capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {"type": "reply", "text": "Fitting timed out after 180s."}

    if r.returncode != 0:
        return {"type": "reply", "text": "Fit failed:\n" + r.stderr.strip()[-500:]}

    out_dir = Path(ctx["PATHS"]["files"])
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for ext in (".png", ".ipg"):
        src = proj / f"{out_name}{ext}"
        if src.exists() and src.stat().st_mtime >= start - 1:
            dest = out_dir / src.name
            shutil.copy(src, dest)
            made.append(str(dest))

    if not made:
        return {"type": "reply", "text": "Ran, but found no output files.\n" + r.stdout.strip()[-300:]}

    # shape_fitter.py prints its own summary line ("N shapes   final similarity ...")
    # on the last non-empty line of stdout — surface it instead of re-deriving it.
    summary = next((l for l in reversed(r.stdout.strip().splitlines()) if l.strip()), "")
    text = f"Fit {img_path.name} ({shapes} shapes, {size}px):\n{summary}\n" + "\n".join(made)
    return {"type": "reply", "text": text}
