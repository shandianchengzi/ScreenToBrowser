"""
ScreenToBrowser GUI 入口
全屏透明覆盖层，拖拽框选屏幕区域，启动 MJPEG 流服务并自动打开浏览器。
"""

import asyncio
import json
import logging
import socket
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("screen2browser")

CONFIG_PATH = Path(__file__).parent / "config.json"

# ---------------------------------------------------------------------------
# Config helpers (与 server.py 共用逻辑，保持独立可运行)
# ---------------------------------------------------------------------------

def load_config() -> dict:
    default = {
        "capture_region": {"left": 0, "top": 0, "width": 1920, "height": 1080},
        "server": {"host": "0.0.0.0", "port": 8080, "fps": 15},
    }
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            for key in default:
                if key in user:
                    default[key].update(user[key])
        except Exception:
            pass
    return default


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def find_free_port(start: int = 8080) -> int:
    """从 start 开始找一个可用端口。"""
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    return start


def get_local_ip() -> str:
    """获取本机局域网 IP。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Selection Overlay — 全屏透明窗口，拖拽选区
# ---------------------------------------------------------------------------

class SelectionOverlay:
    """全屏半透明覆盖层，用户拖拽鼠标框选捕获区域。"""

    # 颜色
    BG_COLOR = "#000000"
    OVERLAY_ALPHA = 0.3          # 覆盖层透明度
    SELECTION_COLOR = "#4fc3f7"  # 选区边框
    SELECTION_FILL = "#4fc3f7"   # 选区填充（半透明）

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ScreenToBrowser — 选择共享区域")

        # 获取虚拟屏幕范围（覆盖所有显示器）
        vleft = self.root.winfo_vrootx()
        vtop = self.root.winfo_vrooty()
        vwidth = self.root.winfo_vrootwidth()
        vheight = self.root.winfo_vrootheight()

        self.screen_left = vleft
        self.screen_top = vtop
        self.screen_width = vwidth
        self.screen_height = vheight

        # 全屏窗口
        self.root.geometry(f"{vwidth}x{vheight}+{vleft}+{vtop}")
        self.root.overrideredirect(True)  # 无边框
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self.OVERLAY_ALPHA)

        # Windows 专有：设置窗口不捕获鼠标（方便跨屏操作）
        if sys.platform == "win32":
            try:
                self.root.attributes("-transparentcolor", "")
            except Exception:
                pass

        # Canvas
        self.canvas = tk.Canvas(
            self.root,
            width=vwidth,
            height=vheight,
            bg=self.BG_COLOR,
            highlightthickness=0,
            cursor="crosshair",
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # 拖拽状态
        self._start_x = 0
        self._start_y = 0
        self._rect_id = None
        self._coord_text_id = None
        self.selection = None  # (left, top, width, height)

        # 绑定事件
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        # 底部提示
        self._draw_hint()

    def _draw_hint(self) -> None:
        """在屏幕底部绘制操作提示。"""
        cx = self.screen_width // 2 + self.screen_left
        cy = self.screen_height - 80 + self.screen_top

        # 提示文字（直接画在 canvas 上，覆盖层透明不影响显示）
        self.canvas.create_text(
            cx, cy,
            text="拖拽鼠标框选要共享的屏幕区域  |  按 ESC 取消",
            fill="#ffffff",
            font=("Microsoft YaHei UI", 14),
            anchor="center",
        )
        self.canvas.create_text(
            cx, cy + 30,
            text="松开鼠标后将自动启动服务并在浏览器中打开",
            fill="#aaaaaa",
            font=("Microsoft YaHei UI", 11),
            anchor="center",
        )

    def _on_press(self, event: tk.Event) -> None:
        self._start_x = event.x
        self._start_y = event.y
        if self._rect_id:
            self.canvas.delete(self._rect_id)
        if self._coord_text_id:
            self.canvas.delete(self._coord_text_id)

    def _on_drag(self, event: tk.Event) -> None:
        if self._rect_id:
            self.canvas.delete(self._rect_id)
        if self._coord_text_id:
            self.canvas.delete(self._coord_text_id)

        x0, y0 = self._start_x, self._start_y
        x1, y1 = event.x, event.y

        self._rect_id = self.canvas.create_rectangle(
            x0, y0, x1, y1,
            outline=self.SELECTION_COLOR,
            width=3,
            fill=self.SELECTION_FILL,
            stipple="gray50",  # 半透明填充效果
        )

        # 实时显示尺寸
        w, h = abs(x1 - x0), abs(y1 - y0)
        self._coord_text_id = self.canvas.create_text(
            min(x0, x1) + w // 2, min(y0, y1) - 15,
            text=f"{w} × {h}",
            fill="#ffffff",
            font=("Consolas", 12, "bold"),
            anchor="center",
        )

    def _on_release(self, event: tk.Event) -> None:
        x0, y0 = self._start_x, self._start_y
        x1, y1 = event.x, event.y

        left = min(x0, x1) + self.screen_left
        top = min(y0, y1) + self.screen_top
        width = abs(x1 - x0)
        height = abs(y1 - y0)

        if width < 10 or height < 10:
            log.info("选区太小，忽略")
            return

        self.selection = (left, top, width, height)
        log.info("选区: left=%d, top=%d, %dx%d", left, top, width, height)
        self.root.destroy()

    def run(self) -> tuple[int, int, int, int] | None:
        """运行选择界面，返回 (left, top, width, height) 或 None（用户取消）。"""
        self.root.mainloop()
        return self.selection


# ---------------------------------------------------------------------------
# Server launcher — 在子线程中启动 aiohttp 服务
# ---------------------------------------------------------------------------

def start_server_thread(cfg: dict) -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """在守护线程中启动 asyncio 事件循环和 HTTP 服务。"""
    from server import StreamApp

    loop = asyncio.new_event_loop()
    app = StreamApp()

    def _run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(app.start())
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(app.stop())
            loop.close()

    t = threading.Thread(target=_run, daemon=True, name="server")
    t.start()

    # 等待服务就绪
    import time
    for _ in range(50):
        time.sleep(0.1)
        if app._runner and app._runner.server:
            break

    return loop, t


# ---------------------------------------------------------------------------
# Status window — 服务运行中的小窗口
# ---------------------------------------------------------------------------

class StatusWindow:
    """服务运行状态窗口，显示局域网地址，提供停止按钮。"""

    def __init__(self, host: str, port: int, region: dict):
        self.root = tk.Tk()
        self.root.title("ScreenToBrowser — 正在共享")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        self._loop: asyncio.AbstractEventLoop | None = None

        # 居中显示
        w, h = 460, 280
        sx = (self.root.winfo_screenwidth() - w) // 2
        sy = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{sx}+{sy}")

        # 配色
        bg = "#1a1a2e"
        fg = "#e0e0e0"
        accent = "#4fc3f7"
        self.root.configure(bg=bg)

        # 标题
        tk.Label(
            self.root, text="📡 屏幕正在共享中", font=("Microsoft YaHei UI", 16, "bold"),
            bg=bg, fg=accent,
        ).pack(pady=(18, 10))

        # 局域网地址
        lan_ip = get_local_ip()
        url = f"http://{lan_ip}:{port}"
        tk.Label(
            self.root, text=f"局域网地址: {url}", font=("Consolas", 13),
            bg=bg, fg=fg,
        ).pack(pady=2)

        tk.Label(
            self.root, text=f"本机地址:   http://127.0.0.1:{port}", font=("Consolas", 13),
            bg=bg, fg="#888888",
        ).pack(pady=2)

        # 捕获区域信息
        region_text = (
            f"捕获区域:   左={region['left']}  上={region['top']}  "
            f"宽={region['width']}  高={region['height']}"
        )
        tk.Label(
            self.root, text=region_text, font=("Consolas", 11),
            bg=bg, fg="#888888",
        ).pack(pady=(8, 4))

        # 复制按钮
        btn_frame = tk.Frame(self.root, bg=bg)
        btn_frame.pack(pady=6)

        def copy_url():
            self.root.clipboard_clear()
            self.root.clipboard_append(url)

        tk.Button(
            btn_frame, text="复制局域网地址", command=copy_url,
            font=("Microsoft YaHei UI", 10), bg="#0f3460", fg=fg,
            activebackground="#16213e", activeforeground=fg,
            relief="flat", padx=12, pady=4, cursor="hand2",
        ).pack(side=tk.LEFT, padx=6)

        tk.Button(
            btn_frame, text="在浏览器中打开", command=lambda: webbrowser.open(url),
            font=("Microsoft YaHei UI", 10), bg="#0f3460", fg=fg,
            activebackground="#16213e", activeforeground=fg,
            relief="flat", padx=12, pady=4, cursor="hand2",
        ).pack(side=tk.LEFT, padx=6)

        # 停止按钮
        tk.Button(
            self.root, text="⏹ 停止共享", command=self._stop,
            font=("Microsoft YaHei UI", 12, "bold"), bg="#e74c3c", fg="#ffffff",
            activebackground="#c0392b", activeforeground="#ffffff",
            relief="flat", padx=24, pady=6, cursor="hand2",
        ).pack(pady=(14, 8))

        self.root.protocol("WM_DELETE_WINDOW", self._stop)

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def main() -> None:
    # 1. 选择共享区域
    log.info("启动区域选择界面...")
    overlay = SelectionOverlay()
    selection = overlay.run()

    if selection is None:
        log.info("用户取消，退出。")
        return

    left, top, width, height = selection

    # 2. 更新配置
    cfg = load_config()
    cfg["capture_region"] = {
        "left": left, "top": top,
        "width": width, "height": height,
    }
    port = find_free_port(cfg["server"]["port"])
    cfg["server"]["port"] = port
    save_config(cfg)
    log.info("配置已保存，端口: %d", port)

    # 3. 启动 HTTP 服务（守护线程）
    log.info("启动 HTTP 服务...")
    loop, _ = start_server_thread(cfg)

    # 4. 自动打开浏览器
    url = f"http://127.0.0.1:{port}"
    log.info("打开浏览器: %s", url)
    webbrowser.open(url)

    # 5. 显示状态窗口（主线程 Tk 主循环）
    status_win = StatusWindow("0.0.0.0", port, cfg["capture_region"])
    status_win.set_loop(loop)
    status_win.run()

    log.info("程序退出。")


if __name__ == "__main__":
    main()
