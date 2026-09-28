# -*- coding: utf-8 -*-
"""Redmi X3000 路由器控制台 —— macOS 桌面应用入口。

运行方式：
    .venv/bin/python app.py            # 使用项目虚拟环境
或直接双击「启动.command」

功能：连接 MiWiFi 路由器，实时监控设备与网速，拉黑/解黑设备、修改设备名、重启路由器。
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from typing import Any, Optional

import webview

from router_api import MiWiFiRouter, RouterAuthError, RouterError, RouterUnreachable

APP_TITLE = "Redmi X3000 路由器控制台"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
UI_DIR = os.path.join(APP_DIR, "ui")
KEYCHAIN_SERVICE = "RedmiX3000Monitor"

HISTORY_MAX = 300  # 图表保留的历史采样点数


# ------------------------------------------------------------------ 配置存取
def _keychain_account(host: str) -> str:
    return f"router@{host}"


def keychain_save(host: str, password: str) -> bool:
    """把密码写入 macOS 钥匙串（登录钥匙串）。"""
    try:
        subprocess.run(
            [
                "security", "add-generic-password",
                "-s", KEYCHAIN_SERVICE,
                "-a", _keychain_account(host),
                "-w", password,
                "-U",  # 已存在则更新
            ],
            check=True, capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def keychain_load(host: str) -> Optional[str]:
    try:
        out = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", KEYCHAIN_SERVICE,
                "-a", _keychain_account(host),
                "-w",
            ],
            check=True, capture_output=True,
        )
        return out.stdout.decode("utf-8").rstrip("\n") or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def load_config() -> dict:
    cfg = {"host": "192.168.31.1", "username": "admin", "poll_interval": 2,
           "password": "", "storage": "keychain"}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: data[k] for k in ("host", "username", "poll_interval", "password", "storage")
                        if k in data})
    except (OSError, ValueError):
        pass
    if cfg.get("storage") == "config":
        cfg["password"] = cfg.get("password", "")
    else:
        cfg["password"] = keychain_load(cfg["host"]) or ""
    return cfg


def save_config(host: str, username: str, poll_interval: int, password: str = "") -> None:
    data = {
        "host": host.strip(),
        "username": username.strip() or "admin",
        "poll_interval": max(1, min(int(poll_interval or 2), 30)),
        "password": "",
        "storage": "keychain",
    }
    if password:
        if keychain_save(data["host"], password):
            data["password"] = ""
            data["storage"] = "keychain"
        else:
            # 钥匙串不可用（如被系统策略拦截）时回退本地存储，README 已说明
            data["password"] = password
            data["storage"] = "config"
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _fmt_bytes(n: float) -> str:
    """字节数 -> 人类可读。"""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_speed(n: float) -> str:
    return _fmt_bytes(n) + "/s"


def _fmt_duration(secs: int) -> str:
    secs = int(secs or 0)
    if secs <= 0:
        return "-"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{mins}分"
    return f"{mins}分钟"


# ------------------------------------------------------------------ 应用主体
class App:
    """pywebview js_api：前端通过 pywebview.api.xxx 调用。"""

    def __init__(self) -> None:
        self.cfg = load_config()
        self.router: Optional[MiWiFiRouter] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._snapshot: dict = {
            "connected": False, "error": "",
            "router": {}, "wan": {}, "count": {"online": 0, "all": 0},
            "blocked": [], "devices": [], "history": [], "last_update": "",
            "cpu": 0, "mem": 0, "uptime": 0,
        }
        self.history: deque = deque(maxlen=HISTORY_MAX)
        self._busy: set[str] = set()  # 正在执行的控制操作（去重）

    # ------------------------------------------------------------ 连接
    def _make_router(self, host: str, username: str, password: str) -> MiWiFiRouter:
        return MiWiFiRouter(host=host, username=username, password=password)

    def connect(self, host: str = "", username: str = "", password: str = "") -> dict:
        """连接路由器（可由前端传入覆盖配置），成功则启动轮询。"""
        host = (host or self.cfg["host"]).strip() or "192.168.31.1"
        username = username or self.cfg["username"] or "admin"
        password = password or self.cfg.get("password") or ""
        try:
            router = self._make_router(host, username, password)
            router.login()
        except (RouterAuthError, RouterUnreachable, RouterError) as err:
            with self._lock:
                self._snapshot["connected"] = False
                self._snapshot["error"] = str(err)
            return {"ok": False, "msg": str(err)}

        # 更新配置并持久化（密码仅在用户传入时才写入钥匙串）
        self.cfg.update(host=host, username=username, password=password)
        if password:
            save_config(host, username, self.cfg["poll_interval"], password)
        else:
            save_config(host, username, self.cfg["poll_interval"])

        self.router = router
        self._stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        return {"ok": True, "msg": f"已连接 {host}"}

    def reconnect(self) -> dict:
        return self.connect()

    # ------------------------------------------------------------ 轮询
    def _poll_loop(self) -> None:
        interval = max(1, int(self.cfg.get("poll_interval", 2)))
        while not self._stop.is_set():
            started = time.time()
            try:
                self._refresh_once()
            except Exception as err:  # 网络抖动等，记录但不断线
                with self._lock:
                    self._snapshot["error"] = f"刷新失败: {err}"
            self._stop.wait(max(0.3, interval - (time.time() - started)))

    def _refresh_once(self) -> None:
        router = self.router
        if router is None:
            return
        init = router.get_init_info()
        status = router.get_status()
        devices = router.get_devices()
        wifi = router.get_wifi_connect_devices()
        blocked = [b.get("mac", "") for b in router.get_blocked_devices()]
        merged = MiWiFiRouter.merge_devices(devices, status.get("dev", []), wifi)

        dev = status.get("dev", [])
        wan = status.get("wan", {})
        count = status.get("count", {})
        now = time.time()
        self.history.append(
            [now, float(wan.get("downspeed", 0) or 0), float(wan.get("upspeed", 0) or 0)]
        )
        cpu = status.get("cpu", {})
        mem = status.get("mem", {})
        snap = {
            "connected": True,
            "error": "",
            "router": {
                "name": init.get("routername", ""),
                "model": init.get("model", ""),
                "is_redmi": bool(init.get("isRedmi")),
                "rom": init.get("romversion", ""),
                "hardware": init.get("hardware", ""),
            },
            "wan": {
                "down": float(wan.get("downspeed", 0) or 0),
                "up": float(wan.get("upspeed", 0) or 0),
                "downTotal": float(wan.get("download", 0) or 0),
                "upTotal": float(wan.get("upload", 0) or 0),
            },
            "count": {
                "online": MiWiFiRouter._to_int(count.get("online", 0)),
                "all": MiWiFiRouter._to_int(count.get("all", 0)),
            },
            "blocked": sorted({MiWiFiRouter.normalize_mac(m) for m in blocked}),
            "devices": merged,
            "history": [[t, d, u] for t, d, u in self.history],
            "cpu": float(cpu.get("load", 0) or 0),
            "mem": float(mem.get("usage", 0) or 0),
            "uptime": MiWiFiRouter._to_int(status.get("upTime", 0)),
            "last_update": time.strftime("%H:%M:%S"),
        }
        with self._lock:
            self._snapshot = snap

    # ------------------------------------------------------------ js_api
    def get_snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._snapshot, ensure_ascii=False))

    def get_config(self) -> dict:
        return {
            "host": self.cfg.get("host", ""),
            "username": self.cfg.get("username", "admin"),
            "poll_interval": self.cfg.get("poll_interval", 2),
            "has_password": bool(self.cfg.get("password")),
        }

    def test_connection(self, host: str, username: str, password: str) -> dict:
        """测试连接（不改动当前会话）。"""
        try:
            router = self._make_router(host.strip() or "192.168.31.1",
                                       username.strip() or "admin", password)
            router.login()
            info = router.get_init_info()
            router.close()
            return {
                "ok": True,
                "msg": f"连接成功：{info.get('routername', '')} "
                       f"({info.get('model', '')}) 固件 {info.get('romversion', '')}",
            }
        except (RouterAuthError, RouterUnreachable, RouterError) as err:
            return {"ok": False, "msg": str(err)}

    def save_settings(self, host: str, username: str, password: str,
                      poll_interval: int) -> dict:
        """保存设置；若提供密码则写钥匙串，随后重连。"""
        try:
            save_config(host, username, int(poll_interval), password or "")
        except OSError as err:
            return {"ok": False, "msg": f"保存设置失败: {err}"}
        self.cfg = load_config()
        if password:
            self.cfg["password"] = password
        if self.router is not None:
            self._stop.set()
            self.router.close()
            self.router = None
        return self.connect()

    def set_poll_interval(self, seconds: int) -> dict:
        try:
            self.cfg["poll_interval"] = max(1, min(int(seconds), 30))
            save_config(self.cfg["host"], self.cfg["username"], self.cfg["poll_interval"])
            return {"ok": True}
        except (OSError, ValueError):
            return {"ok": False, "msg": "间隔数值无效"}

    # ------------------------------------------------------------ 控制操作
    def _guard(self, key: str) -> bool:
        if key in self._busy:
            return False
        self._busy.add(key)
        return True

    def _release(self, key: str) -> None:
        self._busy.discard(key)

    def _run_action(self, key: str, fn, ok_msg: str, err_msg: str) -> dict:
        if not self._guard(key):
            return {"ok": False, "msg": "操作进行中，请稍候"}
        try:
            ok = fn()
            return {"ok": ok, "msg": ok_msg if ok else err_msg}
        except (RouterError, RouterUnreachable) as err:
            return {"ok": False, "msg": str(err)}
        finally:
            self._release(key)

    def block_device(self, mac: str) -> dict:
        return self._run_action(f"block:{mac}",
                                lambda: self.router.block_device(mac),
                                "已拉黑该设备", "拉黑失败")

    def unblock_device(self, mac: str) -> dict:
        return self._run_action(f"unblock:{mac}",
                                lambda: self.router.unblock_device(mac),
                                "已解除拉黑", "解除拉黑失败")

    def rename_device(self, mac: str, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "msg": "名称不能为空"}
        return self._run_action(f"rename:{mac}",
                                lambda: self.router.rename_device(mac, name),
                                "设备名已更新", "修改设备名失败")

    def reboot_router(self) -> dict:
        return self._run_action("reboot",
                                lambda: self.router.reboot(),
                                "已发送重启指令", "重启指令发送失败")

    def open_router_web(self) -> None:
        webbrowser.open(f"http://{self.cfg.get('host', '192.168.31.1')}")

    def quit(self) -> None:
        self._stop.set()
        if self.router is not None:
            self.router.close()


# ------------------------------------------------------------------ 本地 UI 服务
class UiHandler(http.server.SimpleHTTPRequestHandler):
    """UI 静态文件 + 只读快照回退接口（供浏览器模式/调试渲染验证）。"""

    def __init__(self, *args, app=None, directory=None, **kwargs):
        self.app = app
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, *args):  # 不刷屏
        pass

    def do_GET(self):
        if self.path == "/api/snapshot" and self.app is not None:
            body = json.dumps(self.app.get_snapshot(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()


def start_ui_server(app: Optional[App] = None) -> Any:
    """在 127.0.0.1 随机端口提供 ui/ 目录，返回 (server, port)。"""
    import socketserver

    handler = functools.partial(UiHandler, app=app, directory=UI_DIR)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def main() -> None:
    app = App()
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        # 供自动化验证：不弹窗口，仅检查依赖与配置可加载
        print(json.dumps({"ok": True, "config": app.get_config()}, ensure_ascii=False))
        return

    server, port = start_ui_server(app)
    url = f"http://127.0.0.1:{port}/index.html"

    # 已有保存的密码配置时，启动后自动连接
    if app.cfg.get("password"):
        threading.Thread(target=app.connect, daemon=True).start()

    api = webview.create_window(
        APP_TITLE,
        url=url,
        js_api=app,
        width=1180,
        height=800,
        min_size=(960, 640),
        background_color="#0f1117",
    )

    def on_closed():
        app.quit()
        server.shutdown()

    api.events.closed += on_closed
    webview.start(private_mode=False)


if __name__ == "__main__":
    main()
