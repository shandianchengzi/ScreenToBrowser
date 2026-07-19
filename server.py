"""
ScreenToBrowser HTTP 服务端
MJPEG 流式屏幕共享，通过 aiohttp 提供 Web 页面和实时画面流。
"""

import asyncio
import ctypes
from ctypes import wintypes
import io
import json
import logging
import signal
import sys
import time
from pathlib import Path

import mss
from aiohttp import web
from PIL import Image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("screen2browser")


def get_config_path() -> Path:
    """返回 config.json 的持久化路径（exe 所在目录或脚本所在目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "config.json"
    return Path(__file__).parent / "config.json"


CONFIG_PATH = get_config_path()

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """从 config.json 加载配置，缺失字段用默认值补全。"""
    default = {
        "capture_region": {"left": 0, "top": 0, "width": 1920, "height": 1080, "include_cursor": True},
        "server": {"host": "0.0.0.0", "port": 8080, "fps": 15},
    }
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            # 合并：用户值覆盖默认值
            for key in default:
                if key in user:
                    default[key].update(user[key])
        except Exception as e:
            log.warning("读取配置失败，使用默认值: %s", e)
    return default


def save_config(cfg: dict) -> None:
    """将配置写入 config.json。"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    log.info("配置已保存到 %s", CONFIG_PATH)


# ---------------------------------------------------------------------------
# Windows cursor capture (GDI + User32)
# ---------------------------------------------------------------------------

class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", wintypes.POINT),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HANDLE),
        ("hbmColor", wintypes.HANDLE),
    ]


class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", ctypes.c_long),
        ("bmWidth", ctypes.c_long),
        ("bmHeight", ctypes.c_long),
        ("bmWidthBytes", ctypes.c_long),
        ("bmPlanes", ctypes.c_short),
        ("bmBitsPixel", ctypes.c_short),
        ("bmBits", ctypes.c_void_p),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_short),
        ("biBitCount", ctypes.c_short),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

CURSOR_SHOWING = 0x00000001
DIB_RGB_COLORS = 0
DI_NORMAL = 0x0003

# 定义函数参数类型，确保 64 位句柄不会溢出
user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
user32.GetCursorInfo.restype = wintypes.BOOL
user32.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ICONINFO)]
user32.GetIconInfo.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.DrawIconEx.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HANDLE,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint, wintypes.HANDLE, wintypes.UINT,
]
user32.DrawIconEx.restype = wintypes.BOOL
gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID]
gdi32.GetObjectW.restype = ctypes.c_int
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.c_void_p, ctypes.c_uint,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL


def _draw_cursor_on_image(img: Image.Image, region: dict) -> Image.Image:
    """Windows: 在 PIL Image 上绘制当前鼠标光标。

    使用 GetCursorInfo + DrawIconEx 渲染光标，支持所有光标类型
    （标准箭头、文字 I 形、手型、彩色、单色、动画光标等）。
    """
    try:
        ci = CURSORINFO()
        ci.cbSize = ctypes.sizeof(CURSORINFO)
        if not user32.GetCursorInfo(ctypes.byref(ci)):
            log.debug("GetCursorInfo 失败")
            return img
        if not (ci.flags & CURSOR_SHOWING):
            log.debug("光标不可见 (flags=0x%x)", ci.flags)
            return img

        log.debug("光标位置: (%d, %d), hCursor=%s", ci.ptScreenPos.x, ci.ptScreenPos.y, ci.hCursor)

        ii = ICONINFO()
        if not user32.GetIconInfo(ci.hCursor, ctypes.byref(ii)):
            log.debug("GetIconInfo 失败")
            return img

        # 获取光标位图尺寸
        bm = BITMAP()
        hbm = ii.hbmColor if ii.hbmColor else ii.hbmMask
        ret = gdi32.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm))
        cw = bm.bmWidth
        ch = bm.bmHeight if ii.hbmColor else bm.bmHeight // 2
        log.debug("光标尺寸: %dx%d, hotspot=(%d,%d), GetObjectW=%d", cw, ch, ii.xHotspot, ii.yHotspot, ret)

        # 计算光标在捕获区域中的位置（减去热点偏移）
        dx = ci.ptScreenPos.x - region["left"] - ii.xHotspot
        dy = ci.ptScreenPos.y - region["top"] - ii.yHotspot
        log.debug("光标绘制位置: (%d, %d), 区域: %s", dx, dy, region)

        # 释放 GetIconInfo 分配的位图
        if ii.hbmMask:
            gdi32.DeleteObject(ii.hbmMask)
        if ii.hbmColor:
            gdi32.DeleteObject(ii.hbmColor)

        # 创建内存 DC，将帧像素拷入 DIB Section
        w, h = img.size
        src_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(src_dc)

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h  # 自上而下
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0  # BI_RGB

        bits = ctypes.c_void_p()
        hbm_out = gdi32.CreateDIBSection(
            mem_dc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0,
        )
        old_bmp = gdi32.SelectObject(mem_dc, hbm_out)

        # 将当前帧像素复制到 DIB Section
        raw = img.tobytes("raw", "BGRX")
        ctypes.memmove(bits, raw, len(raw))

        # 在帧上绘制光标（AND/XOR 掩码合成）
        draw_ret = user32.DrawIconEx(mem_dc, dx, dy, ci.hCursor, cw, ch, 0, None, DI_NORMAL)
        if not draw_ret:
            log.debug("DrawIconEx 失败, GetLastError=%d", ctypes.GetLastError())

        # 读回合成后的像素（从 DIB Section 指针读取原始字节）
        pixel_data = ctypes.string_at(bits, w * h * 4)
        result = Image.frombytes("RGB", (w, h), pixel_data, "raw", "BGRX")

        # 清理 GDI 对象
        gdi32.SelectObject(mem_dc, old_bmp)
        gdi32.DeleteObject(hbm_out)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, src_dc)

        return result
    except Exception:
        log.exception("绘制光标时出错")
        return img


# ---------------------------------------------------------------------------
# Screen capture
# ---------------------------------------------------------------------------

class ScreenCapture:
    """使用 mss 高速截屏，输出 JPEG bytes。"""

    def __init__(self, region: dict, fps: int = 15, include_cursor: bool = True):
        self.region = {
            "left": region["left"],
            "top": region["top"],
            "width": region["width"],
            "height": region["height"],
        }
        self.fps = fps
        self.include_cursor = include_cursor
        self._sct = mss.mss()
        self._frame_interval = 1.0 / max(fps, 1)

    def update_region(self, region: dict) -> None:
        self.region = {
            "left": region["left"],
            "top": region["top"],
            "width": region["width"],
            "height": region["height"],
        }

    def update_fps(self, fps: int) -> None:
        self.fps = max(fps, 1)
        self._frame_interval = 1.0 / self.fps

    def grab_jpeg(self, quality: int = 70) -> bytes:
        """截取一帧并编码为 JPEG bytes。"""
        sct_img = self._sct.grab(self.region)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        if self.include_cursor:
            img = _draw_cursor_on_image(img, self.region)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()

    def close(self) -> None:
        self._sct.close()


# ---------------------------------------------------------------------------
# HTML viewer page
# ---------------------------------------------------------------------------

VIEWER_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ScreenToBrowser</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #1a1a2e; color: #eee;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    display: flex; flex-direction: column; align-items: center;
    min-height: 100vh;
  }
  .header {
    padding: 12px 24px; width: 100%;
    background: #16213e; text-align: center;
    font-size: 18px; font-weight: 600;
    border-bottom: 1px solid #0f3460;
  }
  .stream-container {
    flex: 1; display: flex; align-items: center; justify-content: center;
    padding: 16px; width: 100%;
  }
  img#stream {
    max-width: 100%; max-height: calc(100vh - 80px);
    border: 2px solid #0f3460; border-radius: 4px;
    background: #000;
  }
  .status {
    position: fixed; bottom: 12px; right: 16px;
    font-size: 13px; color: #888;
  }
</style>
</head>
<body>
  <div class="header">ScreenToBrowser — 实时屏幕共享</div>
  <div class="stream-container">
    <img id="stream" src="/stream" alt="屏幕共享流">
  </div>
  <div class="status" id="status"></div>
<script>
  const img = document.getElementById('stream');
  const status = document.getElementById('status');
  let fps = 0, lastTime = performance.now();
  img.onload = () => {
    fps++;
    const now = performance.now();
    if (now - lastTime >= 1000) {
      status.textContent = fps + ' fps';
      fps = 0; lastTime = now;
    }
  };
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class StreamApp:
    """管理 HTTP 服务和屏幕捕获。"""

    def __init__(self, config: dict | None = None):
        self.config = config if config is not None else load_config()
        self.cap = ScreenCapture(
            self.config["capture_region"],
            self.config["server"]["fps"],
            include_cursor=self.config["capture_region"].get("include_cursor", True),
        )
        self._app = web.Application()
        self._setup_routes()
        self._runner: web.AppRunner | None = None
        self._active_streams: int = 0

    # -- routes ---------------------------------------------------------------

    def _setup_routes(self) -> None:
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/stream", self._handle_stream)
        self._app.router.add_get("/api/config", self._handle_get_config)
        self._app.router.add_post("/api/config", self._handle_set_config)
        self._app.router.add_post("/api/stop", self._handle_stop)

    async def _handle_index(self, request: web.Request) -> web.Response:
        return web.Response(text=VIEWER_HTML, content_type="text/html")

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        """MJPEG 流端点 — 持续推送 JPEG 帧。"""
        self._active_streams += 1
        log.info("新客户端连接，当前流数: %d", self._active_streams)

        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Pragma": "no-cache",
            },
        )
        await response.prepare(request)

        try:
            while True:
                frame = await asyncio.get_event_loop().run_in_executor(
                    None, self.cap.grab_jpeg
                )
                await response.write(
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                    + frame + b"\r\n"
                )
                await asyncio.sleep(self.cap._frame_interval)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._active_streams -= 1
            log.info("客户端断开，剩余流数: %d", self._active_streams)
        return response

    async def _handle_get_config(self, request: web.Request) -> web.Response:
        return web.json_response(self.config)

    async def _handle_set_config(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "无效的 JSON"}, status=400)

        if "capture_region" in data:
            self.config["capture_region"].update(data["capture_region"])
            self.cap.update_region(self.config["capture_region"])
        if "server" in data:
            self.config["server"].update(data["server"])
            if "fps" in data["server"]:
                self.cap.update_fps(data["server"]["fps"])

        save_config(self.config)
        return web.json_response({"ok": True, "config": self.config})

    async def _handle_stop(self, request: web.Request) -> web.Response:
        log.info("收到停止请求")
        asyncio.get_event_loop().call_soon(self._shutdown)
        return web.json_response({"ok": True, "message": "服务正在停止..."})

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        host = self.config["server"]["host"]
        port = self.config["server"]["port"]
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        log.info("服务已启动: http://%s:%d", host, port)
        log.info("捕获区域: %s", self.config["capture_region"])

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            log.info("服务已停止")

    def _shutdown(self) -> None:
        """触发优雅关闭。"""
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(loop.stop)


# ---------------------------------------------------------------------------
# CLI entry point (可独立运行 server.py)
# ---------------------------------------------------------------------------

async def async_main() -> None:
    app = StreamApp()
    await app.start()

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def on_signal(*_):
        log.info("收到终止信号，正在关闭...")
        stop_event.set()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, on_signal)

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await app.stop()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
