"""
EMLII v2.0 — Dual Mode Edition
================================
Runs as either a terminal chatbot or a browser-based web app.

Usage:
    python EMLII.py              → interactive mode picker
    python EMLII.py --terminal   → terminal mode
    python EMLII.py --web        → web mode (default port 5000)
    python EMLII.py --web 8888   → web mode on port 8888

How EMLII learns:
    Every message is hashed with MD5. The hash becomes a filename in
    data/mem/. If that file exists, its contents are the response.
    If not, EMLII asks you to teach it — and saves what you say.
    Responses persist across restarts because they live on disk.
"""

# ── Standard library imports ──────────────────────────────────────────────────

import os           # File system: mkdir, chdir, path checks
import sys          # Command-line args (sys.argv), exit (sys.exit)
import hashlib      # Cryptographic hash functions: MD5, SHA1, SHA256, SHA512
import socket       # Network: used to detect the device's LAN IP address
import threading    # Run the file server in the background without freezing Flask
import string       # Pre-built character sets: ascii_lowercase, digits, etc.
import itertools    # itertools.product() generates character combinations for brute force
import re           # Validate that a client-supplied hash is really a hash (see api_teach)
import functools    # functools.partial() binds `directory=` onto the file-server handler
import importlib    # Import tool plugins from tools/ by filename, without hardcoding names
from datetime import datetime   # Current date/time for !date and history logging
from pathlib import Path        # Object-oriented path handling; mkdir with parents


# ── Optional import: Flask (only needed for web mode) ────────────────────────

try:
    from flask import Flask, jsonify, request as flask_request
    FLASK_AVAILABLE = True
except ImportError:
    # Flask not installed — web mode will show an install message
    FLASK_AVAILABLE = False


# ── Optional import: file server handler ─────────────────────────────────────

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
# SimpleHTTPRequestHandler: serves whatever the current directory is over HTTP
# ThreadingHTTPServer:      handles each connection in its own thread so multiple
#                           devices can download simultaneously without queuing


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

# ── Storage folders ───────────────────────────────────────────────────────────

PATHS = {
    'mem':       'data/mem/',       # One file per learned response (named by MD5 hash)
    'history':   'data/history/',   # Append-only log of every conversation
    'files':     'data/files/',     # Exported hash results from !hash
    'downloads': 'data/downloads/', # Reserved for future download features
}

# Create all folders at startup. parents=True creates intermediate directories.
# exist_ok=True means no error if the folder already exists.
for _p in PATHS.values():
    Path(_p).mkdir(parents=True, exist_ok=True)


# ── Session-ending words ──────────────────────────────────────────────────────

KILL = {"exit", "leave", "quit", "q"}

# Reserved words a tool plugin may not claim, so a plugin can never shadow
# a core command (accidentally or otherwise).
BUILTIN_COMMANDS = {
    "help", "version", "date", "calculate", "hash", "crack",
    "encrypt", "ecrypt", "decrypt", "dirserver", "stopserver",
    "exit", "quit", "leave", "q",
}


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL PLUGINS
# ═══════════════════════════════════════════════════════════════════════════════
# This is the extension point: new !commands can be added by dropping a file
# into tools/, with nothing in this file ever needing to change again. See
# tools/README.md for the full contract. Summary:
#
#   tools/mytool.py:
#       COMMAND = "mytool"                 # triggers "!mytool <arg>"
#       HELP    = "One line for !help"
#       def run(arg, ctx):
#           return {"type": "reply", "text": "..."}   # same result-dict shapes
#                                                       # EMLII.commands() returns
#
# TOOLS maps the lowercase command word to its loaded module.

TOOLS = {}          # {"mytool": <module>, ...} — populated by load_tools()
TOOLS_DIR = Path("tools")


def load_tools():
    """
    Import every *.py file in tools/ (skipping names starting with '_') and
    register the ones that expose COMMAND and a callable run(arg, ctx).

    A broken or malformed tool is reported and skipped rather than crashing
    EMLII — one bad plugin should never take down the whole app.
    """
    TOOLS_DIR.mkdir(exist_ok=True)
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))

    for f in sorted(TOOLS_DIR.glob("*.py")):
        if f.stem.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f.stem)
            importlib.reload(mod)   # picks up edits without restarting the picker loop
        except Exception as e:
            print(f"[tools] failed to load {f.name}: {e}")
            continue

        cmd = getattr(mod, "COMMAND", None)
        if not cmd or not callable(getattr(mod, "run", None)):
            print(f"[tools] {f.name} is missing COMMAND or run() — skipped")
            continue

        cmd = cmd.lower().lstrip("!")
        if cmd in BUILTIN_COMMANDS:
            print(f"[tools] {f.name}: '!{cmd}' is a built-in command, skipped")
            continue
        TOOLS[cmd] = mod

    if TOOLS:
        print(f"[tools] loaded: {', '.join('!' + c for c in TOOLS)}")
# Using a set (not a list) because set membership check `x in KILL` is O(1)
# regardless of size, while a list check is O(n). Sets use a hash table internally.


# ── ANSI terminal colour codes ────────────────────────────────────────────────
# These are escape sequences interpreted by the terminal to change text colour.
# \033 is the ESC character (octal). [ starts the colour code. m ends it.
# Only used in terminal mode; the web UI uses CSS colours instead.

GREEN  = '\033[92m'   # Bright green  — EMLII's voice
CYAN   = '\033[96m'   # Bright cyan   — hashes, URLs, highlights
YELLOW = '\033[93m'   # Yellow        — warnings, teach prompts
RED    = '\033[91m'   # Bright red    — errors
MUTED  = '\033[90m'   # Dark grey     — secondary info
BOLD   = '\033[1m'    # Bold text
RESET  = '\033[0m'    # Resets ALL colour/style back to terminal default


# ═══════════════════════════════════════════════════════════════════════════════
# HASH UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

# ── Common password dictionary ────────────────────────────────────────────────
# The crack function tries every word here before brute-forcing. Most real-world
# hash cracks succeed on this list because people use weak passwords.

WORDLIST = [
    "password", "123456", "qwerty", "admin", "letmein", "monkey", "dragon",
    "master", "sunshine", "princess", "welcome", "shadow", "superman",
    "michael", "football", "baseball", "abc123", "iloveyou", "trustno1",
    "passw0rd", "batman", "hello", "freedom", "login", "starwars", "access",
    "mustang", "hunter", "ranger", "joshua", "thomas", "robert", "daniel",
    "george", "jordan", "harley", "dakota", "cheese", "pepper", "butter",
    "cookie", "killer", "matrix", "buster", "ginger", "summer", "winter",
    "spring", "autumn", "purple", "orange", "yellow", "silver",
]


def do_hash(text, algo="md5"):
    """
    Hash a string with the specified algorithm and return the hex digest.

    Parameters:
        text  (str): The plaintext to hash.
        algo  (str): Algorithm name — 'md5', 'sha1', 'sha256', or 'sha512'.
                     Defaults to 'md5' if the name is unrecognised.

    Returns:
        str: Lowercase hex string, e.g. '5d41402abc4b2a76b9719d911017c592'

    How it works:
        Hash functions take any input and produce a fixed-length fingerprint.
        The same input always produces the same fingerprint (deterministic).
        You cannot reverse the fingerprint back to the input (one-way).
        Even changing one character completely changes the fingerprint.
    """
    # Map algorithm names to the actual hashlib constructor functions.
    # The functions are stored as values, NOT called yet (no parentheses).
    fns = {
        "md5":    hashlib.md5,
        "sha1":   hashlib.sha1,
        "sha256": hashlib.sha256,
        "sha512": hashlib.sha512,
    }

    # .get(algo, hashlib.md5) returns the function if found, or MD5 as fallback.
    # (text.encode()) converts the Python string to UTF-8 bytes — hash functions
    # require bytes, not strings.
    # .hexdigest() converts the raw binary hash output to a readable hex string.
    return fns.get(algo, hashlib.md5)(text.encode()).hexdigest()


def hash_all(text):
    """
    Hash a string with all four supported algorithms at once.

    Returns:
        dict: { 'md5': '...', 'sha1': '...', 'sha256': '...', 'sha512': '...' }

    Used by the !hash command to show all digests in one go rather than
    making the user run the command four times.
    """
    # Dict comprehension: creates a dict by iterating over algorithm names
    # and calling do_hash for each one. Equivalent to a for-loop building a dict.
    return {algo: do_hash(text, algo) for algo in ["md5", "sha1", "sha256", "sha512"]}


def crack_hash(target, verbose=False):
    """
    Attempt to find the original text that produced the given hash.

    Works in two phases:
        1. Dictionary attack: tries every word in WORDLIST against MD5, SHA1, SHA256.
        2. Brute force: tries every combination of a-z and 0-9 up to 3 characters.

    Parameters:
        target  (str):  The hex hash string to crack.
        verbose (bool): If True, prints progress to the terminal (used in terminal mode).

    Returns:
        dict: { 'found': True,  'text': 'password', 'algo': 'md5' }
           or { 'found': False }

    Why only 3-char brute force:
        36 chars (a-z + 0-9), length 3 = 36^3 = 46,656 combinations × 3 algos
        = ~140,000 hash operations. Fast enough for a web request.
        Length 4 would be 1.7 million operations — too slow for a live response.
    """
    # Normalise: strip accidental spaces and convert to lowercase so
    # 'ABC123' and 'abc123' compare equal.
    target = target.strip().lower()

    algos = ["md5", "sha1", "sha256"]

    # ── Phase 1: Dictionary attack ──
    for word in WORDLIST:
        for algo in algos:
            if do_hash(word, algo) == target:
                # Found a match — return immediately without brute forcing
                return {"found": True, "text": word, "algo": algo}

    # ── Phase 2: Brute force ──
    # string.ascii_lowercase = 'abcdefghijklmnopqrstuvwxyz'
    # string.digits          = '0123456789'
    chars = string.ascii_lowercase + string.digits

    for length in range(1, 4):   # Try lengths 1, 2, 3
        for combo in itertools.product(chars, repeat=length):
            # itertools.product('ab', repeat=2) → ('a','a'),('a','b'),('b','a'),('b','b')
            # ''.join(combo) converts the tuple ('a','b') into the string 'ab'
            word = ''.join(combo)

            if verbose:
                # \r returns the cursor to the start of the line so the next
                # print overwrites it — creating an in-place progress display.
                print(f"\r{MUTED}Trying: {word:<5}{RESET}", end='', flush=True)

            for algo in algos:
                if do_hash(word, algo) == target:
                    if verbose:
                        print()   # Move to new line after the \r progress display
                    return {"found": True, "text": word, "algo": algo}

    if verbose:
        print()
    return {"found": False}


# ═══════════════════════════════════════════════════════════════════════════════
# ENCRYPTION
# ═══════════════════════════════════════════════════════════════════════════════

def caesar(text, shift):
    """
    Encrypt or decrypt text using a Caesar cipher (character rotation).

    A Caesar cipher shifts each letter by `shift` positions in the alphabet.
    ROT-13 is a Caesar cipher with shift=13 (the most common variant because
    applying it twice returns the original: 13+13=26=full alphabet).

    Parameters:
        text  (str): The text to transform.
        shift (int): How many positions to shift. Positive = encrypt, negative = decrypt.
                     Use shift=13 for ROT-13 encrypt, shift=-13 for ROT-13 decrypt.

    Returns:
        str: The transformed text (non-letters pass through unchanged).

    Example:
        caesar('hello', 13)  → 'uryyb'
        caesar('uryyb', -13) → 'hello'

    How the maths works:
        'a' has ASCII code 97. Subtracting base (97) gives position 0.
        Adding shift and taking % 26 keeps the result in range 0-25.
        Adding base back converts to an ASCII code. chr() converts to a character.
    """
    out = []
    for ch in text:
        if ch.isalpha():
            # ord() converts a character to its ASCII integer code.
            # Upper and lower case have different base codes (65 vs 97),
            # so we pick the right base to keep case intact.
            base = ord('A') if ch.isupper() else ord('a')

            # Subtract base → position in alphabet (0-25)
            # Add shift     → shift position
            # % 26          → wrap around (position 25 + 1 = position 0 = 'a')
            # Add base      → back to ASCII code
            # chr()         → back to character
            out.append(chr((ord(ch) - base + shift) % 26 + base))
        else:
            # Numbers, spaces, punctuation — pass through unchanged
            out.append(ch)

    # Join the list of characters into a single string
    return ''.join(out)


# ═══════════════════════════════════════════════════════════════════════════════
# FILE SERVER  (from emserver.py)
# ═══════════════════════════════════════════════════════════════════════════════

# ── File server state ─────────────────────────────────────────────────────────
# A single dict holds everything about the current file server session.
# 'httpd' stores the actual server object so stop_fs() can call .shutdown() on it.

fs = {
    "running":   False,
    "port":      8080,
    "directory": str(Path.home()),  # Default to home dir; Path.home() = ~ resolved
    "httpd":     None,
}

# A mutex (mutual exclusion lock) protects the fs dict.
# Flask and the file server run on different threads simultaneously.
# Without the lock, both could read/write fs at the same time → corrupted data.
# Only one thread can hold the lock at a time.
fs_lock = threading.Lock()


def get_lan_ip():
    """
    Detect the device's local network (LAN) IP address.

    Returns:
        str: The LAN IP, e.g. '192.168.1.42'. Falls back to '127.0.0.1'.

    How the trick works:
        A UDP socket is created (SOCK_DGRAM = UDP, connectionless protocol).
        Calling .connect() on a UDP socket does NOT send any data.
        It just makes the OS decide which network interface it would use
        to reach the given address. Once the OS has decided, .getsockname()
        returns the local IP of that interface — which is your LAN IP.
        The target address (10.255.255.255) is non-routable and unreachable;
        it's only used to trigger the OS's routing decision.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # AF_INET = IPv4 address family
        # SOCK_DGRAM = UDP (we don't need a real connection, just routing info)
        s.connect(("10.255.255.255", 1))  # No data sent; OS just picks an interface
        ip = s.getsockname()[0]           # [0] = IP address, [1] would be port
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"  # Loopback fallback if no network available


def _run_fs(directory, port):
    """
    Start the HTTP file server and block until shutdown() is called.

    This function is designed to run inside a background thread.
    It blocks on serve_forever() — that's intentional because the calling
    thread (the background thread) has nothing else to do.

    Parameters:
        directory (str): The folder to serve.
        port      (int): The TCP port to listen on.
    """
    # Serve `directory` via the `directory=` keyword (supported since Python
    # 3.7) instead of os.chdir(). os.chdir() changes the *whole process's*
    # working directory, not just this thread's — every other relative path
    # in the app (data/mem/, data/history/, data/files/) would start
    # resolving inside whatever folder got served, silently redirecting
    # EMLII's own memory/history writes until the process restarts.
    SimpleHTTPRequestHandler.log_message = lambda *a: None  # silence request logging
    handler = functools.partial(SimpleHTTPRequestHandler, directory=directory)

    # ("", port) = listen on ALL network interfaces (empty string = 0.0.0.0).
    # This is what makes other devices on the LAN able to reach the server.
    httpd = ThreadingHTTPServer(("", port), handler)

    # Update shared state under the lock before serve_forever() blocks.
    with fs_lock:
        fs["httpd"]   = httpd
        fs["running"] = True

    # Block here, handling requests, until httpd.shutdown() is called
    # from another thread (i.e., when the user runs !stopserver).
    httpd.serve_forever()


def start_fs(directory, port):
    """
    Spawn a background thread that runs the file server.

    Parameters:
        directory (str): The folder to serve.
        port      (int): The TCP port to listen on.

    Why daemon=True:
        Daemon threads are killed automatically when the main program exits.
        Without it, the file server thread would keep the process alive even
        after you close the terminal or press Ctrl+C.
    """
    t = threading.Thread(
        target=_run_fs,         # The function the thread will call
        args=(directory, port), # Arguments to pass to _run_fs
        daemon=True,            # Die when the main thread dies
    )
    t.start()


def stop_fs():
    """
    Gracefully shut down the file server if it is running.

    httpd.shutdown() signals serve_forever() to exit its loop cleanly,
    allowing the background thread to finish naturally.
    The lock is held for the entire operation so no other thread reads
    stale state while the shutdown is in progress.
    """
    with fs_lock:
        if fs.get("httpd"):
            fs["httpd"].shutdown()  # Signals serve_forever() to stop
            fs["httpd"] = None
        fs["running"] = False


# ═══════════════════════════════════════════════════════════════════════════════
# EMLII CORE
# ═══════════════════════════════════════════════════════════════════════════════

class EMLII:
    """
    The central chatbot logic. Shared by both terminal and web modes.

    All methods return a result dict with a 'type' key. The two frontends
    (terminal renderer and Flask API) each interpret these dicts in their
    own way — terminal prints coloured text, web returns JSON.

    Result types:
        reply          → { type, text }
        needs_teaching → { type, hash, prompt }
        learned        → { type, text }
        help           → { type, commands }
        hash_result    → { type, input, results }
        crack_start    → { type, hash }
        crypt_result   → { type, original, output, mode, method }
        dirserver      → { type, url, directory }
        kill           → { type }
    """

    VERSION = "2.0"
    # Class variable — shared by all instances, accessed as EMLII.VERSION.
    # Defined here (not inside a method) so it's part of the class itself.


    @staticmethod
    def process(userchat, username="User"):
        """
        Main entry point. Route a message to commands or memory lookup.

        @staticmethod means the method doesn't receive 'self' or 'cls'.
        It's a plain function that lives inside the class for organisation.

        Parameters:
            userchat (str): What the user typed.
            username (str): The user's name (used in some responses).

        Returns:
            dict: A result dict describing what EMLII wants to say.
        """
        userchat = userchat.strip()

        # Empty message — nothing to do
        if not userchat:
            return None

        # Messages starting with ! are commands, not conversational input
        if userchat[0] == '!':
            return EMLII.commands(userchat, username)

        # ── Memory lookup ──
        # Hash the user's message. The MD5 hex digest becomes the filename.
        # This maps any input string to a unique, filesystem-safe filename.
        h = hashlib.md5(userchat.encode()).hexdigest()

        try:
            # Try to open the file named after this hash in the mem folder.
            # If it exists, EMLII has a learned response for this input.
            with open(PATHS['mem'] + h) as f:
                return {"type": "reply", "text": f.read()}

        except FileNotFoundError:
            # No file means EMLII has never seen this input before.
            # Return needs_teaching so the frontend can ask the user to teach it.
            return {"type": "needs_teaching", "hash": h, "prompt": userchat}


    @staticmethod
    def learn(h, response):
        """
        Save a new response to disk, keyed by the hash of the input.

        Parameters:
            h        (str): MD5 hex digest of the original user message.
            response (str): What EMLII should say next time it sees that message.

        Returns:
            dict: { type: 'learned', text: response }

        The file is named after the hash and stored in data/mem/.
        Next time process() sees the same input, it will hash it,
        find this file, and return its contents.
        """
        # 'w' mode creates the file if it doesn't exist, or overwrites it.
        with open(PATHS['mem'] + h, 'w') as f:
            f.write(response)
        return {"type": "learned", "text": response}


    @staticmethod
    def commands(userchat, username="User"):
        """
        Parse and execute a !command string.

        Splits the input into a command name and optional argument:
            '!hash hello world' → cmd='hash', arg='hello world'
            '!help'             → cmd='help',  arg=''

        If the command isn't one of the built-ins below, TOOLS is checked
        for a matching plugin loaded from tools/ (see load_tools()).

        Parameters:
            userchat (str): The full message starting with '!'.
            username (str): The current user's name, passed through to
                             plugins as part of their context dict.

        Returns:
            dict: A result dict appropriate to the command.
        """
        # Strip the leading '!' character
        raw = userchat[1:]

        # split(None, 1) splits on any whitespace, but AT MOST ONCE.
        # This keeps the argument intact even if it contains spaces.
        # '!hash hello world' → raw = 'hash hello world'
        #                     → parts = ['hash', 'hello world']
        parts = raw.split(None, 1)

        cmd = parts[0].lower().strip()               # Command name, normalised
        arg = parts[1].strip() if len(parts) > 1 else ""  # Everything after the command


        # ── !help ─────────────────────────────────────────────────────────────
        if cmd == 'help':
            # Returns a list of (command, description) tuples.
            # Both frontends iterate over this to display a table.
            return {"type": "help", "commands": [
                ("!help",              "Show all commands"),
                ("!version",           "EMLII version info"),
                ("!date",              "Current date & time"),
                ("!calculate <expr>",  "Evaluate a math expression"),
                ("!hash <text>",       "Hash with MD5 / SHA1 / SHA256 / SHA512"),
                ("!crack <hash>",      "Dictionary + brute-force crack"),
                ("!encrypt <text>",    "ROT-13 Caesar cipher encrypt"),
                ("!decrypt <text>",    "ROT-13 Caesar cipher decrypt"),
                ("!dirserver [path]",  "Start LAN file server on port 8080"),
                ("!stopserver",        "Stop the file server"),
                ("!exit / !quit",      "End session"),
            ] + [
                # Tool plugins add themselves here automatically — nothing
                # above needs editing when a new tools/*.py file shows up.
                (f"!{c}", getattr(mod, "HELP", "(no description)"))
                for c, mod in sorted(TOOLS.items())
            ]}


        # ── !version ──────────────────────────────────────────────────────────
        elif cmd == 'version':
            return {"type": "reply", "text": f"EMLII v{EMLII.VERSION} — Dual Mode Edition"}


        # ── !date ─────────────────────────────────────────────────────────────
        elif cmd == 'date':
            # datetime.now() gets the current local time.
            # .ctime() formats it as a human-readable string.
            return {"type": "reply", "text": f"Date: {datetime.now().ctime()}"}


        # ── !calculate ────────────────────────────────────────────────────────
        elif cmd == 'calculate':
            if not arg:
                return {"type": "reply", "text": "Usage: !calculate <expression>\nExample: !calculate 2 + 2 * 10"}
            try:
                # Security: only allow safe characters in the expression.
                # eval() can execute arbitrary Python code, which is dangerous.
                # Whitelisting digits, operators, brackets, and spaces blocks
                # injection like: eval("__import__('os').system('rm -rf /')")
                allowed = set('0123456789+-*/(). ')
                if not all(c in allowed for c in arg):
                    raise ValueError("Expression contains disallowed characters")

                # {"__builtins__": {}} removes all Python built-in functions
                # from eval's scope, adding another layer of protection even
                # if something slipped through the character whitelist.
                result = eval(arg, {"__builtins__": {}})
                return {"type": "reply", "text": f"{arg} = {result}"}
            except Exception:
                return {"type": "reply", "text": f"Could not evaluate: {arg}"}


        # ── !hash ─────────────────────────────────────────────────────────────
        elif cmd == 'hash':
            if not arg:
                return {"type": "reply", "text": "Usage: !hash <text>\nExample: !hash hello world"}
            # hash_all() returns all four algorithm results in one dict.
            # The frontend renders this as a table.
            return {"type": "hash_result", "input": arg, "results": hash_all(arg)}


        # ── !crack ────────────────────────────────────────────────────────────
        elif cmd == 'crack':
            if not arg:
                return {"type": "reply", "text": "Usage: !crack <hash>\nExample: !crack 5d41402abc4b2a76b9719d911017c592"}
            # Return crack_start instead of running the crack here.
            # This lets each frontend handle the blocking operation differently:
            # terminal runs it with verbose progress; web runs it in a fetch call.
            return {"type": "crack_start", "hash": arg}


        # ── !encrypt / !decrypt ───────────────────────────────────────────────
        elif cmd in ('encrypt', 'ecrypt'):
            if not arg:
                return {"type": "reply", "text": "Usage: !encrypt <text>"}
            return {
                "type":     "crypt_result",
                "original": arg,
                "output":   caesar(arg, 13),   # ROT-13 shift
                "mode":     "Encrypt",
                "method":   "ROT-13 (Caesar cipher)",
            }

        elif cmd == 'decrypt':
            if not arg:
                return {"type": "reply", "text": "Usage: !decrypt <text>"}
            return {
                "type":     "crypt_result",
                "original": arg,
                "output":   caesar(arg, -13),  # Reverse ROT-13
                "mode":     "Decrypt",
                "method":   "ROT-13 (Caesar cipher)",
            }


        # ── !dirserver ────────────────────────────────────────────────────────
        elif cmd == 'dirserver':
            # Check if already running — avoid starting a second server
            with fs_lock:
                if fs["running"]:
                    return {"type": "reply",
                            "text": f"Server already running → http://{get_lan_ip()}:{fs['port']}"}

            # Use the argument as the directory, or fall back to home
            directory = arg if arg else str(Path.home())

            if not os.path.isdir(directory):
                return {"type": "reply", "text": f"Directory not found: {directory}"}

            # Update state then start the background thread
            with fs_lock:
                fs["port"]      = 8080
                fs["directory"] = directory

            start_fs(directory, 8080)

            return {
                "type":      "dirserver",
                "url":       f"http://{get_lan_ip()}:8080",
                "directory": directory,
            }


        # ── !stopserver ───────────────────────────────────────────────────────
        elif cmd == 'stopserver':
            stop_fs()
            return {"type": "reply", "text": "File server stopped."}


        # ── !exit / !quit ─────────────────────────────────────────────────────
        elif cmd in KILL or cmd == 'exit':
            # The 'kill' type signals each frontend to end the session.
            # Terminal: breaks the while loop. Web: disables the input.
            return {"type": "kill"}


        # ── Tool plugins ──────────────────────────────────────────────────────
        elif cmd in TOOLS:
            ctx = {
                "PATHS":    PATHS,
                "username": username,
                "fs":       fs,            # read-only peek at the file-server state if a tool wants it
            }
            try:
                result = TOOLS[cmd].run(arg, ctx)
            except Exception as e:
                # A plugin raising an exception should never take EMLII down.
                return {"type": "reply", "text": f"'!{cmd}' failed: {e}"}
            if isinstance(result, dict) and result.get("type"):
                return result
            # Tools may return a plain string instead of a full result dict.
            return {"type": "reply", "text": str(result)}


        # ── Unknown command ───────────────────────────────────────────────────
        else:
            return {"type": "reply",
                    "text": f"Unknown command: !{cmd}\nType !help for a list."}


# ═══════════════════════════════════════════════════════════════════════════════
# TERMINAL FRONTEND
# ═══════════════════════════════════════════════════════════════════════════════

def print_header():
    """
    Print the EMLII ASCII art banner to the terminal.
    Only called once at the start of a terminal session.
    """
    print(f"""
{GREEN}{BOLD} ######  ##     ##  #       ###  ###
 #       # #   # #  #        #    #
 ######  #  # #  #  #        #    #
 #       #   #   #  #        #    #
 ######  #       #  ######  ###  ###
{RESET}
{MUTED} EMLII v{EMLII.VERSION} — Dual Mode Edition{RESET}
 {"─" * 40}
""")


def render_terminal(result, username):
    """
    Render a result dict as coloured terminal output.

    This is the terminal equivalent of the web UI's handle() function.
    Both receive the same dict shapes from EMLII.process() — they just
    display them differently.

    For 'needs_teaching', this function blocks on input() to get the
    response from the user inline. The web version instead shows a
    teach bar in the browser and handles it asynchronously.

    Parameters:
        result   (dict): Return value from EMLII.process() or EMLII.commands().
        username (str):  The current user's name.

    Returns:
        bool: True if the session should end (kill signal), False otherwise.
    """
    if not result:
        return False

    t = result.get('type')


    # ── Plain text reply ──────────────────────────────────────────────────────
    if t == 'reply':
        print(f"\n{GREEN}EMLII:{RESET} {result['text']}\n")


    # ── EMLII doesn't know — ask user to teach it ─────────────────────────────
    elif t == 'needs_teaching':
        print(f"\n{YELLOW}EMLII:{RESET} I don't know how to respond to \"{result['prompt']}\".")
        response = input(f"{YELLOW}     → Teach me (or press Enter to skip): {RESET}").strip()
        if response:
            EMLII.learn(result['hash'], response)
            print(f"\n{GREEN}EMLII:{RESET} {response}\n")
        else:
            print(f"{MUTED}     Skipped teaching.{RESET}\n")


    # ── Already just learned something (shouldn't normally appear in terminal) ─
    elif t == 'learned':
        print(f"\n{GREEN}EMLII:{RESET} {result['text']}\n")


    # ── Help table ────────────────────────────────────────────────────────────
    elif t == 'help':
        print(f"\n{MUTED}{'─' * 46}{RESET}")
        print(f"{GREEN} Available commands:{RESET}")
        print(f"{MUTED}{'─' * 46}{RESET}")
        for cmd, desc in result['commands']:
            # :<26 left-aligns the command name in a 26-character wide column
            print(f"  {CYAN}{cmd:<26}{RESET}{MUTED}{desc}{RESET}")
        print(f"{MUTED}{'─' * 46}{RESET}\n")


    # ── Hash results table ────────────────────────────────────────────────────
    elif t == 'hash_result':
        print(f"\n{MUTED} Input: \"{result['input']}\"{RESET}")
        print(f"{MUTED}{'─' * 46}{RESET}")
        for algo, h in result['results'].items():
            # :<8 pads the algorithm name to 8 characters for alignment
            print(f"  {MUTED}{algo.upper():<8}{RESET}{CYAN}{h}{RESET}")
        print(f"{MUTED}{'─' * 46}{RESET}\n")


    # ── Crack — run with terminal progress display ────────────────────────────
    elif t == 'crack_start':
        print(f"\n{YELLOW} Cracking hash...{RESET}")
        print(f"{MUTED} Dictionary attack → brute force (a-z, 0-9, up to 3 chars){RESET}\n")

        # verbose=True enables the \r progress line inside crack_hash
        d = crack_hash(result['hash'], verbose=True)

        if d['found']:
            print(f"\n{GREEN} Hash cracked!{RESET}")
            print(f"{MUTED}{'─' * 46}{RESET}")
            print(f"  {MUTED}Algorithm : {RESET}{d['algo'].upper()}")
            print(f"  {MUTED}Plaintext : {RESET}{GREEN}{BOLD}{d['text']}{RESET}")
            print(f"{MUTED}{'─' * 46}{RESET}\n")
        else:
            print(f"\n{RED} Could not crack hash.{RESET}")
            print(f"{MUTED} (Dictionary + 3-char brute force tried){RESET}\n")


    # ── Cipher results ────────────────────────────────────────────────────────
    elif t == 'crypt_result':
        print(f"\n{MUTED} {result['mode']} · {result['method']}{RESET}")
        print(f"{MUTED}{'─' * 46}{RESET}")
        print(f"  {MUTED}Input : {RESET}{result['original']}")
        print(f"  {MUTED}Output: {RESET}{YELLOW}{result['output']}{RESET}")
        print(f"{MUTED}{'─' * 46}{RESET}\n")


    # ── File server started ───────────────────────────────────────────────────
    elif t == 'dirserver':
        print(f"\n{GREEN} File server started!{RESET}")
        print(f"{MUTED}{'─' * 46}{RESET}")
        print(f"  {MUTED}Directory : {RESET}{result['directory']}")
        print(f"  {MUTED}LAN URL   : {RESET}{CYAN}{result['url']}{RESET}")
        print(f"\n{MUTED} Open this URL on any device on your Wi-Fi.{RESET}")
        print(f"{MUTED}{'─' * 46}{RESET}\n")


    # ── Session end ───────────────────────────────────────────────────────────
    elif t == 'kill':
        print(f"\n{MUTED} Session ended. Goodbye, {username}.{RESET}\n")
        return True   # Signal the main loop to break


    # ── Anything else (typically a tool plugin) ───────────────────────────────
    # Tools are only guaranteed to return 'type' + 'text'; print that if it's
    # there instead of silently showing nothing for an unrecognised type.
    elif result.get('text'):
        print(f"\n{GREEN}EMLII:{RESET} {result['text']}\n")

    return False  # Session continues


def run_terminal():
    """
    Main loop for terminal mode.

    Prints the header, asks for a username, then enters a read-eval-print
    loop: read input → pass to EMLII.process() → render result.

    Exits on:
        - !exit / !quit / 'exit' etc.  (kill signal from process())
        - Ctrl+C (KeyboardInterrupt)
        - Ctrl+D / end of input (EOFError)
    """
    print_header()

    # Get username — strip whitespace, default to 'User' if blank
    username = input(f"{GREEN}Enter your username: {RESET}").strip() or "User"

    print(f"\n{GREEN}EMLII:{RESET} Hello, {username}. I am EMLII v{EMLII.VERSION}.")
    print(f"{GREEN}EMLII:{RESET} Type a message to chat, or {CYAN}!help{RESET} for commands.\n")

    while True:
        try:
            # The prompt shows the username — matches the web UI's prompt symbol
            user_input = input(f"{CYAN}{username} ▶ {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            # Ctrl+C sends KeyboardInterrupt; Ctrl+D sends EOFError
            print(f"\n{MUTED} Session ended.{RESET}\n")
            break

        # Skip blank lines
        if not user_input:
            continue

        # Append to chat history file
        # 'a' mode appends without overwriting
        with open(PATHS['history'] + 'chat.txt', 'a') as f:
            f.write(f"[{datetime.now().ctime()}] {username}: {user_input}\n")

        # Process the input and render the result
        result = EMLII.process(user_input, username)

        # render_terminal returns True when a kill signal is received
        if render_terminal(result, username):
            break


# ═══════════════════════════════════════════════════════════════════════════════
# WEB FRONTEND  (Flask)
# ═══════════════════════════════════════════════════════════════════════════════

# Only define the Flask app if Flask is installed.
# This avoids a NameError crash when running in terminal-only environments.
if FLASK_AVAILABLE:

    app = Flask(__name__)
    # Flask(__name__) creates the web application. __name__ is the current module's
    # name — Flask uses it to locate templates and static files (not used here
    # since everything is embedded in the UI string).


    # ── /api/chat ─────────────────────────────────────────────────────────────
    @app.route('/api/chat', methods=['POST'])
    def api_chat():
        """
        Receive a message from the browser and return EMLII's response as JSON.

        The browser sends:  POST /api/chat  { "message": "hello", "username": "Teylan" }
        This function:      logs it, passes to EMLII.process(), returns the result dict.

        @app.route registers this function as the handler for POST /api/chat.
        methods=['POST'] means GET requests to this URL return 405 Method Not Allowed.
        """
        # get_json(silent=True) parses the body as JSON, returns None on failure.
        # `or {}` means if parsing fails (None), use an empty dict instead,
        # so the .get() calls below never raise AttributeError.
        data = flask_request.get_json(silent=True) or {}

        msg  = data.get('message', '').strip()
        user = data.get('username', 'User')

        # Return an empty reply for blank messages rather than crashing
        if not msg:
            return jsonify({"type": "reply", "text": ""})

        # Log every message with a timestamp
        with open(PATHS['history'] + 'chat.txt', 'a') as f:
            f.write(f"[{datetime.now().ctime()}] {user}: {msg}\n")

        result = EMLII.process(msg, user)

        # jsonify() converts the Python dict to a JSON HTTP response.
        # `or {"type": "reply", "text": ""}` handles the case where process()
        # returns None (empty input after stripping).
        return jsonify(result or {"type": "reply", "text": ""})


    # ── /api/teach ────────────────────────────────────────────────────────────
    @app.route('/api/teach', methods=['POST'])
    def api_teach():
        """
        Save a new learned response, called when the user fills in the teach bar.

        The browser sends: POST /api/teach  { "hash": "5d41...", "response": "Hi!" }
        """
        data = flask_request.get_json(silent=True) or {}
        h    = data.get('hash', '')
        resp = data.get('response', '').strip()

        if not h or not resp:
            return jsonify({"ok": False, "error": "Missing hash or response"})

        # `h` is used as a filename below (PATHS['mem'] + h). process() only
        # ever generates real MD5 hex digests, so anything else — e.g.
        # "../../../etc/whatever" — is either a bug on the client side or a
        # deliberate attempt to write outside data/mem/. Reject it.
        if not re.fullmatch(r"[0-9a-f]{32}", h):
            return jsonify({"ok": False, "error": "Invalid hash"})

        result = EMLII.learn(h, resp)

        # **result unpacks the learned dict into the outer dict.
        # { "ok": True, **{"type": "learned", "text": "Hi!"} }
        # → { "ok": True, "type": "learned", "text": "Hi!" }
        return jsonify({"ok": True, **result})


    # ── /api/crack ────────────────────────────────────────────────────────────
    @app.route('/api/crack', methods=['POST'])
    def api_crack():
        """
        Run crack_hash() and return the result as JSON.

        This is a separate route (not inside /api/chat) because cracking can
        take several seconds. The browser shows a loading indicator while it
        waits, then displays the result when this route responds.
        """
        data   = flask_request.get_json(silent=True) or {}
        target = data.get('hash', '').strip()

        if not target:
            return jsonify({"found": False, "error": "No hash provided"})

        # verbose=False because there's no terminal to print progress to
        return jsonify(crack_hash(target, verbose=False))


    # ── / (root) ──────────────────────────────────────────────────────────────
    @app.route('/')
    def index():
        """
        Serve the embedded HTML page when the browser visits the root URL.

        The entire UI is stored in the UI string below and returned here.
        There are no separate template files — everything is in this one file.
        """
        return UI


    # ── Embedded UI ───────────────────────────────────────────────────────────
    # r"""...""" is a raw string — backslashes are not treated as Python escape
    # sequences. This matters because the JS inside contains regex patterns
    # like /^!/ that use backslashes, which Python would otherwise misinterpret.
    # The entire HTML/CSS/JS page is one Python string, served by the / route.

    UI = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<!-- viewport: tells mobile browsers to use the device's actual width.
     Without this, phones render at ~980px desktop width and scale down. -->
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EMLII v2.0</title>
<style>

/* CSS custom properties (variables) defined on :root (the html element).
   Changing a value here updates it everywhere it's used via var(). */
:root {
  --bg:       #0d0f12;   /* Near-black page background */
  --surface:  #131518;   /* Slightly lighter — cards, header, input bar */
  --border:   #1d2028;   /* Subtle dividers between elements */
  --green:    #00e676;   /* EMLII's voice colour */
  --green-lo: #00e67614; /* Same green at ~8% opacity (14 hex = 20 decimal / 255) */
  --cyan:     #00c8b4;   /* Hashes, URLs, user badge */
  --yellow:   #f0c040;   /* Teach bar, cipher output */
  --red:      #e05555;   /* Errors */
  --text:     #c8d0e0;   /* Primary body text */
  --muted:    #45506a;   /* Secondary text, labels */
  --mono: 'JetBrains Mono','Fira Mono','Courier New',monospace;
}

/* Universal reset: apply to every element and its ::before / ::after pseudo-elements.
   box-sizing: border-box makes width/height include padding and border,
   not add to them (the default content-box causes confusing layout math).
   Zeroing margin and padding removes browser default spacing. */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

/* overflow: hidden on the root elements prevents the page itself from scrolling.
   The chat area scrolls internally. This keeps the input bar pinned to the bottom. */
html, body {
  height: 100%;
  background: var(--bg);
  color: var(--text);
  font-family: var(--mono);
  font-size: 13px;
  overflow: hidden;
}

/* ── App shell ──────────────────────────────────────────────────────────── */

/* flex + flex-direction: column stacks children (topbar, chat, teach-bar, input-bar)
   vertically. height: 100vh fills the entire viewport height. */
.app { display: flex; flex-direction: column; height: 100vh; }

/* ── Header / top bar ───────────────────────────────────────────────────── */

/* flex-shrink: 0 prevents the header from shrinking when the chat fills up.
   justify-content: space-between pushes the logo left and the controls right. */
.topbar {
  flex-shrink: 0;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 10px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.logo { font-size: 15px; font-weight: 700; color: var(--green); letter-spacing: 0.12em; }
.logo-ver { color: var(--muted); font-size: 11px; margin-top: 2px; letter-spacing: 0.1em; }
.top-right { display: flex; align-items: center; gap: 12px; }
.session-tag { font-size: 11px; color: var(--muted); }
.session-tag b { color: var(--cyan); font-weight: 400; }
.ico-btn {
  background: none;
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--muted);
  font-family: var(--mono);
  font-size: 11px;
  padding: 4px 10px;
  cursor: pointer;
  /* transition animates the property changes on hover over 150ms */
  transition: all 0.15s;
}
.ico-btn:hover { border-color: var(--green); color: var(--green); background: var(--green-lo); }

/* ── Chat message area ──────────────────────────────────────────────────── */

/* flex: 1 makes the chat take all remaining vertical space after the fixed-height
   header and input bar. overflow-y: auto adds a scrollbar when content overflows. */
.chat {
  flex: 1;
  overflow-y: auto;
  padding: 16px 16px 8px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* ── Individual message bubbles ─────────────────────────────────────────── */

.msg { display: flex; flex-direction: column; gap: 3px; }
.msg.user { align-items: flex-end; }    /* User messages: right-aligned */
.msg.bot  { align-items: flex-start; } /* EMLII messages: left-aligned */
.msg.sys  { align-items: center; }     /* System notices: centred */

/* Sender label above the bubble */
.who { font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted); }
.msg.bot .who { color: var(--green); }

/* The bubble itself:
   max-width: min(88%, 620px) — never wider than 88% of the screen or 620px.
   min() is a CSS function picking the smaller value.
   white-space: pre-wrap — respects \n characters AND wraps long lines.
   word-break: break-all — forces long hash strings to wrap instead of overflow. */
.bubble {
  max-width: min(88%, 620px);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 9px 13px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}
.msg.user .bubble { background: #141e2c; border-color: #1e2f46; }
.msg.bot  .bubble { color: var(--green); }
/* System messages: no background, dashed border, centred muted text */
.msg.sys .bubble {
  background: transparent;
  border-style: dashed;
  color: var(--muted);
  font-size: 11px;
  text-align: center;
}

/* ── Specialised message content ────────────────────────────────────────── */

.sub-label {
  font-size: 10px; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 8px;
}

/* Hash results table */
.hash-tbl { border-collapse: collapse; width: 100%; }
.hash-tbl td { padding: 4px 6px; border-bottom: 1px solid var(--border); }
.hash-tbl td:first-child { color: var(--muted); font-size: 11px; white-space: nowrap; padding-right: 14px; }
.hash-tbl td:last-child { color: var(--cyan); font-size: 11px; word-break: break-all; }

/* Help / command table */
.cmd-tbl { border-collapse: collapse; width: 100%; }
.cmd-tbl tr { border-bottom: 1px solid var(--border); }
.cmd-tbl td { padding: 5px 6px; }
.cmd-tbl td:first-child { color: var(--cyan); white-space: nowrap; padding-right: 16px; }
.cmd-tbl td:last-child { color: var(--muted); }

/* Cipher before/after boxes */
.crypt-box { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; margin-top: 6px; }
.crypt-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 3px; }
.crypt-val { color: var(--yellow); word-break: break-all; }

.server-link { color: var(--cyan); text-decoration: none; }
.server-link:hover { text-decoration: underline; }

/* ── Teach bar ───────────────────────────────────────────────────────────── */

/* Starts hidden (display: none). Adding the .open class switches it to
   display: flex. JavaScript adds/removes .open to show/hide the bar. */
.teach-bar {
  flex-shrink: 0;
  display: none;
  background: #18140a;
  border-top: 2px solid var(--yellow);
  padding: 10px 14px;
  flex-direction: column;
  gap: 8px;
}
.teach-bar.open { display: flex; }
.teach-prompt { font-size: 11px; color: var(--yellow); line-height: 1.5; }
.teach-row { display: flex; gap: 8px; }

/* ── Input bar ───────────────────────────────────────────────────────────── */

.input-bar {
  flex-shrink: 0;
  background: var(--surface);
  border-top: 1px solid var(--border);
  padding: 11px 14px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.prompt-sym { color: var(--green); white-space: nowrap; font-size: 13px; }
/* Transparent background on the input so it blends with the bar */
.chat-in {
  flex: 1;
  background: transparent;
  border: none;
  outline: none;
  color: var(--text);
  font-family: var(--mono);
  font-size: 13px;
  caret-color: var(--green); /* The blinking cursor colour */
}
.chat-in::placeholder { color: var(--muted); }
.chat-in:disabled { opacity: 0.3; }
.send-btn {
  background: none; border: 1px solid var(--border); border-radius: 5px;
  color: var(--muted); font-family: var(--mono); font-size: 11px;
  padding: 5px 10px; cursor: pointer; transition: all 0.15s;
}
.send-btn:hover { border-color: var(--green); color: var(--green); }

/* ── Shared input / button styles ────────────────────────────────────────── */

.t-in {
  flex: 1; background: var(--bg); border: 1px solid var(--border);
  border-radius: 5px; padding: 7px 11px; color: var(--text);
  font-family: var(--mono); font-size: 13px; outline: none;
}
.t-in:focus { border-color: var(--green); }
.btn { border: none; border-radius: 5px; padding: 7px 14px; font-family: var(--mono); font-size: 12px; font-weight: 600; cursor: pointer; transition: opacity 0.15s; }
.btn:active { opacity: 0.7; } /* Slight dim when clicked */
.btn-g { background: var(--green); color: var(--bg); }
.btn-m { background: var(--border); color: var(--muted); }

/* ── Setup / username screen ─────────────────────────────────────────────── */

/* position: fixed covers the entire screen. z-index: 100 places it above everything.
   Removed once the user enters their name. */
.setup {
  position: fixed; inset: 0; background: var(--bg); z-index: 100;
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 24px; padding: 24px;
}
/* clamp(min, preferred, max): font scales with viewport but stays in range.
   white-space: pre preserves all ASCII art spacing exactly as written. */
.setup-art {
  color: var(--green);
  font-size: clamp(9px, 2.2vw, 13px);
  white-space: pre;
  line-height: 1.55;
  text-align: center;
  letter-spacing: 0.05em;
}
.setup-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 22px; width: 100%; max-width: 360px; }
.setup-title { color: var(--muted); font-size: 11px; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.12em; }
.setup-row { display: flex; gap: 8px; }
.setup-foot { color: var(--muted); font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; }

/* ── Slide-out help panel ────────────────────────────────────────────────── */

/* Starts 300px off the right edge (invisible). Adding .open moves right to 0.
   CSS transition animates the change smoothly over 220ms with ease curve. */
.help-panel {
  position: fixed; top: 0; right: -300px; width: 290px; height: 100%;
  background: var(--surface); border-left: 1px solid var(--border);
  z-index: 50; transition: right 0.22s ease;
  overflow-y: auto; padding: 18px;
}
.help-panel.open { right: 0; }
.hp-title { color: var(--green); font-size: 12px; margin-bottom: 14px; display: flex; justify-content: space-between; align-items: center; }
.hp-close { background: none; border: none; color: var(--muted); font-size: 18px; cursor: pointer; line-height: 1; }
.hp-close:hover { color: var(--text); }
.hp-note { margin-top: 14px; color: var(--muted); font-size: 11px; line-height: 1.7; }

/* ── Loading animation (three bouncing dots) ─────────────────────────────── */

/* @keyframes defines the animation sequence.
   0% and 100% are the same (full opacity) so it loops seamlessly.
   50% is half-dimmed. */
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.2} }
.dots { display: inline-flex; gap: 4px; vertical-align: middle; }
.dots span { width: 5px; height: 5px; border-radius: 50%; background: var(--green); animation: blink 1s infinite; }
/* nth-child selectors apply staggered delays creating the wave effect */
.dots span:nth-child(2) { animation-delay: .2s; }
.dots span:nth-child(3) { animation-delay: .4s; }

/* ── Custom scrollbar (WebKit/Chromium/Android browsers) ─────────────────── */
::-webkit-scrollbar { width: 3px; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

</style>
</head>
<body>

<!-- ══ SETUP SCREEN ════════════════════════════════════════════════════════ -->
<!-- Covers the full page until the user enters a username. -->
<div class="setup" id="setup">

  <!-- <pre> preserves whitespace/newlines exactly — needed for ASCII art. -->
  <pre class="setup-art"> ######  ##     ##  #       ###  ###
 #       # #   # #  #        #    #
 ######  #  # #  #  #        #    #
 #       #   #   #  #        #    #
 ######  #       #  ######  ###  ###</pre>

  <div class="setup-card">
    <div class="setup-title">// identify yourself to begin</div>
    <div class="setup-row">
      <!-- autofocus: browser gives keyboard focus to this input on load.
           maxlength: enforced by HTML, no JS needed. -->
      <input class="t-in" id="unameIn" placeholder="username" maxlength="20" autofocus>
      <button class="btn btn-g" onclick="startSession()">→</button>
    </div>
  </div>

  <div class="setup-foot">emlii v2.0 · dual mode · created by teylan</div>
</div>

<!-- ══ MAIN APP ════════════════════════════════════════════════════════════ -->
<!-- Hidden initially (display:none). Revealed by startSession(). -->
<div class="app" id="mainApp" style="display:none">

  <!-- Top bar: logo left, session info + help button right -->
  <div class="topbar">
    <div>
      <div class="logo">[ EMLII ]</div>
      <div class="logo-ver">v2.0 · web edition</div>
    </div>
    <div class="top-right">
      <div class="session-tag">session: <b id="unameShow">—</b></div>
      <button class="ico-btn" onclick="toggleHelp()">!help</button>
    </div>
  </div>

  <!-- Chat messages are appended here by JavaScript -->
  <div class="chat" id="chat"></div>

  <!-- Teach bar: slides in when EMLII doesn't know a response.
       Starts with display:none; .open class switches to display:flex. -->
  <div class="teach-bar" id="teachBar">
    <div class="teach-prompt" id="teachPrompt">
      EMLII: I don't know how to respond. Teach me:
    </div>
    <div class="teach-row">
      <input class="t-in" id="teachIn" placeholder="Enter the response EMLII should give…">
      <button class="btn btn-g" onclick="submitTeach()">Save</button>
      <button class="btn btn-m" onclick="cancelTeach()">Skip</button>
    </div>
  </div>

  <!-- Input bar: fixed at bottom, always visible -->
  <div class="input-bar">
    <span class="prompt-sym" id="promptSym">▶</span>
    <!-- autocomplete/autocorrect/spellcheck all disabled: they break !commands -->
    <input class="chat-in" id="chatIn"
      placeholder="type a message or !command…"
      autocomplete="off" autocorrect="off" spellcheck="false">
    <button class="send-btn" onclick="send()">send</button>
  </div>

</div>

<!-- ══ HELP PANEL (slide-out) ═════════════════════════════════════════════ -->
<!-- Starts off-screen (right: -300px). .open class slides it in. -->
<div class="help-panel" id="helpPanel">
  <div class="hp-title">
    <span>Commands</span>
    <button class="hp-close" onclick="toggleHelp()">✕</button>
  </div>
  <table class="cmd-tbl">
    <tr><td>!help</td><td>List all commands</td></tr>
    <tr><td>!version</td><td>Version info</td></tr>
    <tr><td>!date</td><td>Current date & time</td></tr>
    <tr><td>!calculate &lt;expr&gt;</td><td>Evaluate math</td></tr>
    <tr><td>!hash &lt;text&gt;</td><td>MD5/SHA1/SHA256/SHA512</td></tr>
    <tr><td>!crack &lt;hash&gt;</td><td>Dictionary + brute-force</td></tr>
    <tr><td>!encrypt &lt;text&gt;</td><td>ROT-13 encrypt</td></tr>
    <tr><td>!decrypt &lt;text&gt;</td><td>ROT-13 decrypt</td></tr>
    <tr><td>!dirserver [path]</td><td>Start LAN file server</td></tr>
    <tr><td>!stopserver</td><td>Stop file server</td></tr>
    <tr><td>!exit / !quit</td><td>End session</td></tr>
  </table>
  <div class="hp-note">
    EMLII learns from you.<br>
    When it doesn't know something,<br>
    type the response to teach it.
  </div>
</div>


<!-- ══ JAVASCRIPT ══════════════════════════════════════════════════════════ -->
<script>

// ── Module-level state ──────────────────────────────────────────────────────
// These variables persist for the entire page session (until browser close/refresh).
let username = '';    // Set once in startSession(), used in every message
let pending  = null;  // { hash, prompt } when EMLII is waiting to be taught
let msgId    = 0;     // Counter: incremented for each message to give unique DOM ids


// ── startSession() ──────────────────────────────────────────────────────────
// Called when the user submits the username form.
// Transitions from the setup screen to the main app.
function startSession() {
  const val = document.getElementById('unameIn').value.trim();
  if (!val) return; // Do nothing if the field is blank

  username = val;

  // Hide setup screen, reveal main app by toggling display property
  document.getElementById('setup').style.display   = 'none';
  document.getElementById('mainApp').style.display = 'flex';

  // Update the username display in the header and the prompt symbol
  document.getElementById('unameShow').textContent = username;
  document.getElementById('promptSym').textContent = username + ' ▶';

  // Move keyboard focus to the chat input so the user can start typing
  document.getElementById('chatIn').focus();

  // Show the welcome message as EMLII's first message
  botMsg(`Hello, ${username}.\nI am EMLII v2.0 — Dual Mode Edition.\nType a message to chat, or !help to see commands.`);
}

// Allow pressing Enter on the username input instead of clicking the button.
// addEventListener attaches an event handler. 'keydown' fires on every key press.
// The arrow function checks if the pressed key is Enter and calls startSession().
document.getElementById('unameIn').addEventListener('keydown', e => {
  if (e.key === 'Enter') startSession();
});


// ── toggleHelp() ────────────────────────────────────────────────────────────
// Adds or removes the 'open' class on the help panel, triggering the CSS
// slide animation (right: -300px → right: 0 animated by transition).
function toggleHelp() {
  document.getElementById('helpPanel').classList.toggle('open');
}


// ── Event listeners for the two inputs ──────────────────────────────────────
// Enter on the chat input → send the message
// Enter on the teach input → submit the taught response
document.getElementById('chatIn').addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
document.getElementById('teachIn').addEventListener('keydown', e => { if (e.key === 'Enter') submitTeach(); });


// ── send() ──────────────────────────────────────────────────────────────────
// Reads the chat input, shows the user's message, sends it to the server,
// shows a loading indicator while waiting, then handles the response.
// async: marks the function as asynchronous so it can use await inside.
async function send() {
  const inp = document.getElementById('chatIn');
  const msg = inp.value.trim();
  if (!msg) return;   // Ignore blank messages
  inp.value = '';     // Clear input immediately — don't wait for the response

  userMsg(msg);       // Show the user's message in the chat right away

  // Local kill-word check: handle exit without a round trip to the server.
  // replace(/^!/, '') strips a leading ! so both 'exit' and '!exit' work.
  const lower = msg.replace(/^!/, '').toLowerCase();
  if (['exit', 'leave', 'quit', 'q'].includes(lower)) {
    botMsg(`Goodbye, ${username}. Session ended.`);
    // Disable the input so no more messages can be sent
    inp.disabled = true;
    document.querySelector('.send-btn').disabled = true;
    return;
  }

  // Show loading dots while waiting for the server
  const lid = loadingMsg();

  try {
    // fetch() sends an HTTP request. await pauses THIS function until the
    // response arrives, but does NOT freeze the page — the browser stays
    // responsive because async functions yield control while awaiting.
    const r = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // JSON.stringify converts the JS object to a JSON string for the request body.
      // { message: msg, username } is shorthand for { message: msg, username: username }
      // — when key name matches variable name, you can write it once.
      body: JSON.stringify({ message: msg, username }),
    });

    removeMsg(lid);              // Remove the loading dots
    handle(await r.json());      // r.json() parses the response body as JSON
  } catch (e) {
    // fetch throws if there's a network error (server down, no connection, etc.)
    removeMsg(lid);
    sysMsg('Connection error — is the server running?');
  }
}


// ── handle(d) ───────────────────────────────────────────────────────────────
// Central dispatcher: reads d.type and calls the right render function.
// d is the parsed JSON object from the server — same shape as the Python dicts
// returned by EMLII.process() and EMLII.commands().
function handle(d) {
  if (!d || !d.type) return;  // Guard against malformed responses

  switch (d.type) {

    case 'reply':
      botMsg(d.text);
      break;

    case 'kill':
      // !exit was processed server-side — end the session
      botMsg(`Goodbye, ${username}. Session ended.`);
      document.getElementById('chatIn').disabled = true;
      break;

    case 'needs_teaching':
      // EMLII doesn't know this input — store the hash and show the teach bar
      pending = { hash: d.hash, prompt: d.prompt };
      document.getElementById('teachPrompt').textContent =
        `EMLII: I don't know how to respond to "${d.prompt}". Teach me:`;
      document.getElementById('teachBar').classList.add('open');
      document.getElementById('teachIn').focus();
      break;

    case 'learned':
      // Teach flow completed — show what EMLII now knows
      botMsg(d.text);
      break;

    case 'help':
      helpMsg(d.commands);
      break;

    case 'hash_result':
      hashMsg(d);
      break;

    case 'crack_start':
      // Run the crack asynchronously — it may take several seconds
      doCrack(d.hash);
      break;

    case 'crypt_result':
      cryptMsg(d);
      break;

    case 'dirserver':
      // Show the URL as a clickable link
      botMsg(
        `File server started!\nDirectory: ${d.directory}\n\nShare this URL on your Wi-Fi:\n${d.url}`,
        { link: d.url }
      );
      break;

    default:
      // Unknown type (typically a tool plugin) — show text and an optional
      // link if the result has them, instead of silently showing nothing.
      if (d.text) botMsg(d.text, d.link ? { link: d.link } : undefined);
  }
}


// ── submitTeach() ────────────────────────────────────────────────────────────
// Called when the user saves a response in the teach bar.
// POSTs the hash and response to /api/teach, then closes the teach bar.
async function submitTeach() {
  const val = document.getElementById('teachIn').value.trim();
  if (!val || !pending) return;

  document.getElementById('teachIn').value = '';
  document.getElementById('teachBar').classList.remove('open');

  const r = await fetch('/api/teach', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ hash: pending.hash, response: val }),
  });
  const d = await r.json();
  pending = null;   // Clear the pending state

  if (d.ok) {
    sysMsg('✓ Response saved');
    botMsg(val);    // Show what EMLII now knows
  }
}


// ── cancelTeach() ────────────────────────────────────────────────────────────
// Dismiss the teach bar without saving anything.
function cancelTeach() {
  document.getElementById('teachBar').classList.remove('open');
  pending = null;
  sysMsg('Teaching skipped');
}


// ── doCrack(hash) ────────────────────────────────────────────────────────────
// Sends the hash to /api/crack and waits for the result.
// Separate from send() because cracking can take several seconds —
// displaying a dedicated loading message makes that clear.
async function doCrack(hash) {
  const lid = loadingMsg('Cracking hash…');
  try {
    const r = await fetch('/api/crack', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hash }),
    });
    removeMsg(lid);
    const d = await r.json();

    if (d.found) {
      botMsg(`Hash cracked!\n\nAlgorithm : ${d.algo.toUpperCase()}\nPlaintext : ${d.text}`);
    } else {
      botMsg('Could not crack hash.\n(Dictionary + 3-char brute force tried)');
    }
  } catch (e) {
    removeMsg(lid);
    sysMsg('Crack failed: connection error');
  }
}


// ── DOM helper: mkMsg(cls) ───────────────────────────────────────────────────
// Creates a new empty message div, appends it to the chat, and returns it.
// cls: CSS class string — 'user', 'bot', or 'sys'
function mkMsg(cls) {
  // ++msgId increments THEN uses the value; so first call = 1, not 0
  const id   = 'm' + (++msgId);
  const chat = document.getElementById('chat');
  const el   = document.createElement('div');  // Creates a real DOM <div>
  el.className = 'msg ' + cls;
  el.id        = id;
  chat.appendChild(el);                         // Inserts at end of chat
  chat.scrollTop = chat.scrollHeight;           // Scroll to bottom
  return { id, el, chat };
}

// Scroll chat to the very bottom (called after modifying message content)
function scrollChat() {
  const c = document.getElementById('chat');
  c.scrollTop = c.scrollHeight;
}


// ── userMsg(text) ────────────────────────────────────────────────────────────
// Add a message bubble on the right side (user's message).
// esc() prevents XSS — if the user types HTML tags, they display as text.
function userMsg(text) {
  const { el } = mkMsg('user');
  el.innerHTML = `
    <div class="who">${esc(username)}</div>
    <div class="bubble">${esc(text)}</div>`;
  scrollChat();
}


// ── botMsg(text, extra) ──────────────────────────────────────────────────────
// Add a message bubble on the left side (EMLII's response).
// extra: optional { link: 'http://...' } to append a clickable link after the text.
function botMsg(text, extra) {
  const { el } = mkMsg('bot');
  let inner = `<div class="bubble">${esc(text)}`;
  if (extra && extra.link) {
    // Only the link is rendered as real HTML — the text itself is escaped.
    inner += `\n<a class="server-link" href="${esc(extra.link)}" target="_blank">${esc(extra.link)}</a>`;
  }
  inner += `</div>`;
  el.innerHTML = `<div class="who">EMLII</div>${inner}`;
  scrollChat();
}


// ── sysMsg(text) ────────────────────────────────────────────────────────────
// Add a centred, muted system notice (not EMLII speaking, not the user).
// Used for: ✓ Response saved, Teaching skipped, Connection error, etc.
function sysMsg(text) {
  const { el } = mkMsg('sys');
  el.innerHTML = `<div class="bubble">${esc(text)}</div>`;
  scrollChat();
}


// ── loadingMsg(label) ────────────────────────────────────────────────────────
// Show an animated three-dot loading indicator as an EMLII message.
// Returns the message's id so removeMsg() can delete it later.
function loadingMsg(label = '') {
  const { id, el } = mkMsg('bot');
  el.innerHTML = `
    <div class="who">EMLII</div>
    <div class="bubble">${label ? esc(label) + ' ' : ''}<span class="dots"><span></span><span></span><span></span></span></div>`;
  scrollChat();
  return id;  // Caller stores this to call removeMsg(id) later
}


// ── removeMsg(id) ────────────────────────────────────────────────────────────
// Remove a message element from the DOM by its id.
// `if (el)` guards against the case where it's already been removed.
function removeMsg(id) {
  const el = document.getElementById(id);
  if (el) el.remove();
}


// ── helpMsg(commands) ────────────────────────────────────────────────────────
// Render a help result (list of [cmd, desc] pairs) as a table inside a bubble.
function helpMsg(commands) {
  const { el } = mkMsg('bot');
  // .map() transforms each [cmd, desc] pair into a <tr> string.
  // .join('') concatenates all the strings into one.
  const rows = commands.map(([c, d]) =>
    `<tr><td>${esc(c)}</td><td>${esc(d)}</td></tr>`
  ).join('');
  el.innerHTML = `
    <div class="who">EMLII</div>
    <div class="bubble">
      <div class="sub-label">Available commands</div>
      <table class="cmd-tbl">${rows}</table>
    </div>`;
  scrollChat();
}


// ── hashMsg(d) ───────────────────────────────────────────────────────────────
// Render a hash_result as a table of algorithm → digest rows.
// Object.entries() converts { md5: '...', sha1: '...' } to [['md5','...'], ['sha1','...']]
function hashMsg(d) {
  const { el } = mkMsg('bot');
  const rows = Object.entries(d.results).map(([algo, h]) =>
    `<tr><td>${algo.toUpperCase()}</td><td>${h}</td></tr>`
  ).join('');
  el.innerHTML = `
    <div class="who">EMLII</div>
    <div class="bubble">
      <div class="sub-label">Input: "${esc(d.input)}"</div>
      <table class="hash-tbl">${rows}</table>
    </div>`;
  scrollChat();
}


// ── cryptMsg(d) ──────────────────────────────────────────────────────────────
// Render an encrypt/decrypt result showing input and output side by side.
function cryptMsg(d) {
  const { el } = mkMsg('bot');
  el.innerHTML = `
    <div class="who">EMLII</div>
    <div class="bubble">
      <div class="sub-label">${esc(d.mode)} · ${esc(d.method)}</div>
      <div class="crypt-box">
        <div class="crypt-label">Input</div>
        <div>${esc(d.original)}</div>
      </div>
      <div class="crypt-box" style="margin-top:7px">
        <div class="crypt-label">Output</div>
        <div class="crypt-val">${esc(d.output)}</div>
      </div>
    </div>`;
  scrollChat();
}


// ── esc(s) ───────────────────────────────────────────────────────────────────
// HTML-escape a string to prevent XSS (cross-site scripting).
// If a user types <script>alert('x')<\/script>, the browser would execute it
// if we put it in innerHTML directly. Escaping converts special characters
// to their HTML entity equivalents so they display as text, not code.
// & must be replaced FIRST — otherwise the & in &lt; would get double-escaped.
// /g flag = global replace (all occurrences, not just the first)
function esc(s) {
  return String(s)
    .replace(/&/g,  '&amp;')   // & → &amp;
    .replace(/</g,  '&lt;')    // < → &lt;
    .replace(/>/g,  '&gt;')    // > → &gt;
    .replace(/"/g,  '&quot;'); // " → &quot;
}

</script>
</body>
</html>"""


# ── run_web() ─────────────────────────────────────────────────────────────────
def run_web(port=5000):
    """
    Start the Flask web server (control panel + EMLII chat).

    Parameters:
        port (int): The TCP port for the Flask app. Default 5000.
    """
    if not FLASK_AVAILABLE:
        print(f"\n{RED} Flask is not installed.{RESET}")
        print(f" Run:  {CYAN}pip install flask --break-system-packages{RESET}\n")
        sys.exit(1)

    lan = get_lan_ip()
    print(f"""
{GREEN}{BOLD} ######  ##     ##  #       ###  ###
 #       # #   # #  #        #    #
 ######  #  # #  #  #        #    #
 #       #   #   #  #        #    #
 ######  #       #  ######  ###  ###
{RESET}
{GREEN} EMLII v{EMLII.VERSION} — Web Mode{RESET}
 {"─" * 42}
{MUTED} Local   →{RESET}  {CYAN}http://127.0.0.1:{port}{RESET}
{MUTED} Network →{RESET}  {CYAN}http://{lan}:{port}{RESET}
 {"─" * 42}
{MUTED} Open either URL in your browser.
 Other devices: use the Network URL.{RESET}
""")
    # host='0.0.0.0' listens on all interfaces so other devices can connect.
    # debug=False avoids Flask's reloader conflicting with background threads.
    app.run(host='0.0.0.0', port=port, debug=False)


# ── run_terminal() already defined above ──────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    """
    if __name__ == '__main__' only runs when this file is executed directly:
        python EMLII.py

    It does NOT run when the file is imported as a module by another script.
    This is standard Python convention for the program's entry point.

    sys.argv is a list of command-line arguments.
        sys.argv[0] = the script name ('EMLII.py')
        sys.argv[1] = first argument, e.g. '--web' or '--terminal'
        sys.argv[2] = second argument, e.g. '8888' (port number)
    """

    # Load tool plugins from tools/ before doing anything else, so both
    # terminal and web mode (and !help) see them from the first command.
    load_tools()

    # ── Parse command-line arguments ──────────────────────────────────────────

    if '--web' in sys.argv:
        # Optional port as second argument: python EMLII.py --web 8888
        try:
            port = int(sys.argv[sys.argv.index('--web') + 1])
        except (IndexError, ValueError):
            port = 5000   # Default port if none provided or invalid
        run_web(port)

    elif '--terminal' in sys.argv or '--term' in sys.argv:
        run_terminal()

    else:
        # ── Interactive mode picker ────────────────────────────────────────────
        print(f"""
{GREEN}{BOLD} EMLII v{EMLII.VERSION}{RESET}
 {"─" * 30}
  {CYAN}1.{RESET} Web mode    {MUTED}(browser UI, shareable on LAN){RESET}
  {CYAN}2.{RESET} Terminal    {MUTED}(classic coloured terminal){RESET}
 {"─" * 30}""")

        choice = input(f"  {GREEN}Choose [1/2]: {RESET}").strip()

        if choice == '1':
            # Optional port prompt for web mode
            port_in = input(f"  {MUTED}Port [default 5000]: {RESET}").strip()
            port = int(port_in) if port_in.isdigit() else 5000
            run_web(port)
        else:
            run_terminal()
