"""One launcher for every platform (Windows, macOS, Linux, Android/Termux, iOS a-Shell/Pythonista).

    python shapeprompt_cli.py doctor                         check Python/numpy/Pillow/platform
    python shapeprompt_cli.py image pic.jpg [--guess]        image -> prompt
    python shapeprompt_cli.py text "a girl on a beach" --style anime
    python shapeprompt_cli.py retrain --images images --tags tags --out my_weights.npz   (then: label --weights my_weights.npz)
    python shapeprompt_cli.py bridge --raw raw_images --data data_sharp [--vit W.npz]   (auto-captions for net_regression.py)
    python shapeprompt_cli.py batch --images my_images --out prompts [--recursive] [--guess] [--negative]
    python shapeprompt_cli.py label --images images --tags tags [--lan] [--open]
    python shapeprompt_cli.py prep|train|predict|check ...   pass-through to the dataset/training scripts
    python shapeprompt_cli.py test                           run all self-tests
Only needs Python 3.8+, numpy, Pillow (pip install -r requirements.txt).
"""
import os
import platform
import runpy
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = {"image": "image_to_prompt.py", "text": "text_to_prompt.py", "label": "label_web_tool.py", "batch": "batch_caption.py", "retrain": "tagger_retrain.py", "bridge": "caption_bridge.py",
           "prep": "tagger_prep_dataset.py", "train": "tagger_train.py", "predict": "tagger_predict.py", "check": "gradient_check.py"}


def doctor():
    ok = True
    print(f"platform : {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"python   : {sys.version.split()[0]}", "OK" if sys.version_info >= (3, 8) else "TOO OLD (need 3.8+)")
    ok &= sys.version_info >= (3, 8)
    for mod in ("numpy", "PIL"):
        try:
            m = __import__(mod)
            print(f"{mod:9s}: {getattr(m, '__version__', 'ok')}")
        except ImportError:
            print(f"{mod:9s}: MISSING -> pip install -r requirements.txt")
            ok = False
    try:
        import onnxruntime
        print(f"onnxrt   : {onnxruntime.__version__} (optional)")
    except ImportError:
        print("onnxrt   : not installed (optional, only for --wd14)")
    print("encoding :", sys.getdefaultencoding(), "| stdout:", getattr(sys.stdout, "encoding", "?"))
    return 0 if ok else 1


def run_tests():
    bad = 0
    for t in ("test_content_moderation.py", "test_text_to_prompt.py", "gradient_check.py"):
        r = subprocess.run([sys.executable, os.path.join(HERE, t)], cwd=HERE, stdout=subprocess.DEVNULL)
        print(("PASS " if r.returncode == 0 else "FAIL "), t)
        bad += r.returncode != 0
    return bad


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "doctor":
        return doctor()
    if cmd == "test":
        return run_tests()
    if cmd not in SCRIPTS:
        print("unknown command:", cmd)
        print(__doc__)
        return 2
    sys.path.insert(0, HERE)
    sys.argv = [SCRIPTS[cmd]] + rest
    try:
        runpy.run_path(os.path.join(HERE, SCRIPTS[cmd]), run_name="__main__")
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return 1
        return e.code or 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
