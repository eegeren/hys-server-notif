# monitor_multi.py
# Gruplu izleme: "Kayıt Cihazları" (DVR/NVR/Kamera vb.) ve "Sunucular"
# - servers.txt içindeki bölüm başlıklarıyla ( # --- Cihazlar --- / # --- Sunucular --- ) gruplar korunur
# - Şema yoksa: "IP:port" -> tcp (Cihazlar), "IP/host" -> ping (Sunucular)
# - İlk DOWN görüldüğünde de uyarı gönderir
# - Başlangıçta her grubun sayısını ve izleme listesini Telegram’a yollar

import os
import sys
import time
import subprocess
import socket
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

# ---------- Yardımcılar ----------

def base_dir_of_app() -> str:
    """EXE veya .py çalışma klasörünü döndürür."""
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))

def load_env():
    base = base_dir_of_app()
    load_dotenv(os.path.join(base, ".env"))
    return base

BASE_DIR = load_env()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
COOLDOWN = int(os.getenv("ALERT_COOLDOWN_SECONDS", "600"))
RECOVERY_NOTIFY = os.getenv("RECOVERY_NOTIFY", "true").lower() == "true"
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "5") or "5")

assert BOT_TOKEN and CHAT_ID, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID zorunlu."

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

def send(msg: str):
    try:
        requests.post(API_URL, data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        print("Telegram gönderim hatası:", e)

def is_http(target: str) -> bool:
    t = target.strip().lower()
    return t.startswith("http://") or t.startswith("https://")

def is_tcp(target: str) -> bool:
    return target.strip().lower().startswith("tcp://")

def is_ping(target: str) -> bool:
    return target.strip().lower().startswith("ping://")

def check_http(url: str) -> tuple[bool, str]:
    try:
        # Self-signed olabilir; doğrulamayı kapatıyoruz. Güvenli ağda kullanın.
        r = requests.get(url, timeout=TIMEOUT, verify=False)
        ok = 200 <= r.status_code < 300
        return ok, f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"HTTP hata: {e}"

def check_tcp(tcp_url: str) -> tuple[bool, str]:
    # tcp://IP:port
    u = urlparse(tcp_url)
    host = u.hostname or ""
    port = u.port or 0
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return True, f"TCP {host}:{port} OK"
    except Exception as e:
        return False, f"TCP hata {host}:{port} → {e}"

def check_ping(ping_url: str) -> tuple[bool, str]:
    # ping://IP veya çıplak IP normalize edilmiş halidir
    try:
        ip = ping_url.replace("ping://", "").strip()
        count_flag = "-n" if os.name == "nt" else "-c"
        r = subprocess.run(["ping", count_flag, "1", ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return (r.returncode == 0), ("PING OK" if r.returncode == 0 else "PING FAIL")
    except Exception as e:
        return False, f"PING hata: {e}"

def normalize_target(raw: str) -> tuple[str, str]:
    """
    Şema yoksa:
      - 'IP:port' -> tcp
      - 'IP/host' -> ping
    Dönen: (normalized_target, kind)  kind ∈ {"http","tcp","ping"}
    """
    t = raw.strip()
    tl = t.lower()
    if tl.startswith(("http://", "https://")):
        return t, "http"
    if tl.startswith("tcp://"):
        return t, "tcp"
    if tl.startswith("ping://"):
        return t, "ping"
    if ":" in t:
        return f"tcp://{t}", "tcp"
    return f"ping://{t}", "ping"

# ---------- servers.txt yükleme ve gruplama ----------

def load_servers_grouped(path: str):
    """
    servers.txt satırları:
      - Bölüm başlıkları: '# --- Cihazlar ---' veya '# --- Sunucular ---'
      - Veri: 'İSİM,HEDEF'  (HEDEF http(s)://..., tcp://IP:port, ping://IP veya çıplak IP/host)
    Döndürür: list(dict(name, target, kind, group))
    """
    items = []
    current_group = None  # "device" | "server" | None

    def group_from_header(line: str):
        line_l = line.lower()
        if "cihaz" in line_l:     # Cihazlar
            return "device"
        if "sunucu" in line_l:    # Sunucular
            return "server"
        return None

    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                g = group_from_header(line)
                if g:
                    current_group = g
                continue
            # Veri satırı
            if "," not in line:
                # İsim yoksa hedefi isim olarak da kabul et
                name = line
                target = line
            else:
                name, target = line.split(",", 1)
            name = name.strip()
            target = target.strip()
            norm, kind = normalize_target(target)

            # Başlık yazılmadıysa otomatik gruplama:
            # http/tcp -> device, ping -> server
            if current_group is None:
                grp = "device" if kind in ("http", "tcp") else "server"
            else:
                grp = current_group

            items.append({"name": name, "target": norm, "kind": kind, "group": grp})
    return items

# ---------- Başlangıç özeti ----------

def format_watchlist(items):
    dev = [i for i in items if i["group"] == "device"]
    srv = [i for i in items if i["group"] == "server"]

    def _lines(group_items):
        # "Ad (hedef)" biçiminde liste
        out = []
        for it in group_items:
            out.append(f"• {it['name']}  →  {it['target']}")
        return "\n".join(out) if out else "—"

    msg = []
    msg.append(f"📋 İzlenenler")
    msg.append(f"   • Kayıt Cihazları: {len(dev)}")
    msg.append(f"   • Sunucular: {len(srv)}\n")
    msg.append("🔸 Kayıt Cihazları:\n" + _lines(dev))
    msg.append("\n🔹 Sunucular:\n" + _lines(srv))
    return "\n".join(msg)

# ---------- Ana döngü ----------

def main():
    servers_file = os.path.join(BASE_DIR, "servers.txt")
    if not os.path.exists(servers_file):
        raise FileNotFoundError("servers.txt bulunamadı (script ile aynı klasöre koyun).")

    items = load_servers_grouped(servers_file)
    devices = [i for i in items if i["group"] == "device"]
    servers = [i for i in items if i["group"] == "server"]

    # Durum & cooldown kayıtları (isim bazlı)
    last_state = {i["name"]: None for i in items}
    last_alert = {i["name"]: 0 for i in items}

    # Başlangıç mesajları
    send(f"🔍 İzleme başlatıldı: {len(devices)} kayıt cihazı, {len(servers)} sunucu | interval={INTERVAL}s")
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

            # İlk kez DOWN görülürse de uyar
            if not ok and prev is None:
                if now - last_alert[name] >= COOLDOWN:
                    send(f"❌ {name} erişilemiyor!\nDetay: {target} | {info}")
                    last_alert[name] = now

            # UP -> DOWN
            elif not ok and prev is True:
                if now - last_alert[name] >= COOLDOWN:
                    send(f"❌ {name} erişilemiyor!\nDetay: {target} | {info}")
                    last_alert[name] = now

            # DOWN -> UP
            elif ok and prev is False and RECOVERY_NOTIFY:
                send(f"✅ {name} tekrar erişilebilir.\nDetay: {target} | {info}")

            # İlk ölçümü konsola yaz (sessiz)
            if prev is None:
                print(f"[INIT] {name} → {'UP' if ok else 'DOWN'} ({kind} | {info})")

            last_state[name] = ok

        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()