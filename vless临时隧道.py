
import asyncio
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import atexit
from websockets.asyncio.server import serve
HOST = "127.0.0.1"
PASSWORD = str(uuid.uuid4())
PATH = "/"
TUNNEL_PROCESS = None
TUNNEL_HOST = None
def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]
PORT = get_free_port()
def install_cloudflared():
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
    arch = arch_map[machine]
    install_dir = os.path.expanduser("~/.local/bin")
    os.makedirs(install_dir, exist_ok=True)
    binary = os.path.join(install_dir, "cloudflared")
    url = (
        "https://github.com/cloudflare/cloudflared/"
        f"releases/latest/download/cloudflared-linux-{arch}"
    )
    print("[*] 正在下载 cloudflared...")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Python-VLESS"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        with tempfile.NamedTemporaryFile(
            dir=install_dir,
            delete=False
        ) as temp:
            temp_path = temp.name
            shutil.copyfileobj(response, temp)
    print("[+] cloudflared 下载完成")
    os.chmod(temp_path, 0o755)
    os.replace(temp_path, binary)
    print("[+] cloudflared 安装成功")
    return binary
class WSReader:
    def __init__(self, ws):
        self.ws = ws
        self.buffer = bytearray()
    async def readexactly(self, size):
        while len(self.buffer) < size:
            data = await self.ws.recv()
            self.buffer.extend(data)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result
    async def readsome(self, size):
        while not self.buffer:
            data = await self.ws.recv()
            self.buffer.extend(data)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result
class WSWriter:
    def __init__(self, ws):
        self.ws = ws
    async def write(self, data):
        for offset in range(0, len(data), 16384):
            await self.ws.send(data[offset:offset + 16384])
async def read_vless_request(reader):
    version = (await reader.readexactly(1))[0]
    if version != 0:
        raise ValueError("Invalid VLESS version")
    client_uuid = await reader.readexactly(16)
    if client_uuid != uuid.UUID(PASSWORD).bytes:
        raise ValueError("Invalid UUID")
    addons_length = (await reader.readexactly(1))[0]
    if addons_length:
        await reader.readexactly(addons_length)
    command = (await reader.readexactly(1))[0]
    if command != 1:
        raise ValueError("Unsupported VLESS command")
    port = struct.unpack("!H", await reader.readexactly(2))[0]
    atyp = (await reader.readexactly(1))[0]
    if atyp == 1:
        host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
    elif atyp == 2:
        length = (await reader.readexactly(1))[0]
        host = (await reader.readexactly(length)).decode("idna")
    elif atyp == 3:
        host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
    else:
        raise ValueError("Invalid address type")
    return host, port
async def relay_client(reader, writer):
    while True:
        data = await reader.readsome(16384)
        if not data:
            break
        writer.write(data)
        await writer.drain()
async def relay_server(reader, writer):
    while True:
        data = await reader.read(16384)
        if not data:
            break
        await writer.write(data)
async def handle_client(ws):
    request = getattr(ws, "request", None)
    path = getattr(request, "path", "/").split("?", 1)[0]
    if path != PATH:
        await ws.close(code=1008)
        return
    ws_reader = WSReader(ws)
    host, port = await read_vless_request(ws_reader)
    remote_reader, remote_writer = await asyncio.open_connection(host, port)
    await ws.send(b"\x00\x00")
    ws_writer = WSWriter(ws)
    tasks = [
        asyncio.create_task(relay_client(ws_reader, remote_writer)),
        asyncio.create_task(relay_server(remote_reader, ws_writer))
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.wait(pending)
    for task in done:
        task.result()
    remote_writer.close()
    await remote_writer.wait_closed()
def _drain_tunnel_output(proc):
    global TUNNEL_HOST
    for line in iter(proc.stdout.readline, b""):
        text = line.decode(errors="ignore")
        matches = re.findall(r"https://([a-zA-Z0-9-]+\.trycloudflare\.com)", text)
        TUNNEL_HOST = next(iter(matches), TUNNEL_HOST)
def start_tunnel(binary, port):
    global TUNNEL_PROCESS, TUNNEL_HOST
    print("[*] 正在启动 Cloudflare Tunnel...")
    TUNNEL_PROCESS = subprocess.Popen(
        [
            binary,
            "tunnel",
            "--no-autoupdate",
            "--protocol",
            "http2",
            "--url",
            f"http://127.0.0.1:{port}"
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0
    )
    thread = threading.Thread(
        target=_drain_tunnel_output,
        args=(TUNNEL_PROCESS,),
        daemon=True
    )
    thread.start()
    while TUNNEL_HOST is None:
        time.sleep(0.5)
    print("[+] Cloudflare Tunnel 启动成功")
    return TUNNEL_HOST
def stop_tunnel():
    global TUNNEL_PROCESS
    if TUNNEL_PROCESS is not None and TUNNEL_PROCESS.poll() is None:
        print("[*] 正在停止 Cloudflare Tunnel...")
        TUNNEL_PROCESS.terminate()
        TUNNEL_PROCESS.wait()
        print("[+] Cloudflare Tunnel 已停止")
    TUNNEL_PROCESS = None
atexit.register(stop_tunnel)
async def main():
    binary = await asyncio.to_thread(install_cloudflared)
    async with serve(
        handle_client,
        HOST,
        PORT,
        max_size=None,
        compression=None,
        ping_interval=20,
        process_request=lambda connection, request: (
            connection.respond(404, "404 page not found\n")
            if request.path.split("?", 1)[0] != PATH
            else (
                connection.respond(400, "Bad Request\n")
                if request.headers.get("Upgrade", "").lower() != "websocket"
                or "upgrade" not in request.headers.get("Connection", "").lower()
                else None
            )
        )
    ):
        host = await asyncio.to_thread(start_tunnel, binary, PORT)
        link = (
            f"vless://{PASSWORD}@104.18.33.0:443"
            f"?encryption=none"
            f"&security=tls"
            f"&sni={host}"
            f"&type=ws"
            f"&host={host}"
            f"&path=%2F"
            f"#{host}"
        )
        print()
        print("VLESS 运行成功")
        print("Cloudflare Tunnel 运行成功")
        print("Local Port:", PORT)
        print("UUID:", PASSWORD)
        print("Protocol: VLESS")
        print("Transport: WebSocket")
        print("Path:", PATH)
        print("TLS: Enabled")
        print("SNI:", host)
        print("Host:", host)
        result = subprocess.run(
            ["curl", "-sS", "https://ipinfo.io"],
            capture_output=True,
            text=True,
            timeout=15
        )
        print(result.stdout)
        print()
        print("节点链接:")
        print(link)
        print()
        print("Running...")
        while True:
            await asyncio.sleep(1)
if __name__ == "__main__":
    asyncio.run(main())
