# monitor_multi.py — Paralel kontrol + Telegram timeout/proxy fix + ping kill + okunabilir alarmlar + raporlar
import os, sys, time, subprocess, socket, re, json, random, threading
from urllib.parse import urlparse
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor
import requests
from dotenv import load_dotenv

# ========= Kurulum / Ortam =========
def app_base():
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))

BASE_DIR = app_base()
load_dotenv(os.path.join(BASE_DIR, ".env"))

BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID")
INTERVAL    = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
COOLDOWN    = int(os.getenv("ALERT_COOLDOWN_SECONDS", "600"))
RECOVERY_NOTIFY = os.getenv("RECOVERY_NOTIFY", "true").lower() == "true"
TIMEOUT     = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "5") or "5")    
PING_MS     = int(os.getenv("PING_TIMEOUT_MS", "800"))                 
PING_KILL   = int(os.getenv("PING_KILL_AFTER_MS", "2000"))             
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "12"))                      
TEL_TIMEOUT = float(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "4"))        
DEBUG       = os.getenv("DEBUG", "0") == "1"                           

DAILY_H   = int(os.getenv("DAILY_REPORT_HOUR", "9"))
DAILY_M   = int(os.getenv("DAILY_REPORT_MINUTE", "0"))
WEEKDAY_W = int(os.getenv("WEEKLY_REPORT_WEEKDAY", "0"))    
WEEKLY_H  = int(os.getenv("WEEKLY_REPORT_HOUR", "9"))
WEEKLY_M  = int(os.getenv("WEEKLY_REPORT_MINUTE", "5"))


FAILURE_THRESHOLD     = int(os.getenv("FAILURE_THRESHOLD", "3"))
SUCCESS_THRESHOLD     = int(os.getenv("SUCCESS_THRESHOLD", "2"))
AGG_WINDOW_SECONDS    = int(os.getenv("AGG_WINDOW_SECONDS", "60"))
STORM_THRESHOLD       = int(os.getenv("STORM_THRESHOLD", "6"))
STORM_SUPPRESS_SEC    = int(os.getenv("STORM_SUPPRESS_SEC", "900"))
GLOBAL_PROBE_TARGET   = os.getenv("GLOBAL_PROBE_TARGET", "1.1.1.1")
GLOBAL_SUPPRESS_SEC   = int(os.getenv("GLOBAL_SUPPRESS_SEC", "600"))
MAX_ALERTS_PER_WINDOW = int(os.getenv("MAX_ALERTS_PER_WINDOW", "5"))

assert BOT_TOKEN and CHAT_ID, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID zorunlu."

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
STATE_PATH = os.path.join(BASE_DIR, "state.json")

# Tek bir session kullan, sistem proxy'lerini devre dışı bırak
SESSION = requests.Session()
SESSION.trust_env = False  # env proxy'lerini kullanma (asıl donma sebebi olabiliyor)

# ========= Telegram / Format =========
def html_escape(s: str) -> str:
    return (s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"))

def send_raw(msg_html: str):
    try:
        SESSION.post(
            API_URL,
            data={
                "chat_id": CHAT_ID,
                "text": msg_html,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=TEL_TIMEOUT,
            allow_redirects=False
        )
    except Exception as e:
        print(f"[WARN] Telegram gönderim hatası (timeout/proxy olabilir): {e}")

# Basit global rate limiter + kuyruk + grouped flush
rate_lock = threading.Lock()
rate_window = []   # timestamps of sent messages
rate_queue = []    # queued messages text
def rate_evict_old():
    cutoff = time.time() - AGG_WINDOW_SECONDS
    with rate_lock:
        while rate_window and rate_window[0] < cutoff:
            rate_window.pop(0)

def rate_can_send():
    rate_evict_old()
    with rate_lock:
        return len(rate_window) < MAX_ALERTS_PER_WINDOW

def rate_register_send():
    with rate_lock:
        rate_window.append(time.time())

def rate_queue_push(text):
    with rate_lock:
        rate_queue.append(text)

def rate_flush_queue_grouped():
    with rate_lock:
        if not rate_queue:
            return
        header = f"⏳ Oran sınırı nedeniyle geciken {len(rate_queue)} uyarı — tek mesajda birleştirildi:\n"
        body = "\n".join(f"• {html_escape(l)}" for l in rate_queue)
        # empty queue first to avoid recursion causing duplication
        rate_queue.clear()
    # Use send_with_rate (below) to actually send the grouped message
    send_with_rate(header + body)

def send_with_rate(text):
    # if allowed -> send immediate; else queue
    if rate_can_send():
        send_raw(text)
        rate_register_send()
    else:
        rate_queue_push(text)

# ========= Tür algılama / normalize =========
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

# ========= Hata nedenlerini kısalt =========
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

# ========= Kontroller =========
def check_http(url: str):
    try:
        r = SESSION.get(url, timeout=TIMEOUT, verify=False, allow_redirects=False)
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
    """Ping işlemi en fazla PING_KILL ms bekler; süre dolarsa TIMEOUT döner."""
    try:
        ip = ping_url.replace("ping://", "").strip()
        flag = "-n" if os.name == "nt" else "-c"

        if os.name == "nt":
            cmd = ["ping", flag, "1", "-w", str(PING_MS), ip]  # ms
        else:
            wsec = max(1, PING_MS // 1000)
            cmd = ["ping", flag, "1", "-W", str(wsec), ip]     # s

        kill_sec = max(1, PING_KILL // 1000)
        try:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=kill_sec)
            ok = (r.returncode == 0)
            return ok, ("PING OK" if ok else "PING FAIL")
        except subprocess.TimeoutExpired:
            return False, "PING TIMEOUT"
    except Exception as e:
        return False, short_reason(e)

# ========= servers.txt yükleme ve gruplama =========
def load_servers_grouped(path: str):
    items = []; current_group = None   
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

# ========= Kalıcı durum / İstatistik =========
STATE_PATH = os.path.join(BASE_DIR, "state.json")

def load_state():
    if not os.path.exists(STATE_PATH):
        return {"last_state":{}, "last_alert":{}, "stats":{}, "last_daily":"", "last_week":"", "started_at": time.time(),
                "fail_streak": {}, "ok_streak": {}, "is_down": {}, "last_reminder": {}, "storm_until": 0, "global_suppress_until": 0, "agg_events": []}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)

        st.setdefault("fail_streak", {})
        st.setdefault("ok_streak", {})
        st.setdefault("is_down", {})
        st.setdefault("last_reminder", {})
        st.setdefault("storm_until", 0)
        st.setdefault("global_suppress_until", 0)
        st.setdefault("agg_events", [])
        return st
    except Exception:
        return {"last_state":{}, "last_alert":{}, "stats":{}, "last_daily":"", "last_week":"", "started_at": time.time(),
                "fail_streak": {}, "ok_streak": {}, "is_down": {}, "last_reminder": {}, "storm_until": 0, "global_suppress_until": 0, "agg_events": []}

state_lock = threading.Lock()

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

def pct(a, b): return 0.0 if b == 0 else round(100.0 * a / b, 1)
def human_time(ts):
    if not ts: return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

# ========= Global probe / storm helpers =========
def global_probe_ok():
    try:
        ok, info = check_ping("ping://" + GLOBAL_PROBE_TARGET)
        return ok
    except Exception:
        return False

# Aggregation event list is used to detect storm: store tuples (ts, name, 'DOWN'/'UP')
def agg_add_event(state, name, status):
    with state_lock:
        state["agg_events"].append([time.time(), name, status])
        # evict old
        cutoff = time.time() - AGG_WINDOW_SECONDS
        state["agg_events"] = [e for e in state["agg_events"] if e[0] >= cutoff]

def agg_count_recent_downs(state):
    cutoff = time.time() - AGG_WINDOW_SECONDS
    return sum(1 for e in state.get("agg_events", []) if e[0] >= cutoff and e[2] == "DOWN")

# ========= Rapor zamanlayıcı =========
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
    rows.sort(key=lambda x: (x[0], -x[1]))  
    top5 = rows[:5]

    def _list(lst):
        if not lst: return "—"
        return "\n".join([f"• {html_escape(i['name'])}" for i in lst])

    def _top(lst):
        if not lst: return "—"
        out = []
        for u, fl, name, st in lst:
            out.append(
                f"• {html_escape(name)} — <b>{u}%</b> uptime, "
                f"flap:<b>{st['flaps']}</b>, son değişim:<i>{human_time(st['last_change'])}</i>"
            )
        return "\n".join(out)

    return (
        f"🧾 <b>{title}</b>\n"
        f"Başlangıç: <i>{human_time(state.get('started_at'))}</i>\n"
        f"Şu an: <b>{len(up_now)}</b> UP / <b>{len(down_now)}</b> DOWN\n\n"
        f"🔻 <u>Şu an DOWN</u>\n{_list(down_now)}\n\n"
        f"🏁 <u>En Sorunlu (TOP 5)</u>\n{_top(top5)}"
    )

# ========= Ana döngü =========
def main():
    servers_file = os.path.join(BASE_DIR, "servers.txt")
    if not os.path.exists(servers_file):
        raise FileNotFoundError("servers.txt bulunamadı (script ile aynı klasöre koyun).")

    items = load_servers_grouped(servers_file)
    devices = [i for i in items if i["group"] == "device"]
    servers = [i for i in items if i["group"] == "server"]

    state = load_state()
    with state_lock:
        for it in items:
            ensure_stats_for(state, it["name"])
            state["last_state"].setdefault(it["name"], None)
            state["last_alert"].setdefault(it["name"], 0)
            state["fail_streak"].setdefault(it["name"], 0)
            state["ok_streak"].setdefault(it["name"], 0)
            state["is_down"].setdefault(it["name"], False)
            state["last_reminder"].setdefault(it["name"], 0)
    save_state(state)

    print("[INFO] Başlatılıyor… Telegram bildirimi gönderiliyor (timeout={}s)…".format(TEL_TIMEOUT))
    send_with_rate(f"🔍 <b>İzleme başlatıldı</b>\n"
         f"• Kayıt Cihazları: <b>{len(devices)}</b>\n"
         f"• Sunucular: <b>{len(servers)}</b>\n"
         f"• Aralık: <b>{INTERVAL}s</b>  |  Cooldown: <b>{COOLDOWN}s</b>  |  Paralel işçi: <b>{MAX_WORKERS}</b>")
    send_with_rate(format_watchlist(items))
    print("[INFO] Başlangıç bildirimleri gönderildi. Kontroller başlıyor…")

    # Thread-safe run_single
    def run_single(it):
        name, target, kind = it["name"], it["target"], it["kind"]
        # do probe
        if kind == "http":
            ok, info = check_http(target)
        elif kind == "tcp":
            ok, info = check_tcp(target)
        else:
            ok, info = check_ping(target)

        with state_lock:
            prev = state["last_state"].get(name)
            # update stats
            mark_check(state, name, ok, prev)

            # update streaks
            if ok:
                state["ok_streak"][name] = state["ok_streak"].get(name, 0) + 1
                state["fail_streak"][name] = 0
            else:
                state["fail_streak"][name] = state["fail_streak"].get(name, 0) + 1
                state["ok_streak"][name] = 0

            now_ts = time.time()
            # Global suppress check (eğer global suppress aktifse bireyselleri yoksay)
            if now_ts < state.get("global_suppress_until", 0):
                # sadece istatistik güncelle (bireysel uyarı atma)
                state["last_state"][name] = ok
                return (name, ok, info, prev, None)

            # storm detection pre-check: update agg events for DOWN attempts below, then count
            # Determine transitions with debounce:
            alert_msg = None
            # If currently considered up (prev True or None) and fail_streak reached threshold -> declare DOWN
            if (not state["is_down"].get(name, False)) and (state["fail_streak"][name] >= FAILURE_THRESHOLD):
                # enforce per-target cooldown
                last_alert = state["last_alert"].get(name, 0)
                if (now_ts - last_alert) >= COOLDOWN:
                    # add agg event and detect storm
                    agg_add_event(state, name, "DOWN")
                    recent_downs = agg_count_recent_downs(state)
                    if recent_downs >= STORM_THRESHOLD and now_ts > state.get("storm_until", 0):
                        # trigger storm: set storm_until and global_suppress_until
                        state["storm_until"] = now_ts + STORM_SUPPRESS_SEC
                        state["global_suppress_until"] = now_ts + GLOBAL_SUPPRESS_SEC
                        alert_msg = f"🌪️ <b>ALERT FIRTINASI</b> tespit edildi — {recent_downs} hedef aynı anda düştü. Bireysel uyarılar {int(STORM_SUPPRESS_SEC/60)} dk bastırılıyor."
                        # set last_alert for all targets so we don't spam when storm is active
                        for it2 in items:
                            state["last_alert"][it2["name"]] = now_ts
                    else:
                        # normal DOWN alert
                        alert_msg = "❌ <b>{}</b> erişilemiyor!\n• Hedef: <code>{}</code>\n• Tür: <b>{}</b>\n• Neden: {}".format(
                            html_escape(name), html_escape(target), kind.upper(), html_escape(info))
                        state["last_alert"][name] = now_ts
                        # add event already done

                state["is_down"][name] = True
                state["last_state"][name] = False

            elif state["is_down"].get(name, False) and (state["ok_streak"][name] >= SUCCESS_THRESHOLD):

                agg_add_event(state, name, "UP")
                state["is_down"][name] = False
                state["last_state"][name] = True
                state["last_alert"][name] = now_ts
                if RECOVERY_NOTIFY:
                    alert_msg = "✅ <b>{}</b> tekrar erişilebilir.\n• Hedef: <code>{}</code>\n• Tür: <b>{}</b>\n• Ayrıntı: {}".format(
                        html_escape(name), html_escape(target), kind.upper(), html_escape(info))

            else:
                # If still down, maybe send periodic reminder
                if state["is_down"].get(name, False):
                    last_rem = state["last_reminder"].get(name, 0)
                    # reminder every COOLDOWN (or set different interval)
                    if (now_ts - last_rem) >= COOLDOWN:
                        alert_msg = f"🔁 <b>{html_escape(name)}</b> hâlâ DOWN — {html_escape(target)}"
                        state["last_reminder"][name] = now_ts
                        state["last_alert"][name] = now_ts

            # persist last_state if not set above
            state["last_state"][name] = state["is_down"].get(name, False) == False

        # return tuple for logging/processing in main thread if needed
        return (name, ok, info, prev, alert_msg)

    # main loop
    while True:
        loop_start = time.time()
        # Global probe: if external probe fails, set global suppress for a while
        try:
            if not global_probe_ok():
                with state_lock:
                    if time.time() > state.get("global_suppress_until", 0):
                        # notify once about global issue (use send_with_rate)
                        send_with_rate("🌐 <b>Genel bağlantı sorunu</b> tespit edildi. Bireysel uyarılar {} dk bastırılacak.".format(int(GLOBAL_SUPPRESS_SEC/60)))
                    state["global_suppress_until"] = time.time() + GLOBAL_SUPPRESS_SEC
        except Exception:
            # ignore probe errors
            pass

        # run checks in parallel
        results = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for res in ex.map(run_single, items):
                if res:
                    results.append(res)

        # Collect alert messages (thread-safe returns)
        alert_lines = []
        for tup in results:
            name, ok, info, prev, alert_msg = tup
            if alert_msg:
                # if global suppress active (set after run_single maybe), skip sending individual
                if time.time() < state.get("global_suppress_until", 0):
                    # queue a short info for grouping later
                    agg_add_event(state, name, "SUPPRESSED")
                    continue
                alert_lines.append(alert_msg if isinstance(alert_msg, str) else str(alert_msg))

        # If many alerts at once, group them
        if alert_lines:
            # group into one message if too many
            if len(alert_lines) == 1:
                send_with_rate(alert_lines[0])
            else:
                text = "📣 <b>Durum değişiklikleri ({} adet)</b>\n".format(len(alert_lines))
                text += "\n".join(f"• {line}" for line in alert_lines)
                send_with_rate(text)

        # flush queued rate messages if any
        rate_flush_queue_grouped()

        # Rapor zamanları
        now_dt = time.time()
        if should_send_daily(now_dt, state):
            send_with_rate(render_summary(items, state, "GÜNLÜK RAPOR"))
            state["last_daily"] = date.fromtimestamp(now_dt).isoformat()
            save_state(state)

        if should_send_weekly(now_dt, state):
            send_with_rate(render_summary(items, state, "HAFTALIK RAPOR"))
            dt = datetime.fromtimestamp(now_dt)
            state["last_week"] = f"{dt.isocalendar().year}-W{dt.isocalendar().week}"
            save_state(state)

        save_state(state)
        if DEBUG:
            print(f"[DEBUG] Tur süresi: {round(time.time()-loop_start,2)} sn")
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()