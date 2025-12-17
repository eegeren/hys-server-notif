import os
import sys
import time
import json
import re
import socket
import subprocess
import threading
from dataclasses import dataclass
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv


# -------------------- Utils --------------------
def app_base() -> str:
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))


def html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def short_reason(err: Exception | str) -> str:
    t = str(err)
    tl = t.lower()

    if "timed out" in tl or "timeout" in tl:
        return "Zaman asimi"
    if "connection refused" in tl or "winerror 10061" in tl or "actively refused" in tl:
        return "Baglanti reddedildi"
    if "getaddrinfo failed" in tl or "name or service not known" in tl or "dns" in tl:
        return "DNS/cozumleme hatasi"
    if "network is unreachable" in tl:
        return "Ag erisilemiyor"
    if "ssl" in tl or "certificate" in tl:
        return "SSL/sertifika hatasi"

    t = re.sub(r"HTTP[S]?ConnectionPool\(.*?\)", "HTTP baglanti hatasi", t)
    t = re.sub(r"<.*?object at 0x[0-9a-fA-F]+>", "", t).strip()
    return t[:160] + ("..." if len(t) > 160 else "")


# -------------------- Config --------------------
BASE_DIR = app_base()
load_dotenv(os.path.join(BASE_DIR, ".env"))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TEL_TIMEOUT = float(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "4"))

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "15"))
REQ_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "3"))
PING_MS = int(os.getenv("PING_TIMEOUT_MS", "800"))
PING_KILL_MS = int(os.getenv("PING_KILL_AFTER_MS", "2000"))

FAILURE_THRESHOLD = int(os.getenv("FAILURE_THRESHOLD", "3"))
SUCCESS_THRESHOLD = int(os.getenv("SUCCESS_THRESHOLD", "2"))

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "12"))  # optional, default 12
RECOVERY_NOTIFY = os.getenv("RECOVERY_NOTIFY", "false").lower() == "true"

MAX_ALERTS_PER_WINDOW = int(os.getenv("MAX_ALERTS_PER_WINDOW", "5"))
RATE_WINDOW_SECONDS = int(os.getenv("RATE_WINDOW_SECONDS", "30"))

DEBUG = os.getenv("DEBUG", "0") == "1"
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID zorunlu.")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
STATE_PATH = os.path.join(BASE_DIR, "state.json")
SERVERS_PATH = os.path.join(BASE_DIR, "servers.txt")

SESSION = requests.Session()
SESSION.trust_env = False  # proxy kaynakli donmalari engeller


# -------------------- Targets --------------------
@dataclass
class Target:
    name: str
    kind: str  # ping|tcp|http
    norm: str  # normalized target


def strip_inline_comment(line: str) -> str:
    # "..., tcp://1.2.3.4:80   # comment" -> "..., tcp://1.2.3.4:80"
    return line.split("#", 1)[0].strip()


def normalize_target(raw: str) -> tuple[str, str]:
    t = strip_inline_comment(raw).strip()
    tl = t.lower()

    if tl.startswith(("http://", "https://")):
        return t, "http"
    if tl.startswith("tcp://"):
        return t, "tcp"
    if tl.startswith("ping://"):
        return t, "ping"

    # schema yoksa:
    # "host:port" => tcp
    # "host"      => ping
    if ":" in t:
        return f"tcp://{t}", "tcp"
    return f"ping://{t}", "ping"


def load_targets(path: str) -> list[Target]:
    if not os.path.exists(path):
        raise FileNotFoundError("servers.txt bulunamadi. Script ile ayni klasorde olmali.")

    items: list[Target] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = strip_inline_comment(raw)
            if not line or line.startswith("#"):
                continue

            if "," in line:
                name, tgt = line.split(",", 1)
                name = name.strip()
                tgt = tgt.strip()
            else:
                name = line.strip()
                tgt = line.strip()

            norm, kind = normalize_target(tgt)
            items.append(Target(name=name, kind=kind, norm=norm))

    return items


# -------------------- Checks --------------------
def check_http(url: str) -> tuple[bool, str]:
    try:
        r = SESSION.get(url, timeout=REQ_TIMEOUT, verify=False, allow_redirects=False)
        ok = 200 <= r.status_code < 300
        return ok, f"HTTP {r.status_code}"
    except Exception as e:
        return False, short_reason(e)


def check_tcp(tcp_url: str) -> tuple[bool, str]:
    # urlparse bazen port'ta bosluk/yazi gorurse ValueError atar; yakaliyoruz
    try:
        u = urlparse(tcp_url)
        host = (u.hostname or "").strip()
        port = u.port  # may raise ValueError
        if not host or not port:
            return False, "Gecersiz TCP hedefi"

        with socket.create_connection((host, int(port)), timeout=REQ_TIMEOUT):
            return True, "TCP OK"
    except ValueError:
        return False, "Gecersiz port (servers.txt satirinda port sonu bosluk/yorum olabilir)"
    except Exception as e:
        return False, short_reason(e)


def check_ping(ping_url: str) -> tuple[bool, str]:
    try:
        host = ping_url.replace("ping://", "").strip()
        if not host:
            return False, "Ping hedefi bos"

        count_flag = "-n" if os.name == "nt" else "-c"

        if os.name == "nt":
            cmd = ["ping", count_flag, "1", "-w", str(PING_MS), host]  # ms
        else:
            wsec = max(1, PING_MS // 1000)
            cmd = ["ping", count_flag, "1", "-W", str(wsec), host]     # s

        kill_sec = max(1, PING_KILL_MS // 1000)
        try:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=kill_sec)
            ok = (r.returncode == 0)
            return ok, ("PING OK" if ok else "PING FAIL")
        except subprocess.TimeoutExpired:
            return False, "PING TIMEOUT"
    except Exception as e:
        return False, short_reason(e)


def run_check(t: Target) -> tuple[str, bool, str]:
    if t.kind == "http":
        ok, info = check_http(t.norm)
    elif t.kind == "tcp":
        ok, info = check_tcp(t.norm)
    else:
        ok, info = check_ping(t.norm)
    return t.name, ok, info


# -------------------- Telegram (rate-limited) --------------------
rate_lock = threading.Lock()
send_timestamps: list[float] = []


def _rate_evict(now: float):
    cutoff = now - RATE_WINDOW_SECONDS
    while send_timestamps and send_timestamps[0] < cutoff:
        send_timestamps.pop(0)


def _rate_can_send(now: float) -> bool:
    _rate_evict(now)
    return len(send_timestamps) < MAX_ALERTS_PER_WINDOW


def _rate_register(now: float):
    send_timestamps.append(now)


def tg_send_html(text: str) -> None:
    if DRY_RUN:
        print("[TG-DRYRUN]", text.replace("\n", " ")[:200])
        return

    try:
        r = SESSION.post(
            API_URL,
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=(TEL_TIMEOUT, TEL_TIMEOUT),
            allow_redirects=False,
        )
        if DEBUG:
            body = (r.text or "")[:800]
            print(f"[TG] status={r.status_code} body={body}")
    except Exception as e:
        print("[WARN] Telegram gonderim hatasi:", e)


def tg_send_with_rate(text: str) -> None:
    # Bu projede mesajlar kisa olacak (tek hedef). Yine de rate limit uyguluyoruz.
    now = time.time()
    with rate_lock:
        if not _rate_can_send(now):
            if DEBUG:
                print("[RATE] Telegram rate limit dolu, mesaj atlandi.")
            return
        _rate_register(now)
    tg_send_html(text)


# -------------------- State --------------------
def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {
            "started_at": time.time(),
            "status": {},      # name -> "up"/"down"/None
            "fail": {},        # name -> int
            "ok": {},          # name -> int
        }
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("started_at", time.time())
        st.setdefault("status", {})
        st.setdefault("fail", {})
        st.setdefault("ok", {})
        return st
    except Exception:
        return {
            "started_at": time.time(),
            "status": {},
            "fail": {},
            "ok": {},
        }


def save_state(st: dict) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception as e:
        print("[WARN] state.json yazilamadi:", e)


# -------------------- Main --------------------
def main():
    targets = load_targets(SERVERS_PATH)
    st = load_state()

    for t in targets:
        st["status"].setdefault(t.name, None)
        st["fail"].setdefault(t.name, 0)
        st["ok"].setdefault(t.name, 0)

    save_state(st)

    # Baslangic mesaji (tek sefer)
    tg_send_with_rate(
        "<b>Izleme basladi</b>\n"
        f"Targets: <b>{len(targets)}</b>\n"
        f"Interval: <b>{CHECK_INTERVAL}s</b>\n"
        f"Failure threshold: <b>{FAILURE_THRESHOLD}</b>\n"
    )

    while True:
        loop_start = time.time()

        results: dict[str, tuple[bool, str]] = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = [ex.submit(run_check, t) for t in targets]
            for fu in as_completed(futures):
                name, ok, info = fu.result()
                results[name] = (ok, info)

        # Sadece state transition'da bildirim:
        for t in targets:
            name = t.name
            ok, info = results.get(name, (False, "No result"))

            was_down = (st["status"].get(name) == "down")

            if ok:
                st["ok"][name] = st["ok"].get(name, 0) + 1
                st["fail"][name] = 0
            else:
                st["fail"][name] = st["fail"].get(name, 0) + 1
                st["ok"][name] = 0

            # DOWN transition (sadece bir kez)
            if (not was_down) and (st["fail"][name] >= FAILURE_THRESHOLD):
                st["status"][name] = "down"

                msg = (
                    "<b>DOWN</b>\n"
                    f"Ad: <b>{html_escape(name)}</b>\n"
                    f"Hedef: <code>{html_escape(t.norm)}</code>\n"
                    f"Tur: <b>{html_escape(t.kind.upper())}</b>\n"
                    f"Neden: {html_escape(info)}"
                )
                tg_send_with_rate(msg)

            # UP transition (bildirim istemiyorsun; sadece state toparla)
            elif was_down and (st["ok"][name] >= SUCCESS_THRESHOLD):
                st["status"][name] = "up"

                if RECOVERY_NOTIFY:
                    msg = (
                        "<b>UP</b>\n"
                        f"Ad: <b>{html_escape(name)}</b>\n"
                        f"Hedef: <code>{html_escape(t.norm)}</code>\n"
                        f"Tur: <b>{html_escape(t.kind.upper())}</b>\n"
                        f"Ayrinti: {html_escape(info)}"
                    )
                    tg_send_with_rate(msg)

            # Transition yoksa status degistirme: debounce disinda state degismez.

        save_state(st)

        if DEBUG:
            dur = round(time.time() - loop_start, 2)
            downs = sum(1 for v in st["status"].values() if v == "down")
            print(f"[DEBUG] loop={dur}s targets={len(targets)} down={downs}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
