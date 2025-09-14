# monitor_multi.py — Paralel kontrol + okunabilir HTML alarmlar + Gruplama + Günlük/Haftalık raporlar + Kalıcı state
import os, sys, time, subprocess, socket, re, json, random
from urllib.parse import urlparse
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv

# ========== Kurulum / Ortam ==========
def app_base():
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))

BASE_DIR = app_base()
load_dotenv(os.path.join(BASE_DIR, ".env"))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
INTERVAL  = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
COOLDOWN  = int(os.getenv("ALERT_COOLDOWN_SECONDS", "600"))
RECOVERY_NOTIFY = os.getenv("RECOVERY_NOTIFY", "true").lower() == "true"
TIMEOUT   = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "5") or "5")
PING_MS   = int(os.getenv("PING_TIMEOUT_MS", "800"))   # Windows: ms, Linux/Mac: saniyeye çevrilir
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "20"))

DAILY_H   = int(os.getenv("DAILY_REPORT_HOUR", "9"))
DAILY_M   = int(os.getenv("DAILY_REPORT_MINUTE", "0"))
WEEKDAY_W = int(os.getenv("WEEKLY_REPORT_WEEKDAY", "0"))  # 0=Mon ... 6=Sun
WEEKLY_H  = int(os.getenv("WEEKLY_REPORT_HOUR", "9"))
WEEKLY_M  = int(os.getenv("WEEKLY_REPORT_MINUTE", "5"))

assert BOT_TOKEN and CHAT_ID, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID zorunlu."

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
STATE_PATH = os.path.join(BASE_DIR, "state.json")

# ========== Telegram / Format ==========
def html_escape(s: str) -> str:
    return (s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"))

def send(msg_html: str):
    try:
        requests.post(API_URL, data={
            "chat_id": CHAT_ID,
            "text": msg_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        })
    except Exception as e:
        print("Telegram gönderim hatası:", e)

# ========== Tür algılama / normalize ==========
def is_http(t: str) -> bool: return t.lower().startswith(("http://","https://"))
def is_tcp(t: str)  -> bool: return t.lower().startswith("tcp://")
def is_ping(t: str) -> bool: return t.lower().startswith("ping://")

def normalize_target(raw: str):
    """Şema yoksa: IP:port -> tcp, IP/host -> ping"""
    t = raw.strip(); tl = t.lower()
    if tl.startswith(("http://","https://")): return t, "http"
    if tl.startswith("tcp://"):              return t, "tcp"
    if tl.startswith("ping://"):             return t, "ping"
    if ":" in t:                             return f"tcp://{t}", "tcp"
    return f"ping://{t}", "ping"

# ========== Kısa hata sebepleri ==========
def short_reason(text: str) -> str:
    t = str(text); tl = t.lower()
    if "timed out" in tl or "timeout" in tl: return "Zaman aşımı"
    if "connection refused" in tl or "actively refused" in tl or "winerror 10061" in tl: return "Bağlantı reddedildi"
    if "getaddrinfo failed" in tl or "name or service not known" in tl or "dns" in tl: return "DNS/çözümleme hatası"
    if "network is unreachable" in tl: return "Ağ erişilemiyor"
    if "certificate" in tl or "ssl" in tl: return "SSL/sertifika hatası"
    if "403" in tl: return "HTTP 403 (Yetkisiz)"
    if "401" in tl: return "HTTP 401 (Kimlik doğrulama gerekiyor)"
    if "404" in tl: return "HTTP 404 (Bulunamadı)"
    t = re.sub(r"HTTP[S]?ConnectionPool\(.*?\)", "HTTP bağlantı hatası", t)
    t = re.sub(r"<.*?object at 0x[0-9a-fA-F]+>", "", t)
    t = t.strip()
    return t[:140] + ("…" if len(t) > 140 else "")

# ========== Kontroller ==========
def check_http(url: str):
    try:
        r = requests.get(url, timeout=TIMEOUT, verify=False)  # self-signed olabilir
        ok = 200 <= r.status_code < 300
        return ok, f"HTTP {r.status_code}"
    except Exception as e:
        return False, short_reason(e)

def check_tcp(tcp_url: str):
    u = urlparse(tcp_url); host = u.hostname or ""; port = u.port or 0
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return True, "TCP OK"
    except Exception as e:
        return False, short_reason(e)

def check_ping(ping_url: str):
    try:
        ip = ping_url.replace("ping://", "").strip()
        flag = "-n" if os.name == "nt" else "-c"
        if os.name == "nt":
            cmd = ["ping", flag, "1", "-w", str(PING_MS), ip]  # ms
        else:
            sec = max(1, PING_MS // 1000)
            cmd = ["ping", flag, "1", "-W", str(sec), ip]      # s
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return (r.returncode == 0), ("PING OK" if r.returncode == 0 else "PING FAIL")
    except Exception as e:
        return False, short_reason(e)

# ========== servers.txt yükleme ve gruplama ==========
def load_servers_grouped(path: str):
    items = []; current_group = None  # device | server | None
    def group_from_header(line: str):
        L = line.lower()
        if "cihaz" in L:  return "device"
        if "sunucu" in L: return "server"
        return None

    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line: continue
            if line.startswith("#"):
                g = group_from_header(line)
                if g: current_group = g
                continue
            if "," in line:
                name, target = line.split(",", 1)
            else:
                name, target = line, line
            name = name.strip(); target = target.strip()
            norm, kind = normalize_target(target)
            grp = current_group if current_group else ("device" if kind in ("http","tcp") else "server")
            items.append({"name": name, "target": norm, "kind": kind, "group": grp})
    return items

def format_watchlist(items):
    dev = [i for i in items if i["group"] == "device"]
    srv = [i for i in items if i["group"] == "server"]
    def _lines(lst):
        if not lst: return "—"
        return "\n".join([f"• {html_escape(i['name'])}  →  <code>{html_escape(i['target'])}</code>" for i in lst])
    return (
        f"📋 <b>İzlenenler</b>\n"
        f"• Kayıt Cihazları: <b>{len(dev)}</b>\n"
        f"• Sunucular: <b>{len(srv)}</b>\n\n"
        f"🔸 <u>Kayıt Cihazları</u>\n{_lines(dev)}\n\n"
        f"🔹 <u>Sunucular</u>\n{_lines(srv)}"
    )

# ========== Kalıcı durum / İstatistik ==========
def load_state():
    if not os.path.exists(STATE_PATH):
        return {"last_state":{}, "last_alert":{}, "stats":{}, "last_daily":"", "last_week":"", "started_at": time.time()}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_state":{}, "last_alert":{}, "stats":{}, "last_daily":"", "last_week":"", "started_at": time.time()}

def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        print("state.json yazılamadı:", e)

def ensure_stats_for(state, name):
    if name not in state["stats"]:
        state["stats"][name] = {"checks":0, "up":0, "down":0, "flaps":0, "last_change": None}

def mark_check(state, name, ok, prev):
    ensure_stats_for(state, name)
    s = state["stats"][name]
    s["checks"] += 1
    if ok: s["up"] += 1
    else:  s["down"] += 1
    if prev is not None and prev != ok:
        s["flaps"] += 1
        s["last_change"] = time.time()

def pct(a, b):
    return 0.0 if b == 0 else round(100.0 * a / b, 1)

def human_time(ts):
    if not ts: return "—"
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M")

# ========== Rapor zamanlayıcı ==========
def should_send_daily(now, state):
    d = date.fromtimestamp(now).isoformat()
    dt = datetime.fromtimestamp(now)
    return (state.get("last_daily") != d) and (dt.hour == DAILY_H and dt.minute == DAILY_M)

def should_send_weekly(now, state):
    dt = datetime.fromtimestamp(now)
    yearweek = f"{dt.isocalendar().year}-W{dt.isocalendar().week}"
    return (state.get("last_week") != yearweek) and (dt.weekday() == WEEKDAY_W and dt.hour == WEEKLY_H and dt.minute == WEEKLY_M)

def render_summary(items, state, title):
    last_state = state.get("last_state", {})
    up_now  = [i for i in items if last_state.get(i["name"]) is True]
    down_now= [i for i in items if last_state.get(i["name"]) is False]

    rows = []
    for it in items:
        name = it["name"]
        st = state["stats"].get(name, {"checks":0,"up":0,"down":0,"flaps":0,"last_change":None})
        u = pct(st["up"], st["checks"])
        rows.append((u, st["flaps"], name, st))
    rows.sort(key=lambda x: (x[0], -x[1]))  # en düşük uptime önce
    top5 = rows[:5]

    def _list(lst):
        if not lst: return "—"
        return "\n".join([f"• {html_escape(i['name'])}" for i in lst])

    def _top(lst):
        if not lst: return "—"
        out=[]
        for u, fl, name, st in lst:
            out.append(f"• {html_escape(name)} — <b>{u}%</b> uptime, flap:<b>{st['flaps']}</b>, son değişim:<i>{human_time(st['last_change'])}</i>")
        return "\n".join(out)

    started = human_time(state.get("started_at"))
    return (
        f"🧾 <b>{title}</b>\n"
        f"Başlangıç: <i>{started}</i>\n"
        f"Şu an: <b>{len(up_now)}</b> UP / <b>{len(down_now)}</b> DOWN\n\n"
        f"🔻 <u>Şu an DOWN</u>\n{_list(down_now)}\n\n"
        f"🏁 <u>En Sorunlu (TOP 5)</u>\n{_top(top5)}"
    )

# ========== Ana döngü ==========
def main():
    servers_file = os.path.join(BASE_DIR, "servers.txt")
    if not os.path.exists(servers_file):
        raise FileNotFoundError("servers.txt bulunamadı (script ile aynı klasöre koyun).")

    items = load_servers_grouped(servers_file)
    devices = [i for i in items if i["group"] == "device"]
    servers = [i for i in items if i["group"] == "server"]

    state = load_state()
    for it in items:
        ensure_stats_for(state, it["name"])
        state["last_state"].setdefault(it["name"], None)
        state["last_alert"].setdefault(it["name"], 0)
    save_state(state)

    send(f"🔍 <b>İzleme başlatıldı</b>\n"
         f"• Kayıt Cihazları: <b>{len(devices)}</b>\n"
         f"• Sunucular: <b>{len(servers)}</b>\n"
         f"• Aralık: <b>{INTERVAL}s</b>  |  Cooldown: <b>{COOLDOWN}s</b>  |  Paralel işçi: <b>{MAX_WORKERS}</b>")
    send(format_watchlist(items))

    while True:
        now = time.time()

        # ---- Paralel kontrol ----
        def run_single(it):
            name, target, kind = it["name"], it["target"], it["kind"]
            prev = state["last_state"].get(name)

            if kind == "http":
                ok, info = check_http(target)
            elif kind == "tcp":
                ok, info = check_tcp(target)
            else:
                ok, info = check_ping(target)

            mark_check(state, name, ok, prev)
            last_alert_ts = state["last_alert"].get(name, 0)
            now_local = time.time()

            if not ok and prev is None and (now_local - last_alert_ts) >= COOLDOWN:
                send("❌ <b>{}</b> erişilemiyor!\n• Hedef: <code>{}</code>\n• Tür: <b>{}</b>\n• Neden: {}".format(
                    html_escape(name), html_escape(target), kind.upper(), html_escape(info)))
                state["last_alert"][name] = now_local

            elif not ok and prev is True and (now_local - last_alert_ts) >= COOLDOWN:
                send("❌ <b>{}</b> erişilemiyor!\n• Hedef: <code>{}</code>\n• Tür: <b>{}</b>\n• Neden: {}".format(
                    html_escape(name), html_escape(target), kind.upper(), html_escape(info)))
                state["last_alert"][name] = now_local

            elif ok and prev is False and RECOVERY_NOTIFY:
                send("✅ <b>{}</b> tekrar erişilebilir.\n• Hedef: <code>{}</code>\n• Tür: <b>{}</b>\n• Ayrıntı: {}".format(
                    html_escape(name), html_escape(target), kind.upper(), html_escape(info)))

            if prev is None:
                print(f"[INIT] {name} → {'UP' if ok else 'DOWN'} ({kind} | {info})")

            state["last_state"][name] = ok

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            list(ex.map(run_single, items))

        # ---- Rapor zamanları ----
        now_dt = time.time()
        if should_send_daily(now_dt, state):
            send(render_summary(items, state, "GÜNLÜK RAPOR"))
            state["last_daily"] = date.fromtimestamp(now_dt).isoformat()
            save_state(state)

        if should_send_weekly(now_dt, state):
            send(render_summary(items, state, "HAFTALIK RAPOR"))
            dt = datetime.fromtimestamp(now_dt)
            state["last_week"] = f"{dt.isocalendar().year}-W{dt.isocalendar().week}"
            save_state(state)

        save_state(state)
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()