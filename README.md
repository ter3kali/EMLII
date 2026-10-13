# EMLII

A self-contained chatbot that learns by being taught, with a plugin system
for extending it — built as one Python file with no required dependencies
beyond the standard library (Flask is optional, only for the web UI).

Bundled with it: **shape_prompt_ai**, an offline image → prompt → generated
image toolkit (its own small Vision Transformer, trained from scratch in
plain NumPy — no PyTorch, no cloud, no external APIs) that plugs into EMLII
as two chat commands.

## Features

- **Terminal or web chat**, same brain either way — EMLII doesn't know or
  care which frontend is asking it something.
- **Learns on the fly**: ask it something it doesn't know, teach it the
  answer, it remembers forever (stored as plain text files, hashed by
  message).
- **Plugin system**: drop a `.py` file in `tools/` with a `COMMAND` and a
  `run()` function and it becomes a new `!command` automatically, on both
  frontends, with zero changes to `EMLII.py`.
- **Built-in tools**: hashing/cracking, Caesar cipher, a calculator, a LAN
  file server (`!dirserver`), plus whatever's in `tools/`.
- **shape_prompt_ai**, wired in as `!shapefit <image>` (fit a photo as a
  vector-ish "shape program") and `!shapegen <prompt>` (generate a new
  image from text, using your own trained model).

## Quick start

```bash
git clone <this repo>
cd <this repo>
pip install -r requirements.txt

# chat, in your terminal
python EMLII.py --terminal

# or as a web UI (open the printed URL in a browser)
python EMLII.py --web
```

shape_prompt_ai works standalone too, through one launcher:

```bash
cd shape_prompt_ai
python shapeprompt_cli.py doctor   # checks your environment
python shapeprompt_cli.py test     # runs its self-tests (incl. content-moderation tests)
python shapeprompt_cli.py image path/to/photo.jpg   # caption an image
python shapeprompt_cli.py text "a girl with long pink hair, beach, sunset"
```

See `shape_prompt_ai/README.md` for the full pipeline (dataset building →
tagging → training your own image generator) and `tools/README.md` for how
to write your own EMLII plugin.

## Project structure

```
EMLII.py              the chatbot - terminal + web frontends, plugin loader
tools/
  shape_fit.py         !shapefit  -> wraps shape_prompt_ai/shape_fitter.py
  shape_generate.py    !shapegen  -> wraps shape_prompt_ai/net_binned.py
  README.md             how to write your own tool
shape_prompt_ai/
  content_moderation.py        the safety layer every other script here calls into
  shape_fitter.py               fits a photo as a sequence of simple shapes
  image_to_prompt.py            image -> tags/prompt (basic / wd14 / your own trained tagger)
  text_to_prompt.py             free text -> a structured prompt
  vision_transformer.py         a small ViT, hand-written in NumPy, with its own backward pass
  tagger_*.py, label_web_tool.py  build and train your own tagging model
  net_regression.py, net_binned.py   caption -> shape-program generators
  shapeprompt_cli.py            one launcher for everything above
requirements.txt
```

## Safety

shape_prompt_ai runs every image/caption through `content_moderation.py`
before it's used anywhere (captioning, training-data prep, generation).
It's a starting point, not a finished system — see the moderation test
suite (`python shapeprompt_cli.py test`) for what it currently catches, and
treat its limits seriously before trusting it with anything you haven't
reviewed yourself.

## License

MIT - see `LICENSE`.
