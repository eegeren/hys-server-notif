import os, sys, time, subprocess, requests
from dotenv import load_dotenv

def load_env_near_exe():
    base_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(base_dir, ".env")
    load_dotenv(env_path)
    return base_dir

BASE_DIR = load_env_near_exe()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
COOLDOWN = int(os.getenv("ALERT_COOLDOWN_SECONDS", "600"))
RECOVERY_NOTIFY = os.getenv("RECOVERY_NOTIFY", "true").lower() == "true"

assert BOT_TOKEN and CHAT_ID, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID zorunlu."

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

def send(msg: str):
    try:
        requests.post(API_URL, data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        print("Telegram gönderim hatası:", e)

def ping_host(ip: str) -> bool:
    try:
        count_flag = "-n" if os.name == "nt" else "-c"
        r = subprocess.run(["ping", count_flag, "1", ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False

def load_servers(path: str):
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, ip = line.split(",", 1)
            items.append((name.strip(), ip.strip()))
    return items

def main():
    servers_file = os.path.join(BASE_DIR, "servers.txt")
    if not os.path.exists(servers_file):
        raise FileNotFoundError("servers.txt bulunamadı.")

    servers = load_servers(servers_file)
    last_state = {name: None for name, _ in servers}
    last_alert = {name: 0 for name, _ in servers}

    send(f"🔍 İzleme başlatıldı: {len(servers)} sunucu | interval={INTERVAL}s")
    while True:
        now = time.time()
        for name, ip in servers:
            ok = ping_host(ip)

            if last_state[name] is None:
                print(f"[INIT] {name} ({ip}) → {'UP' if ok else 'DOWN'}")

            elif ok and last_state[name] is False:
                if RECOVERY_NOTIFY:
                    send(f"✅ {name} tekrar erişilebilir. ({ip})")

            elif not ok and (last_state[name] is True or last_state[name] is None):
                if now - last_alert[name] >= COOLDOWN:
                    send(f"❌ {name} erişilemiyor! ({ip})")
                    last_alert[name] = now

            last_state[name] = ok

        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
 