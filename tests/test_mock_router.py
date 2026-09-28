# -*- coding: utf-8 -*-
"""端到端测试：用 Mock MiWiFi 路由器服务验证 router_api 登录与全部接口。

运行：cd 项目目录 && .venv/bin/python tests/test_mock_router.py
"""
from __future__ import annotations

import hashlib
import http.server
import json
import random
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from router_api import MiWiFiRouter, RouterAuthError  # noqa: E402

KEY = "a2ffa5c9be07488bbb04a3a47d3c5f6a"
DEVICE_ID = "123456"
PASSWORD = "mypassword123"
TOKEN = "testtoken123456"

DEVICES = [
    {
        "mac": "AA:BB:CC:DD:EE:01", "name": "我的iPhone", "oname": "iPhone-15",
        "ip": [{"ip": "192.168.31.101"}], "online": 1, "type": "wifi", "isap": 0,
        "statistics": {"downspeed": 1024 * 500, "upspeed": 1024 * 80,
                       "download": 1024 * 1024 * 200, "upload": 1024 * 1024 * 30,
                       "online": "14285.11"},  # 真实固件返回浮点字符串
    },
    {
        "mac": "AA:BB:CC:DD:EE:02", "name": "", "oname": "DESKTOP-ABC",
        "ip": [{"ip": "192.168.31.102"}], "online": 1, "type": "line", "isap": 0,
        "statistics": {"downspeed": 0, "upspeed": 0,
                       "download": 1024 * 1024 * 500, "upload": 1024 * 1024 * 90,
                       "online": 300},
    },
    {
        "mac": "AA:BB:CC:DD:EE:03", "name": "旧手机", "oname": "xiaomi-9",
        "ip": [{"ip": "192.168.31.103"}], "online": 0, "type": "wifi", "isap": 0,
        "statistics": {"downspeed": 0, "upspeed": 0, "download": 0, "upload": 0, "online": 0},
    },
]

STATUS = {
    "code": 0,
    "upTime": 86400 * 3 + 3600 * 2,
    "dev": [
        {"mac": "AA:BB:CC:DD:EE:01", "downspeed": "614400.5", "upspeed": "92160.2",
         "download": 1024 * 1024 * 210, "upload": 1024 * 1024 * 31},
        {"mac": "AA:BB:CC:DD:EE:02", "downspeed": 0, "upspeed": 0,
         "download": 1024 * 1024 * 500, "upload": 1024 * 1024 * 90},
    ],
    "wan": {"downspeed": 1024 * 600, "upspeed": 1024 * 90,
            "download": 1024 * 1024 * 5000, "upload": 1024 * 1024 * 900,
            "history": [1, 2, 3]},
    "count": {"online": 2, "all": 30},
    "cpu": {"load": 0.32, "core": 2, "hz": "1200MHz"},
    "mem": {"usage": 0.41, "total": "256MB"},
    "hardware": {"platform": "RA81", "version": "1.0.60"},
}

WIFI_DEVICES = [
    {"mac": "AA:BB:CC:DD:EE:01", "wifiIndex": 2, "signal": -45},
    {"mac": "AA:BB:CC:DD:EE:03", "wifiIndex": 1, "signal": -62},
]

# 模拟真实固件结构：flist 是「全部设备 + authority 上网权限」视图，
# authority.wan=0 表示已拉黑（禁止上网）
BLOCKED = {"weblist": [], "flist": [
    {"mac": "AA:BB:CC:DD:EE:01", "authority": {"wan": 1, "lan": 1, "admin": 1}},
    {"mac": "AA:BB:CC:DD:EE:02", "authority": {"wan": 1, "lan": 1, "admin": 1}},
    {"mac": "AA:BB:CC:DD:EE:03", "authority": {"wan": 0, "lan": 0, "admin": 0}},
]}

# 演示模式下 WAN 速度随机游走，模拟真实流量
_wan_state = {"down": 1024 * 300, "up": 1024 * 45}


def _wan_speed():
    _wan_state["down"] = max(1024 * 8,
                             _wan_state["down"] + random.uniform(-1024 * 220, 1024 * 220))
    _wan_state["up"] = max(1024 * 2,
                           _wan_state["up"] + random.uniform(-1024 * 60, 1024 * 60))
    return int(_wan_state["down"]), int(_wan_state["up"])


class MockRouter(http.server.BaseHTTPRequestHandler):
    calls: list = []
    login_ok: bool = True
    last_form: dict = {}

    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        MockRouter.calls.append(("GET", self.path))
        if self.path == "/cgi-bin/luci/web":
            html = ("<html><head><script>\n"
                    f"    var key = '{KEY}';\n"
                    f"    var deviceId = '{DEVICE_ID}';\n"
                    "</script></head></html>")
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        m = re.match(r"^/cgi-bin/luci/;stok=([^/]+)/(api/.+)$", self.path)
        if m:
            stok, api_path = m.group(1), m.group(2)
            if stok != TOKEN:
                self._json({"code": 401, "msg": "token invalid"})
                return
            if api_path == "api/misystem/devicelist":
                self._json({"code": 0, "list": DEVICES})
            elif api_path == "api/misystem/status":
                down, up = _wan_speed()
                status = dict(STATUS)
                status["wan"] = dict(STATUS["wan"], downspeed=down, upspeed=up)
                self._json(status)
            elif api_path == "api/xqnetwork/wifi_connect_devices":
                self._json({"code": 0, "list": WIFI_DEVICES})
            elif api_path == "api/xqnetwork/wifi_macfilter_info":
                self._json(BLOCKED)
            elif api_path == "api/xqsystem/init_info":
                self._json({"code": 0, "routername": "Redmi-X3000",
                            "model": "Redmi Router X3000", "isRedmi": 1,
                            "romversion": "1.0.60", "hardware": "RA81"})
            elif api_path == "api/xqsystem/reboot":
                self._json({"code": 0})
            else:
                self._json({"code": -1, "msg": f"unknown {api_path}"})
            return
        self._json({"code": -1, "msg": "not found"}, 404)

    def do_POST(self):
        MockRouter.calls.append(("POST", self.path))
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        if self.path == "/cgi-bin/luci/api/xqsystem/login":
            from urllib.parse import parse_qs
            form = {k: v[0] for k, v in parse_qs(raw).items()}
            MockRouter.last_form = form
            if not MockRouter.login_ok:
                self._json({"code": 401, "msg": "auth failed"})
                return
            self._json({"code": 0, "token": TOKEN, "url": "http://x/cgi-bin/luci/web"})
            return
        m = re.match(r"^/cgi-bin/luci/;stok=([^/]+)/(api/.+)$", self.path)
        if m:
            stok, api_path = m.group(1), m.group(2)
            if stok != TOKEN:
                self._json({"code": 401, "msg": "token invalid"})
                return
            from urllib.parse import parse_qs
            form = {k: v[0] for k, v in parse_qs(raw).items()}
            MockRouter.last_form = form
            if api_path == "api/xqnetwork/set_mac_filter":
                MockRouter.calls.append(("SET_MAC_FILTER", form))
                self._json({"code": 0})
            elif api_path == "api/xqsystem/set_device_nickname":
                MockRouter.calls.append(("SET_NICKNAME", form))
                self._json({"code": 0})
            else:
                self._json({"code": -1, "msg": f"unknown {api_path}"})
            return
        self._json({"code": -1, "msg": "not found"}, 404)


def start_mock() -> http.server.HTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockRouter)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def serve_forever(port: int = 18080) -> None:
    """常驻演示服务：--serve [端口] 启动，供界面演示/截图验证使用。"""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), MockRouter)
    print(f"Mock 路由器常驻服务: http://127.0.0.1:{port}  (Ctrl+C 停止)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ✓", msg)


def main() -> int:
    server = start_mock()
    port = server.server_address[1]
    host = f"127.0.0.1:{port}"
    print(f"Mock 路由器已启动: http://{host}")

    router = MiWiFiRouter(host=host, username="admin", password=PASSWORD)
    token = router.login()
    expect(token == TOKEN, "登录成功且返回正确 token")

    # 校验签名：sha1(nonce + sha1(password + key))
    nonce = MockRouter.last_form["nonce"]
    expect(MockRouter.last_form["username"] == "admin", "登录提交用户名 admin")
    expect(MockRouter.last_form["logtype"] == "2", "登录提交 logtype=2")
    expected = hashlib.sha1(
        (nonce + hashlib.sha1((PASSWORD + KEY).encode()).hexdigest()).encode()
    ).hexdigest()
    expect(MockRouter.last_form["password"] == expected,
           "登录密码签名与协议一致 sha1(nonce + sha1(pwd + key))")

    info = router.get_init_info()
    expect(info.get("model") == "Redmi Router X3000", "init_info 正确识别机型")

    devices = router.get_devices()
    merged = MiWiFiRouter.merge_devices(
        devices, STATUS["dev"], WIFI_DEVICES)
    expect(len(merged) == 3, "设备列表解析出 3 台设备")
    d1 = merged[0]
    expect(d1["name"] == "我的iPhone", "设备名取自 name 字段")
    expect(d1["ip"] == "192.168.31.101", "IP 解析正确")
    expect(d1["downspeed"] == 1024 * 600, "下行速度从 status 合并（600 KB/s）")
    expect(d1["upspeed"] == 1024 * 90, "上行速度从 status 合并（90 KB/s）")
    expect(d1["online_secs"] == 14285, "浮点字符串在线时长安全取整（14285.11 → 14285）")
    expect(d1["band"] == "5G" and d1["signal"] == -45, "无线频段与信号合并正确")
    expect(merged[1]["type"] == "有线", "有线设备类型识别正确")
    expect(merged[2]["online"] is False, "离线设备状态正确")

    blocked = router.get_blocked_devices()
    expect(len(blocked) == 1 and blocked[0]["mac"] == "AA:BB:CC:DD:EE:03",
           "拉黑列表解析正确")

    ok = router.block_device("AA:BB:CC:DD:EE:02")
    expect(ok, "拉黑设备返回 code=0")
    last = MockRouter.calls[-1]
    expect(last[0] == "SET_MAC_FILTER" and last[1]["mac"] == "AA:BB:CC:DD:EE:02"
           and last[1]["wan"] == "0", "拉黑请求体正确 (mac + wan=0)")

    ok = router.unblock_device("AA:BB:CC:DD:EE:02")
    expect(ok, "解黑设备返回 code=0")
    last = MockRouter.calls[-1]
    expect(last[1]["wan"] == "1", "解黑请求体正确 (wan=1)")

    ok = router.rename_device("AA:BB:CC:DD:EE:01", "客厅电视")
    expect(ok, "改名返回 code=0")
    last = MockRouter.calls[-1]
    expect(last[1]["name"] == "客厅电视", "改名请求体正确")

    ok = router.reboot()
    expect(ok, "重启返回 code=0")

    # 密码错误场景
    bad = MiWiFiRouter(host=host, username="admin", password="wrongpass")
    MockRouter.login_ok = False
    try:
        bad.login()
        raise AssertionError("错误密码不应登录成功")
    except RouterAuthError:
        print("  ✓ 错误密码抛出 RouterAuthError")
    finally:
        MockRouter.login_ok = True

    router.close()
    server.shutdown()
    print("\n全部测试通过 ✓")
    return 0


if __name__ == "__main__":
    if "--serve" in sys.argv:
        idx = sys.argv.index("--serve")
        port = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else 18080
        serve_forever(port)
    else:
        sys.exit(main())
