import os
import sys
import platform
import urllib.request
import zipfile
import tempfile
import subprocess
import socket
import uuid
import json
import time
import re
import shutil
def get_arch():
    machine = platform.machine().lower()
    arch_map = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "armhf",
        "armv6l": "arm",
        "i386": "386",
        "i686": "386"
    }
    cf_arch = arch_map.get(machine, "amd64")
    xray_map = {
        "amd64": "64",
        "arm64": "arm64-v8a",
        "armhf": "arm32-v7a",
        "arm": "arm32-v6",
        "386": "32"
    }
    xray_arch = xray_map.get(cf_arch, "64")
    return cf_arch, xray_arch
def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
def download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90) as r, open(path, "wb") as f:
        shutil.copyfileobj(r, f)
def main():
    if os.geteuid() == 0:
        try:
            os.setgid(65534)
            os.setuid(65534)
        except Exception:
            pass
    cf_arch, xray_arch = get_arch()
    work = tempfile.mkdtemp(prefix="xcf_")
    os.chdir(work)
    try:
        cf_bin = "cloudflared"
        cf_url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{cf_arch}"
        download(cf_url, cf_bin)
        os.chmod(cf_bin, 0o755)
        xray_zip = "xray.zip"
        xray_url = f"https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-{xray_arch}.zip"
        download(xray_url, xray_zip)
        with zipfile.ZipFile(xray_zip) as z:
            z.extractall(".")
        os.chmod("xray", 0o755)
        port = find_free_port()
        uuid_str = str(uuid.uuid4())
        cfg = {
            "log": {"loglevel": "none"},
            "inbounds": [{
                "port": port,
                "listen": "127.0.0.1",
                "protocol": "vless",
                "settings": {
                    "clients": [{"id": uuid_str, "level": 0}],
                    "decryption": "none"
                },
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {"path": "/"}
                }
            }],
            "outbounds": [{"protocol": "freedom", "settings": {}}]
        }
        with open("config.json", "w") as f:
            json.dump(cfg, f)
        xray_p = subprocess.Popen(
            ["./xray", "run", "-c", "config.json"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        cf_p = subprocess.Popen(
            ["./cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}", "--protocol", "http2", "--no-autoupdate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        tunnel_host = None
        for line in cf_p.stdout:
            m = re.search(r"https://([a-zA-Z0-9.-]+\.trycloudflare\.com)", line)
            if m:
                tunnel_host = m.group(1)
                break
        if not tunnel_host:
            sys.exit(1)
        time.sleep(2)
        ipinfo = subprocess.check_output(["curl", "-s", "https://ipinfo.io"], text=True)
        print(ipinfo.strip())
        link = f"vless://{uuid_str}@104.18.33.0:443?encryption=none&security=tls&sni={tunnel_host}&type=ws&host={tunnel_host}&path=%2F#{tunnel_host}"
        print(link)
        while True:
            if xray_p.poll() is not None or cf_p.poll() is not None:
                break
            time.sleep(1)
    finally:
        try:
            xray_p.terminate()
            cf_p.terminate()
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)
if __name__ == "__main__":
    main()
