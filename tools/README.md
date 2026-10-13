# EMLII tool plugins

Drop a `.py` file in here to add a new `!command` to EMLII. `EMLII.py`
itself never needs to change — it scans this folder at startup and wires
up whatever it finds.

## The contract

A tool file must define two things:

```python
COMMAND = "mytool"                 # triggers "!mytool <arg>"
HELP    = "One line for !help"     # shown in !help's command table

def run(arg, ctx):
    return {"type": "reply", "text": "..."}
```

- **`arg`** — everything the user typed after the command word, already
  stripped (`"!mytool  do a thing  "` → `arg == "do a thing"`).
- **`ctx`** — a small context dict:
  | key | what it is |
  |---|---|
  | `ctx["PATHS"]` | EMLII's storage folders, e.g. `ctx["PATHS"]["files"]` for `data/files/` |
  | `ctx["username"]` | the current user's name |
  | `ctx["fs"]` | the `!dirserver` state dict, if you need to check whether it's running |
- **Return value** — either a full result dict, or just a plain string
  (EMLII wraps it as `{"type": "reply", "text": ...}` for you). If you
  return a dict, `"type"` is required; `"reply"` with a `"text"` key is
  the safest choice since both the terminal and the web UI always know how
  to show it. Returning `"text"` plus `"link"` also works — the web UI
  shows it as a clickable link.

## Rules enforced automatically

- Files starting with `_` are ignored (use for shared helpers, e.g.
  `_common.py` imported by your other tools).
- A tool can't claim a word that's already a built-in command
  (`help`, `hash`, `crack`, `dirserver`, ...) — it's skipped with a
  message at startup instead of silently overriding core behaviour.
- If a tool fails to import, or is missing `COMMAND`/`run()`, it's skipped
  with a one-line message at startup — EMLII still starts normally.
- If `run()` raises an exception while handling a command, EMLII catches
  it and replies with the error instead of crashing.

## Minimal example

```python
# tools/greet.py
COMMAND = "greet"
HELP = "Say hi back"

def run(arg, ctx):
    return f"Hello {ctx['username']}, you said: {arg!r}"
```
Typing `!greet hows it going` now works, with nothing touched in `EMLII.py`.

## Included examples: `shape_generate.py` and `shape_fit.py`

Both wrap the separate **shape_prompt_ai** project (the shape-fitting +
caption-to-picture toolkit):

- `shape_generate.py` adds `!shapegen <prompt>`, which calls
  `net_binned.py gen` to turn text into a generated image, the same as
  running it by hand. Needs a trained `model_bin.npz` in the project
  folder first.
- `shape_fit.py` adds `!shapefit <image path>`, which calls
  `shape_fitter.py` directly to turn any image into a shape program —
  no training required.

See the comment at the top of each file for setup (where it expects the
project folder, and `SHAPE_PROMPT_AI_DIR` if you keep it somewhere else).

It's a good template for wrapping any other command-line tool as an EMLII
plugin: build the subprocess call, copy whatever output file you care
about into `ctx["PATHS"]["files"]`, and return a text summary with the
path.
