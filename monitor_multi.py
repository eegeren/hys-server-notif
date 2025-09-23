# monitor_multi.py — Paralel kontrol + Telegram timeout/proxy fix + ping kill + okunabilir alarmlar + raporlar
import os, sys, time, subprocess, socket, re, json, random, threading
from urllib.parse import urlparse
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
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
DRY_RUN     = os.getenv("DRY_RUN", "0") == "1"                        

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
SESSION.trust_env = False

# ========= Telegram / Format =========
def html_escape(s: str) -> str:
    return (s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"))

def send_raw(msg_html: str):
    if DRY_RUN:
        if DEBUG: print(f"[TG-DRYRUN] {msg_html[:120].replace(chr(10),' ')}...")
        return
    try:
        r = SESSION.post(
            API_URL,
            data={
                "chat_id": CHAT_ID,
                "text": msg_html,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=(TEL_TIMEOUT, TEL_TIMEOUT),
            allow_redirects=False
        )
        if DEBUG: print(f"[TG] status={getattr(r,'status_code',None)}")
    except Exception as e:
        print(f"[WARN] Telegram gönderim hatası: {e}")

# Basit global rate limiter + kuyruk + grouped flush
rate_lock = threading.Lock()
rate_window, rate_queue = [], []
def rate_evict_old():
    cutoff = time.time() - AGG_WINDOW_SECONDS
    with rate_lock:
        while rate_window and rate_window[0] < cutoff:
            rate_window.pop(0)
def rate_can_send():
    rate_evict_old()
    with rate_lock: return len(rate_window) < MAX_ALERTS_PER_WINDOW
def rate_register_send():
    with rate_lock: rate_window.append(time.time())
def rate_queue_push(text):
    with rate_lock: rate_queue.append(text)
def rate_flush_queue_grouped():
    with rate_lock:
        if not rate_queue: return
        header = f"⏳ Oran sınırı nedeniyle geciken {len(rate_queue)} uyarı:\n"
        body = "\n".join(f"• {html_escape(l)}" for l in rate_queue)
        rate_queue.clear()
    send_with_rate(header + body)
def send_with_rate(text):
    if rate_can_send():
        send_raw(text); rate_register_send()
    else:
        rate_queue_push(text)

# ========= Tür algılama / normalize =========
def normalize_target(raw: str):
    t = raw.strip(); tl = t.lower()
    if tl.startswith(("http://","https://")): return t, "http"
    if tl.startswith("tcp://"):              return t, "tcp"
    if tl.startswith("ping://"):             return t, "ping"
    if ":" in t:                             return f"tcp://{t}", "tcp"
    return f"ping://{t}", "ping"

# ========= Hata nedenlerini kısalt =========
def short_reason(text: str) -> str:
    t = str(text).lower()
    if "timed out" in t or "timeout" in t: return "Zaman aşımı"
    if "refused" in t: return "Bağlantı reddedildi"
    if "dns" in t or "getaddrinfo" in t: return "DNS/çözümleme hatası"
    if "unreachable" in t: return "Ağ erişilemiyor"
    return str(text)[:140]

# ========= Kontroller =========
def check_http(url: str):
    try:
        r = SESSION.get(url, timeout=TIMEOUT, verify=False, allow_redirects=False)
        return (200 <= r.status_code < 300), f"HTTP {r.status_code}"
    except Exception as e:
        return False, short_reason(e)
def check_tcp(tcp_url: str):
    u = urlparse(tcp_url); host, port = u.hostname or "", u.port or 0
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT): return True, "TCP OK"
    except Exception as e: return False, short_reason(e)
def check_ping(ping_url: str):
    try:
        ip = ping_url.replace("ping://", "").strip()
        flag = "-n" if os.name == "nt" else "-c"
        cmd = ["ping", flag, "1", "-w" if os.name=="nt" else "-W", str(max(1,PING_MS//1000)), ip]
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=max(1,PING_KILL//1000))
        return (r.returncode==0), ("PING OK" if r.returncode==0 else "PING FAIL")
    except subprocess.TimeoutExpired: return False, "PING TIMEOUT"
    except Exception as e: return False, short_reason(e)

# ========= servers.txt yükleme =========
def load_servers_grouped(path: str):
    items=[]; current_group=None
    with open(path,"r",encoding="utf-8") as f:
        for raw in f:
            line=raw.strip()
            if not line: continue
            if line.startswith("#"):
                if "cihaz" in line.lower(): current_group="device"
                if "sunucu" in line.lower(): current_group="server"
                continue
            name,target=(line.split(",",1)+[line])[:2]
            norm,kind=normalize_target(target.strip())
            grp=current_group or ("device" if kind in("http","tcp") else "server")
            items.append({"name":name.strip(),"target":norm,"kind":kind,"group":grp})
    return items

def format_watchlist(items):
    dev=[i for i in items if i["group"]=="device"]; srv=[i for i in items if i["group"]=="server"]
    def _lines(lst): return "—" if not lst else "\n".join([f"• {html_escape(i['name'])} → <code>{html_escape(i['target'])}</code>" for i in lst])
    return (f"📋 <b>İzlenenler</b>\n"
            f"• Kayıt Cihazları: <b>{len(dev)}</b>\n"
            f"• Sunucular: <b>{len(srv)}</b>\n\n"
            f"🔸 <u>Kayıt Cihazları</u>\n{_lines(dev)}\n\n"
            f"🔹 <u>Sunucular</u>\n{_lines(srv)}")

# ========= Kalıcı durum =========
def load_state():
    if not os.path.exists(STATE_PATH):
        return {"last_state":{}, "last_alert":{}, "stats":{}, "started_at":time.time(),
                "fail_streak":{}, "ok_streak":{}, "is_down":{}, "last_reminder":{},
                "storm_until":0,"global_suppress_until":0,"agg_events":[]}
    try:
        with open(STATE_PATH,"r",encoding="utf-8") as f: return json.load(f)
    except: return {"last_state":{}, "last_alert":{}, "stats":{}, "started_at":time.time(),
                   "fail_streak":{}, "ok_streak":{}, "is_down":{}, "last_reminder":{},
                   "storm_until":0,"global_suppress_until":0,"agg_events":[]}
state_lock=threading.Lock()
def save_state(state):
    try:
        with open(STATE_PATH,"w",encoding="utf-8") as f: json.dump(state,f)
    except: pass

# ========= Ana döngü =========
def main():
    servers_file=os.path.join(BASE_DIR,"servers.txt")
    if not os.path.exists(servers_file): raise FileNotFoundError("servers.txt bulunamadı.")
    items=load_servers_grouped(servers_file)
    state=load_state()
    print("[INFO] Başlatılıyor…")
    send_with_rate(f"🔍 İzleme başlatıldı\n• Toplam: <b>{len(items)}</b>\n• Aralık: {INTERVAL}s")
    send_with_rate(format_watchlist(items))

    def run_single(it):
        start=time.time()
        name,target,kind=it["name"],it["target"],it["kind"]
        try:
            if kind=="http": ok,info=check_http(target)
            elif kind=="tcp": ok,info=check_tcp(target)
            else: ok,info=check_ping(target)
            prev=state["last_state"].get(name)
            state["last_state"][name]=ok
            return (name,ok,info,prev,None)
        except Exception as e:
            return (name,False,str(e),None,None)
        finally:
            if DEBUG: print(f"[DBG] {name} süresi {round(time.time()-start,2)}s")

    while True:
        loop_start=time.time()
        results=[]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for res in ex.map(run_single,items):
                if res: results.append(res)
        for name,ok,info,prev,alert_msg in results:
            if DEBUG: print(f"[RES] {name}: {ok} ({info})")
        rate_flush_queue_grouped()
        save_state(state)
        if DEBUG: print(f"[DEBUG] Tur süresi: {round(time.time()-loop_start,2)} sn")
        time.sleep(INTERVAL)

if __name__=="__main__":
    main()