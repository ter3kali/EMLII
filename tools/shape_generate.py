r"""
EMLII tool plugin: generate a picture from text using the linked
shape_prompt_ai project (net_binned.py). Drop this file into tools/ --
EMLII.py itself never needs to change to pick it up.

Setup
    Put (or symlink) the shape_prompt_ai project folder next to EMLII.py,
    or point SHAPE_PROMPT_AI_DIR at it:
        export SHAPE_PROMPT_AI_DIR=/path/to/shape_prompt_ai   (mac/linux)
        set    SHAPE_PROMPT_AI_DIR=C:\path\to\shape_prompt_ai (windows)
    Then train a model there first (see its own README, step 3):
        python net_binned.py train data_sharp --slots 40 --epochs 200
    which writes model_bin.npz into that folder.

Command
    !shapegen <words from your training captions> [--n 1-4] [--temp 0-2]
    !shapegen black hair, white background --n 2 --temp 0.8
"""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

COMMAND = "shapegen"
HELP = "Generate a picture from text (shape_prompt_ai)"


def _project_dir():
    env = os.environ.get("SHAPE_PROMPT_AI_DIR")
    if env:
        return Path(env)
    # Default: a sibling folder next to EMLII.py itself.
    return Path(__file__).resolve().parent.parent / "shape_prompt_ai"


def run(arg, ctx):
    arg = (arg or "").strip()
    if not arg:
        return {"type": "reply",
                "text": "Usage: !shapegen <prompt words> [--n 1-4] [--temp 0-2]\n"
                        "Example: !shapegen black hair, white background"}

    proj = _project_dir()
    script = proj / "net_binned.py"
    model = proj / "model_bin.npz"
    if not script.exists():
        return {"type": "reply",
                "text": f"shape_prompt_ai not found at {proj}.\n"
                        "Set SHAPE_PROMPT_AI_DIR, or place the project folder next to EMLII.py."}
    if not model.exists():
        return {"type": "reply",
                "text": f"No trained model at {model}.\n"
                        "Train one first (shape_prompt_ai's README, step 3): "
                        "python net_binned.py train data_sharp"}

    n, temp = 1, 0.7
    m = re.search(r"--n\s+(\d+)", arg)
    if m:
        n = max(1, min(4, int(m.group(1))))
        arg = arg[:m.start()] + arg[m.end():]
    m = re.search(r"--temp\s+([\d.]+)", arg)
    if m:
        temp = max(0.0, min(2.0, float(m.group(1))))
        arg = arg[:m.start()] + arg[m.end():]
    prompt = arg.strip()
    if not prompt:
        return {"type": "reply", "text": "Give me some words to generate from, e.g. !shapegen black hair"}

    start = time.time()
    try:
        r = subprocess.run(
            [sys.executable, "net_binned.py", "gen", "model_bin.npz", prompt,
             "--n", str(n), "--temp", str(temp)],
            cwd=proj, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"type": "reply", "text": "Generation timed out after 120s."}

    if r.returncode != 0:
        return {"type": "reply", "text": "Generation failed:\n" + r.stderr.strip()[-500:]}

    # Copy results into EMLII's own data/files/ so they survive independently
    # of the shape_prompt_ai folder, and report exactly what was made. Only
    # pick up files this run actually wrote (mtime >= start) — otherwise a
    # run that silently produces nothing would report a leftover genb_N.png
    # from a previous command as if it were a fresh result.
    out_dir = Path(ctx["PATHS"]["files"])
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for i in range(1, n + 1):
        src = proj / f"genb_{i}.png"
        if src.exists() and src.stat().st_mtime >= start - 1:
            dest = out_dir / src.name
            shutil.copy(src, dest)
            made.append(str(dest))

    if not made:
        return {"type": "reply", "text": "Ran, but found no output images.\n" + r.stdout.strip()[-300:]}

    text = f'Generated {len(made)} image(s) from "{prompt}" (temp={temp}):\n' + "\n".join(made)
    return {"type": "reply", "text": text}
