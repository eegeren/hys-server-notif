# monitor_multi.py — okunabilir Telegram bildirimleri (HTML), gruplu izleme
import os, sys, time, subprocess, socket, re
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

# ---------- Kurulum ----------
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

assert BOT_TOKEN and CHAT_ID, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID zorunlu."

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# ---------- Yardımcılar ----------
def html_escape(s: str) -> str:
    return (s.replace("&","&amp;")
             .replace("<","&lt;")
             .replace(">","&gt;"))

def send(msg_html: str):
    try:
        requests.post(API_URL, data={"chat_id": CHAT_ID, "text": msg_html, "parse_mode": "HTML", "disable_web_page_preview": True})
    except Exception as e:
        print("Telegram gönderim hatası:", e)

def is_http(t: str) -> bool: return t.lower().startswith(("http://","https://"))
def is_tcp(t: str)  -> bool: return t.lower().startswith("tcp://")
def is_ping(t: str) -> bool: return t.lower().startswith("ping://")

def normalize_target(raw: str) -> tuple[str, str]:
    """Şema yoksa: IP:port -> tcp, IP/host -> ping"""
    t = raw.strip()
    tl = t.lower()
    if tl.startswith(("http://","https://")): return t, "http"
    if tl.startswith("tcp://"):              return t, "tcp"
    if tl.startswith("ping://"):             return t, "ping"
    if ":" in t:                             return f"tcp://{t}", "tcp"
    return f"ping://{t}", "ping"

def short_reason(text: str) -> str:
    """Uzun exception metinlerini kısa, anlaşılır nedenlere çevirir."""
    t = str(text)
    tl = t.lower()

    # Sık karşılaşılanlar
    if "timed out" in tl or "timeout" in tl or "read timed out" in tl:
        return "Zaman aşımı"
    if "connection refused" in tl or "actively refused" in tl or "winerror 10061" in tl:
        return "Bağlantı reddedildi"
    if "name or service not known" in tl or "getaddrinfo failed" in tl or "dns" in tl:
        return "DNS/çözümleme hatası"
    if "network is unreachable" in tl:
        return "Ağ erişilemiyor"
    if "certificate" in tl or "ssl" in tl:
        return "SSL/sertifika hatası"
    if "403" in tl: return "HTTP 403 (Yetkisiz)"
    if "401" in tl: return "HTTP 401 (Kimlik doğrulama gerekiyor)"
    if "404" in tl: return "HTTP 404 (Bulunamadı)"

    # urllib3/requests uzun dizeleri kısalt
    t = re.sub(r"HTTPConnectionPool\(.*?\)", "HTTP bağlantı hatası", t)
    t = re.sub(r"HTTPSConnectionPool\(.*?\)", "HTTPS bağlantı hatası", t)
    t = re.sub(r"<.*?object at 0x[0-9a-fA-F]+>", "", t)

    # En fazla 140 karakter
    t = t.strip()
    return t[:140] + ("…" if len(t) > 140 else "")

# ---------- Kontroller ----------
def check_http(url: str) -> tuple[bool, str]:
    try:
        r = requests.get(url, timeout=TIMEOUT, verify=False)
        ok = 200 <= r.status_code < 300
        return ok, f"HTTP {r.status_code}"
    except Exception as e:
        return False, short_reason(e)

def check_tcp(tcp_url: str) -> tuple[bool, str]:
    u = urlparse(tcp_url)
    host = u.hostname or ""
    port = u.port or 0
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return True, "TCP OK"
    except Exception as e:
        return False, short_reason(e)

def check_ping(ping_url: str) -> tuple[bool, str]:
    try:
        ip = ping_url.replace("ping://", "").strip()
        flag = "-n" if os.name == "nt" else "-c"
        r = subprocess.run(["ping", flag, "1", ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return (r.returncode == 0), ("PING OK" if r.returncode == 0 else "PING FAIL")
    except Exception as e:
        return False, short_reason(e)

# ---------- servers.txt ----------
def load_servers_grouped(path: str):
    """Bölüm başlıkları (Cihazlar/Sunucular) korunur; yoksa otomatik: http/tcp→device, ping→server"""
    items = []
    current_group = None  # device | server | None

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

            name = name.strip()
            target = target.strip()
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

# ---------- Ana döngü ----------
def main():
    servers_file = os.path.join(BASE_DIR, "servers.txt")
    if not os.path.exists(servers_file):
        raise FileNotFoundError("servers.txt bulunamadı (script ile aynı klasöre koyun).")

    items = load_servers_grouped(servers_file)
    devices = [i for i in items if i["group"] == "device"]
    servers = [i for i in items if i["group"] == "server"]

    last_state = {i["name"]: None for i in items}
    last_alert = {i["name"]: 0 for i in items}

    send(f"🔍 <b>İzleme başlatıldı</b>\n"
         f"• Kayıt Cihazları: <b>{len(devices)}</b>\n"
         f"• Sunucular: <b>{len(servers)}</b>\n"
         f"• Aralık: <b>{INTERVAL}s</b>  |  Cooldown: <b>{COOLDOWN}s</b>")
    send(format_watchlist(items))

    while True:
        now = time.time()
        for it in items:
            name, target, kind = it["name"], it["target"], it["kind"]

            if kind == "http":
                ok, info = check_http(target)
            elif kind == "tcp":
                ok, info = check_tcp(target)
            else:
                ok, info = check_ping(target)

            prev = last_state[name]

            if not ok and prev is None:
                if now - last_alert[name] >= COOLDOWN:
                    send("❌ <b>{}</b> erişilemiyor!\n• Hedef: <code>{}</code>\n• Tür: <b>{}</b>\n• Neden: {}".format(
                        html_escape(name), html_escape(target), kind.upper(), html_escape(info)))
                    last_alert[name] = now

            elif not ok and prev is True:
                if now - last_alert[name] >= COOLDOWN:
                    send("❌ <b>{}</b> erişilemiyor!\n• Hedef: <code>{}</code>\n• Tür: <b>{}</b>\n• Neden: {}".format(
                        html_escape(name), html_escape(target), kind.upper(), html_escape(info)))
                    last_alert[name] = now

            elif ok and prev is False and RECOVERY_NOTIFY:
                send("✅ <b>{}</b> tekrar erişilebilir.\n• Hedef: <code>{}</code>\n• Tür: <b>{}</b>\n• Ayrıntı: {}".format(
                    html_escape(name), html_escape(target), kind.upper(), html_escape(info)))

            if prev is None:
                print(f"[INIT] {name} → {'UP' if ok else 'DOWN'} ({kind} | {info})")

            last_state[name] = ok

        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()