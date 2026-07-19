"""
ScreenToBrowser HTTP 服务端
MJPEG 流式屏幕共享，通过 aiohttp 提供 Web 页面和实时画面流。
"""

import asyncio
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import logging
import os
import secrets
import signal
import sys
import time
from pathlib import Path

import mss
import pyautogui
import pyperclip
from aiohttp import web
from PIL import Image

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

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
        "server": {"host": "0.0.0.0", "port": 8080, "fps": 30, "password": ""},
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
# Authentication
# ---------------------------------------------------------------------------

_sessions: dict[str, bool] = {}
_nonces: dict[str, float] = {}


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _get_cookie(request: web.Request, name: str) -> str | None:
    cookie = request.headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith(f"{name}="):
            return part.split("=", 1)[1]
    return None


def _check_auth(request: web.Request) -> bool:
    """检查请求是否已认证。如果未设置密码则直接通过。"""
    password = request.app.get("password", "")
    if not password:
        return True
    session_id = _get_cookie(request, "session")
    if session_id and _sha256_hex(session_id) in _sessions:
        return True
    return False


# ---------------------------------------------------------------------------
# Remote input — keyboard/mouse simulation
# ---------------------------------------------------------------------------

# Windows keycode 映射（evdev code -> pyautogui key name）
WIN_KEY_MAP = {
    1: 'escape', 2: '1', 3: '2', 4: '3', 5: '4', 6: '5', 7: '6',
    8: '7', 9: '8', 10: '9', 11: '0', 12: '-', 13: '=',
    14: 'backspace', 15: 'tab', 16: 'q', 17: 'w', 18: 'e', 19: 'r',
    20: 't', 21: 'y', 22: 'u', 23: 'i', 24: 'o', 25: 'p',
    26: '[', 27: ']', 28: 'enter', 29: 'ctrlleft',
    30: 'a', 31: 's', 32: 'd', 33: 'f', 34: 'g', 35: 'h',
    36: 'j', 37: 'k', 38: 'l', 39: ';', 40: "'", 41: '`',
    42: 'shiftleft', 43: '\\', 44: 'z', 45: 'x', 46: 'c', 47: 'v',
    48: 'b', 49: 'n', 50: 'm', 51: ',', 52: '.', 53: '/',
    56: 'altleft', 57: 'space', 58: 'capslock',
    59: 'f1', 60: 'f2', 61: 'f3', 62: 'f4', 63: 'f5', 64: 'f6',
    65: 'f7', 66: 'f8', 67: 'f9', 68: 'f10', 87: 'f11', 88: 'f12',
    102: 'home', 103: 'up', 104: 'pageup', 105: 'left',
    106: 'right', 107: 'end', 108: 'down', 109: 'pagedown',
    110: 'insert', 111: 'delete',
    125: 'win', 210: 'printscreen', 464: 'f15',
}


def _send_keys(codes_str: str) -> None:
    """发送按键组合，codes_str 格式: '42:1,46:1,46:0,42:0'"""
    parts = codes_str.split(',')
    down_keys, up_keys = [], []
    for p in parts:
        code_str, state = p.split(':')
        key = WIN_KEY_MAP.get(int(code_str))
        if not key:
            continue
        (down_keys if state == '1' else up_keys).append(key)
    for k in down_keys:
        pyautogui.keyDown(k)
    for k in reversed(up_keys):
        pyautogui.keyUp(k)


def _handle_input(data: dict) -> None:
    """处理远程输入请求。"""
    data_type = data.get("type", "")
    data_value = data.get("value", "")
    term_mode = data.get("term_mode", False)

    if data_type == "mouse_move":
        pyautogui.moveRel(data.get("x", 0), data.get("y", 0), duration=0)
    elif data_type == "mouse_click":
        pyautogui.click(button=data.get("value", "left"))
    elif data_type == "mouse_down":
        pyautogui.mouseDown(button=data.get("value", "left"))
    elif data_type == "mouse_up":
        pyautogui.mouseUp(button=data.get("value", "left"))
    elif data_type == "text" and data_value:
        pyperclip.copy(data_value)
        pyautogui.hotkey('ctrl', 'v')
    elif data_type == "key" and data_value:
        code = int(data_value)
        if term_mode and code == 104:
            _send_keys("29:1,42:1,104:1,104:0,42:0,29:0")
        elif term_mode and code == 109:
            _send_keys("29:1,42:1,109:1,109:0,42:0,29:0")
        else:
            _send_keys(f"{data_value}:1,{data_value}:0")
    elif data_type == "key_combo" and data_value:
        codes = [int(c.strip()) for c in data_value.split(',')]
        combo = ",".join(f"{c}:1" for c in codes) + "," + ",".join(f"{c}:0" for c in reversed(codes))
        _send_keys(combo)
    elif data_type == "shortcut" and data_value:
        shortcut_map = {
            "ctrl+a": "29:1,30:1,30:0,29:0",
            "ctrl+z": "29:1,44:1,44:0,29:0",
            "ctrl+c": "29:1,46:1,46:0,29:0" if not term_mode else "29:1,42:1,46:1,46:0,42:0,29:0",
            "shift+insert": "42:1,110:1,110:0,42:0" if not term_mode else "29:1,42:1,47:1,47:0,42:0,29:0",
        }
        if data_value in shortcut_map:
            _send_keys(shortcut_map[data_value])


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
# Login page HTML
# ---------------------------------------------------------------------------

LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>ScreenToBrowser — 登录</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #1a1a2e; color: #eee; font-family: -apple-system, sans-serif;
         display: flex; justify-content: center; align-items: center; min-height: 100vh; }
  .login-box { width: 100%; max-width: 360px; padding: 20px; }
  h2 { margin-bottom: 15px; font-weight: 400; color: #888; text-align: center; }
  input { width: 100%; height: 56px; font-size: 18px; padding: 12px; background: #1e1e1e;
          color: white; border: 1px solid #333; border-radius: 8px; outline: none; }
  input:focus { border-color: #4fc3f7; }
  button { width: 100%; height: 56px; font-size: 20px; margin-top: 12px;
           background: #4fc3f7; color: #1a1a2e; border: none; border-radius: 8px;
           cursor: pointer; font-weight: 600; }
  button:active { background: #0398dc; }
  #msg { margin-top: 10px; color: #f87171; font-size: 14px; text-align: center; min-height: 20px; }
</style>
</head>
<body>
  <div class="login-box">
    <h2>ScreenToBrowser</h2>
    <input id="pw" type="password" placeholder="请输入访问密码" onkeydown="if(event.key==='Enter')login()">
    <button onclick="login()">进入</button>
    <div id="msg"></div>
  </div>
<script>
async function login() {
  const pw = document.getElementById('pw').value.trim();
  if (!pw) { document.getElementById('msg').textContent = '请输入密码'; return; }
  const r = await fetch('/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({password: pw})
  });
  if (r.ok) { window.location.reload(); }
  else { document.getElementById('msg').textContent = '密码错误'; document.getElementById('pw').value = ''; }
}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main viewer page HTML (with remote input panel)
# ---------------------------------------------------------------------------

VIEWER_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>ScreenToBrowser</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #121212; color: #eee; font-family: -apple-system, sans-serif;
       display: flex; height: 100vh; overflow: hidden; }

/* 左侧：屏幕共享 */
.main-area { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.header { padding: 8px 16px; background: #1a1a2e; font-size: 16px; font-weight: 600;
          border-bottom: 1px solid #0f3460; display: flex; align-items: center; justify-content: space-between; }
.header-left { display: flex; align-items: center; gap: 12px; }
#fps { color: #888; font-size: 13px; font-weight: 400; }
.stream-wrap { flex: 1; display: flex; align-items: center; justify-content: center;
               padding: 8px; overflow: hidden; background: #000; }
img#stream { max-width: 100%; max-height: 100%; object-fit: contain; }

/* 右侧面板 */
.panel { width: 320px; background: #1a1a2e; border-left: 1px solid #333;
         display: flex; flex-direction: column; overflow-y: auto; transition: width 0.2s; }
.panel.collapsed { width: 0; overflow: hidden; border-left: none; }
.panel-toggle { position: absolute; right: 320px; top: 50%; transform: translateY(-50%);
                width: 24px; height: 60px; background: #333; border: none; color: #aaa;
                cursor: pointer; border-radius: 6px 0 0 6px; font-size: 16px; z-index: 10;
                transition: right 0.2s; display: flex; align-items: center; justify-content: center; }
.panel.collapsed + .panel-toggle, .panel-toggle.collapsed { right: 0; }
.panel-toggle:hover { background: #444; color: #fff; }

/* 面板内部 */
.panel-section { padding: 12px; border-bottom: 1px solid #222; }
.panel-section:last-child { border-bottom: none; }
.panel-title { font-size: 13px; color: #888; margin-bottom: 8px; display: flex;
               align-items: center; justify-content: space-between; }
.panel-title label { display: flex; align-items: center; gap: 4px; cursor: pointer; font-size: 12px; color: #4fc3f7; }
.panel-title label input { accent-color: #4fc3f7; }

/* 文本输入 */
textarea { width: 100%; height: 80px; font-size: 14px; padding: 8px; background: #1e1e1e;
           color: white; border: 1px solid #333; border-radius: 6px; resize: none; touch-action: auto; }
.main-btn { width: 100%; height: 38px; font-size: 14px; margin-top: 6px; background: #2563eb;
            color: white; border: none; border-radius: 6px; cursor: pointer; }
.main-btn:active { background: #1d4ed8; }

/* 快捷按钮 */
.btn-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; margin-top: 6px; }
.sub-btn { height: 32px; font-size: 12px; background: #333; color: white; border: none;
           border-radius: 4px; cursor: pointer; }
.sub-btn:active { background: #555; }

/* 触摸板区域 */
.mouse-row { display: flex; gap: 6px; height: 140px; }
.click-btn { width: 48px; height: 100%; color: white; border: none; border-radius: 6px;
             font-size: 12px; font-weight: bold; cursor: pointer; }
.click-btn.left { background: #dc2626; }
.click-btn.left:active { background: #b91c1c; }
.click-btn.right { background: #7c3aed; }
.click-btn.right:active { background: #6d28d9; }
.touchpad { flex: 1; height: 100%; background: #222; border: 2px dashed #444; border-radius: 6px;
            display: flex; justify-content: center; align-items: center; color: #555; font-size: 12px;
            touch-action: none; user-select: none; -webkit-user-select: none; }

/* 灵敏度 */
.sens-row { display: flex; align-items: center; gap: 6px; margin-top: 6px; }
.sens-row span { color: #666; font-size: 11px; min-width: 28px; }
.sens-row input[type=range] { flex: 1; height: 3px; -webkit-appearance: none; background: #444;
                              border-radius: 2px; outline: none; }
.sens-row input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; width: 12px; height: 12px;
                                                    background: #2563eb; border-radius: 50%; cursor: pointer; }

/* 键盘模式 */
.kb-row { display: flex; gap: 2px; margin-bottom: 2px; }
.kb-key { min-width: 16px; height: 28px; font-size: 9px; background: #333; color: white;
          border: none; border-radius: 3px; cursor: pointer; display: flex; justify-content: center;
          align-items: center; padding: 0 2px; flex: 1 1 auto; user-select: none; -webkit-user-select: none; }
.kb-key:active { background: #555; }
.kb-key.wide { flex: 1.5 1 auto; font-size: 8px; }
.kb-row:first-child .kb-key { font-size: 7px; height: 22px; }
.kb-key.space { flex: 5 1 auto; }
.kb-key.shift-active { background: #2563eb; }
.kb-key.alt-active { background: #2563eb !important; }

/* 数字键盘 + 触摸板（键盘模式） */
.kb-touchpad-row { display: flex; gap: 6px; height: 130px; margin-top: 4px; }
.numpad { display: grid; grid-template-columns: repeat(3, 1fr); gap: 2px; flex: 0 0 28%; }
.numpad .kb-key { flex: none; width: 100%; height: 100%; font-size: 11px; }
.kb-touchpad-col { flex: 1; display: flex; flex-direction: column; gap: 4px; }
.kb-touchpad-col .touchpad { flex: 1; height: auto; }

/* 隐藏类 */
.hidden { display: none !important; }

/* FPS 计数 */
#panelFps { color: #666; font-size: 11px; }
</style>
</head>
<body>

<!-- 左侧：屏幕共享 -->
<div class="main-area">
  <div class="header">
    <div class="header-left">
      <span>ScreenToBrowser</span>
      <span id="fps"></span>
    </div>
  </div>
  <div class="stream-wrap">
    <img id="stream" src="/stream" alt="屏幕共享流">
  </div>
</div>

<!-- 右侧面板 -->
<div class="panel" id="panel">
  <!-- 文本输入 -->
  <div class="panel-section">
    <div class="panel-title">
      <span>远程输入</span>
      <label><input type="checkbox" id="termMode"> 终端模式</label>
    </div>
    <textarea id="text" placeholder="在这里输入文字..."></textarea>
    <button class="main-btn" onclick="sendText()">发送文本到电脑</button>
  </div>

  <!-- 快捷按钮 -->
  <div class="panel-section">
    <div class="btn-grid">
      <button class="sub-btn" onclick="sendAction('key','1')">Esc</button>
      <button class="sub-btn" onclick="sendAction('key','15')">Tab</button>
      <button class="sub-btn" onclick="sendAction('key','14')">退格</button>
      <button class="sub-btn" onclick="sendAction('mouse_click','right')">右键</button>
    </div>
    <div class="btn-grid">
      <button class="sub-btn" onclick="sendAction('key','103')">↑</button>
      <button class="sub-btn" onclick="sendAction('key','108')">↓</button>
      <button class="sub-btn" onclick="sendAction('key','105')">←</button>
      <button class="sub-btn" onclick="sendAction('key','106')">→</button>
    </div>
    <div class="btn-grid">
      <button class="sub-btn" onclick="sendAction('shortcut','ctrl+z')">撤销</button>
      <button class="sub-btn" onclick="sendAction('shortcut','ctrl+a')">全选</button>
      <button class="sub-btn" onclick="sendAction('shortcut','ctrl+c')">复制</button>
      <button class="sub-btn" onclick="sendAction('shortcut','shift+insert')">粘贴</button>
    </div>
  </div>

  <!-- 触摸板 -->
  <div class="panel-section" id="touchpadSection">
    <div class="panel-title">触摸板</div>
    <div class="mouse-row">
      <button class="sub-btn" style="width:36px;font-size:11px" onclick="sendAction('key','104')">PgUp</button>
      <div class="touchpad" id="pad">滑动移动光标<br>轻触=左键 双击=拖拽</div>
      <button class="sub-btn" style="width:36px;font-size:11px" onclick="sendAction('key','109')">PgDn</button>
      <button class="click-btn left" onclick="sendAction('mouse_click','left')">左键</button>
      <button class="click-btn right" onclick="sendAction('mouse_click','right')">右键</button>
    </div>
    <div class="sens-row">
      <span>灵敏</span>
      <input type="range" id="sensitivity" min="0.3" max="5" step="0.1" value="2">
      <span id="sensVal">2.0</span>
    </div>
  </div>

  <!-- 键盘模式（默认隐藏） -->
  <div class="panel-section hidden" id="keyboardSection">
    <div class="panel-title">虚拟键盘</div>
    <div id="keyboard">
      <div class="kb-row">
        <button class="kb-key wide" data-code="1">Esc</button>
        <button class="kb-key" data-code="59">F1</button><button class="kb-key" data-code="60">F2</button>
        <button class="kb-key" data-code="61">F3</button><button class="kb-key" data-code="62">F4</button>
        <button class="kb-key" data-code="63">F5</button><button class="kb-key" data-code="64">F6</button>
        <button class="kb-key" data-code="65">F7</button><button class="kb-key" data-code="66">F8</button>
        <button class="kb-key" data-code="67">F9</button><button class="kb-key" data-code="68">F10</button>
        <button class="kb-key" data-code="87">F11</button><button class="kb-key" data-code="88">F12</button>
      </div>
      <div class="kb-row">
        <button class="kb-key" data-code="41" data-label="`" data-shift="~">`</button>
        <button class="kb-key" data-code="2" data-label="1" data-shift="!">1</button>
        <button class="kb-key" data-code="3" data-label="2" data-shift="@">2</button>
        <button class="kb-key" data-code="4" data-label="3" data-shift="#">3</button>
        <button class="kb-key" data-code="5" data-label="4" data-shift="$">4</button>
        <button class="kb-key" data-code="6" data-label="5" data-shift="%">5</button>
        <button class="kb-key" data-code="7" data-label="6" data-shift="^">6</button>
        <button class="kb-key" data-code="8" data-label="7" data-shift="&amp;">7</button>
        <button class="kb-key" data-code="9" data-label="8" data-shift="*">8</button>
        <button class="kb-key" data-code="10" data-label="9" data-shift="(">9</button>
        <button class="kb-key" data-code="11" data-label="0" data-shift=")">0</button>
        <button class="kb-key" data-code="12" data-label="-" data-shift="_">-</button>
        <button class="kb-key" data-code="13" data-label="=" data-shift="+">=</button>
        <button class="kb-key wide" data-code="14">退格</button>
      </div>
      <div class="kb-row">
        <button class="kb-key wide" data-code="15">Tab</button>
        <button class="kb-key" data-code="16">Q</button><button class="kb-key" data-code="17">W</button>
        <button class="kb-key" data-code="18">E</button><button class="kb-key" data-code="19">R</button>
        <button class="kb-key" data-code="20">T</button><button class="kb-key" data-code="21">Y</button>
        <button class="kb-key" data-code="22">U</button><button class="kb-key" data-code="23">I</button>
        <button class="kb-key" data-code="24">O</button><button class="kb-key" data-code="25">P</button>
        <button class="kb-key" data-code="26" data-label="[" data-shift="{">[</button>
        <button class="kb-key" data-code="27" data-label="]" data-shift="}">]</button>
        <button class="kb-key" data-code="43" data-label="\\" data-shift="|">\\</button>
      </div>
      <div class="kb-row">
        <button class="kb-key wide" data-code="58">Caps</button>
        <button class="kb-key" data-code="30">A</button><button class="kb-key" data-code="31">S</button>
        <button class="kb-key" data-code="32">D</button><button class="kb-key" data-code="33">F</button>
        <button class="kb-key" data-code="34">G</button><button class="kb-key" data-code="35">H</button>
        <button class="kb-key" data-code="36">J</button><button class="kb-key" data-code="37">K</button>
        <button class="kb-key" data-code="38">L</button>
        <button class="kb-key" data-code="39" data-label=";" data-shift=":">;</button>
        <button class="kb-key" data-code="40" data-label="'" data-shift="&quot;">'</button>
        <button class="kb-key wide" data-code="28">Enter</button>
      </div>
      <div class="kb-row">
        <button class="kb-key wide" id="shiftKey" data-code="42">Shift</button>
        <button class="kb-key" data-code="44">Z</button><button class="kb-key" data-code="45">X</button>
        <button class="kb-key" data-code="46">C</button><button class="kb-key" data-code="47">V</button>
        <button class="kb-key" data-code="48">B</button><button class="kb-key" data-code="49">N</button>
        <button class="kb-key" data-code="50">M</button>
        <button class="kb-key" data-code="51" data-label="," data-shift="&lt;">,</button>
        <button class="kb-key" data-code="52" data-label="." data-shift="&gt;">.</button>
        <button class="kb-key" data-code="53" data-label="/" data-shift="?">/</button>
        <button class="kb-key" data-code="104">PgUp</button>
        <button class="kb-key" data-code="109">PgDn</button>
      </div>
      <div class="kb-row">
        <button class="kb-key wide" data-code="29">Ctrl</button>
        <button class="kb-key wide" data-code="125">Win</button>
        <button class="kb-key wide" data-code="56">Alt</button>
        <button class="kb-key space" data-code="57">Space</button>
        <button class="kb-key" data-code="210">PrtSc</button>
        <button class="kb-key" data-code="105">←</button>
        <button class="kb-key" data-code="103">↑</button>
        <button class="kb-key" data-code="108">↓</button>
        <button class="kb-key" data-code="106">→</button>
      </div>
    </div>
    <div class="kb-touchpad-row">
      <div class="numpad">
        <button class="kb-key" data-code="8">7</button><button class="kb-key" data-code="9">8</button>
        <button class="kb-key" data-code="10">9</button><button class="kb-key" data-code="5">4</button>
        <button class="kb-key" data-code="6">5</button><button class="kb-key" data-code="7">6</button>
        <button class="kb-key" data-code="2">1</button><button class="kb-key" data-code="3">2</button>
        <button class="kb-key" data-code="4">3</button><button class="kb-key" data-code="11">0</button>
        <button class="kb-key" data-code="52">.</button><button class="kb-key" data-code="28">Enter</button>
      </div>
      <div class="kb-touchpad-col">
        <div class="touchpad" id="pad2">触摸板</div>
        <div class="sens-row">
          <span>灵敏</span>
          <input type="range" id="sensitivity2" min="0.3" max="5" step="0.1" value="2">
          <span id="sensVal2">2.0</span>
        </div>
      </div>
    </div>
  </div>

  <!-- 模式切换 -->
  <div class="panel-section">
    <div class="panel-title">
      <label><input type="checkbox" id="kbMode"> 键盘模式</label>
      <span id="panelFps"></span>
    </div>
  </div>
</div>

<!-- 面板折叠按钮 -->
<button class="panel-toggle" id="panelToggle" onclick="togglePanel()">◀</button>

<script>
// === 面板折叠 ===
const panel = document.getElementById('panel');
const toggle = document.getElementById('panelToggle');
function togglePanel() {
  const collapsed = panel.classList.toggle('collapsed');
  toggle.textContent = collapsed ? '▶' : '◀';
  toggle.classList.toggle('collapsed', collapsed);
  localStorage.setItem('panel_collapsed', collapsed ? '1' : '');
}
if (localStorage.getItem('panel_collapsed') === '1') {
  panel.classList.add('collapsed');
  toggle.textContent = '▶';
  toggle.classList.toggle('collapsed', true);
}

// === FPS 计数 ===
const fpsEl = document.getElementById('fps');
const panelFpsEl = document.getElementById('panelFps');
const img = document.getElementById('stream');
let fpsCount = 0, fpsTime = performance.now();
img.onload = () => {
  fpsCount++;
  const now = performance.now();
  if (now - fpsTime >= 1000) {
    const txt = fpsCount + ' fps';
    fpsEl.textContent = txt;
    panelFpsEl.textContent = txt;
    fpsCount = 0; fpsTime = now;
  }
};

// === 键盘模式切换 ===
const kbModeCb = document.getElementById('kbMode');
const touchpadSection = document.getElementById('touchpadSection');
const keyboardSection = document.getElementById('keyboardSection');
kbModeCb.addEventListener('change', () => {
  const active = kbModeCb.checked;
  touchpadSection.classList.toggle('hidden', active);
  keyboardSection.classList.toggle('hidden', !active);
  document.activeElement?.blur();
});

// === 数据发送 ===
async function postData(payload, quiet=false) {
  payload.term_mode = document.getElementById('termMode').checked;
  try {
    const r = await fetch('/api/input', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    if (r.status === 403) window.location.reload();
  } catch(e) {}
}
function sendText() {
  const t = document.getElementById('text');
  if (t.value.trim() === '') return;
  postData({type:'text', value:t.value});
  t.value = '';
}
function sendAction(type, value) {
  postData({type:type, value:value});
}

// === 触摸板 ===
const sensSlider = document.getElementById('sensitivity');
const sensVal = document.getElementById('sensVal');
const sensSlider2 = document.getElementById('sensitivity2');
const sensVal2 = document.getElementById('sensVal2');
const TAP_THRESHOLD = 10;
const DOUBLE_TAP_MS = 200;

const savedSens = localStorage.getItem('stb_sensitivity');
if (savedSens) {
  sensSlider.value = savedSens; sensVal.textContent = parseFloat(savedSens).toFixed(1);
  sensSlider2.value = savedSens; sensVal2.textContent = parseFloat(savedSens).toFixed(1);
}
function syncSens(src) {
  const v = parseFloat(src.value).toFixed(1);
  localStorage.setItem('stb_sensitivity', src.value);
  if (src === sensSlider) { sensVal.textContent = v; sensSlider2.value = src.value; sensVal2.textContent = v; }
  else { sensVal2.textContent = v; sensSlider.value = src.value; sensVal.textContent = v; }
}
sensSlider.addEventListener('input', () => syncSens(sensSlider));
sensSlider2.addEventListener('input', () => syncSens(sensSlider2));

function setupTouchpad(el, sensEl) {
  let lastX=0, lastY=0, totalDx=0, totalDy=0;
  let moveQueue={dx:0,dy:0}, moveTimer=null;
  let padState='idle', tapTimeout=null, lastTapTime=0, isDragging=false, inputType=null;

  function padSendMove() {
    let sx=Math.round(moveQueue.dx), sy=Math.round(moveQueue.dy);
    if (sx!==0||sy!==0) postData({type:'mouse_move',x:sx,y:sy}, true);
    moveQueue={dx:0,dy:0}; moveTimer=null;
  }
  function handleStart(cx,cy) {
    lastX=cx; lastY=cy; totalDx=0; totalDy=0;
    el.style.background='#2a2a2a';
    if (padState==='pending_tap') {
      clearTimeout(tapTimeout); padState='dragging'; isDragging=true;
      postData({type:'mouse_down',value:'left'}, true); el.style.background='#333';
    } else if (padState==='idle'&&lastTapTime&&Date.now()-lastTapTime<DOUBLE_TAP_MS) {
      padState='dragging'; isDragging=true;
      postData({type:'mouse_down',value:'left'}, true); el.style.background='#333';
    }
  }
  function handleMove(cx,cy) {
    let dx=cx-lastX, dy=cy-lastY; lastX=cx; lastY=cy;
    totalDx+=Math.abs(dx); totalDy+=Math.abs(dy);
    let s = parseFloat(sensEl.value);
    moveQueue.dx+=dx*s; moveQueue.dy+=dy*s;
    if (!moveTimer) moveTimer=setTimeout(padSendMove, 40);
  }
  function handleEnd() {
    let isTap=totalDx<TAP_THRESHOLD&&totalDy<TAP_THRESHOLD;
    if (padState==='dragging') {
      postData({type:'mouse_up',value:'left'}, true);
      padState='idle'; isDragging=false; el.style.background='#222'; return;
    }
    if (isTap&&padState==='idle') {
      padState='pending_tap'; lastTapTime=Date.now();
      tapTimeout=setTimeout(()=>{ if(padState==='pending_tap'&&!isDragging){postData({type:'mouse_click',value:'left'},true);padState='idle';} }, DOUBLE_TAP_MS);
    } else if (padState==='pending_tap') { clearTimeout(tapTimeout); padState='idle'; }
    el.style.background='#222';
  }
  el.addEventListener('touchstart', e=>{ inputType='touch'; handleStart(e.touches[0].clientX,e.touches[0].clientY); });
  el.addEventListener('touchmove', e=>{ if(inputType!=='touch'||e.touches.length!==1)return; handleMove(e.touches[0].clientX,e.touches[0].clientY); });
  el.addEventListener('touchend', ()=>{ if(inputType!=='touch')return; handleEnd(); inputType=null; });
  el.addEventListener('mousedown', e=>{ e.preventDefault(); if(inputType==='touch')return; inputType='mouse'; handleStart(e.clientX,e.clientY); });
  window.addEventListener('mousemove', e=>{ if(inputType!=='mouse')return; handleMove(e.clientX,e.clientY); });
  window.addEventListener('mouseup', ()=>{ if(inputType!=='mouse')return; handleEnd(); inputType=null; });
  el.addEventListener('contextmenu', e=>e.preventDefault());
}
setupTouchpad(document.getElementById('pad'), sensSlider);
setupTouchpad(document.getElementById('pad2'), sensSlider2);

// === 虚拟键盘 ===
let shiftActive=false, capsLockActive=false, ctrlActive=false;
const shiftKeyEl=document.getElementById('shiftKey');
const ctrlKeyEl=document.querySelector('[data-code="29"]');
const capsLockKey=document.querySelector('[data-code="58"]');
const letterCodes=new Set([16,17,18,19,20,21,22,23,24,25,30,31,32,33,34,35,36,37,38,44,45,46,47,48,49,50]);

function updateShiftDisplay() {
  document.querySelectorAll('.kb-key[data-shift]').forEach(k=>{ k.textContent=shiftActive?k.dataset.shift:k.dataset.label; });
}

const keyboardEl=document.getElementById('keyboard');
let longPressTimer=null, longPressKey=null, swipeStartX=0, swipeStartY=0, swipeActiveKey=null, swipeTriggered=false;

keyboardEl.addEventListener('touchstart', e=>{
  const key=e.target.closest('.kb-key'); if(!key)return; e.preventDefault();
  const code=parseInt(key.getAttribute('data-code')); if(!code)return;
  if(key.id==='shiftKey'||key.dataset.code==='58'||key.dataset.code==='29')return;
  const touch=e.touches[0]; swipeStartX=touch.clientX; swipeStartY=touch.clientY;
  swipeActiveKey=key; swipeTriggered=false; key.style.background='#555';
  if(key.dataset.shift) longPressTimer=setTimeout(()=>{ longPressKey=key; key.classList.add('long-press'); key.textContent=key.dataset.shift; }, 500);
}, {passive:false});

keyboardEl.addEventListener('touchmove', e=>{
  if(!swipeActiveKey||!swipeActiveKey.dataset.shift)return;
  const touch=e.touches[0]; const diffY=swipeStartY-touch.clientY;
  if(diffY>30&&!swipeTriggered){ swipeTriggered=true; if(longPressTimer){clearTimeout(longPressTimer);longPressTimer=null;} swipeActiveKey.classList.add('alt-active'); swipeActiveKey.textContent=swipeActiveKey.dataset.shift; }
  else if(diffY<=0&&swipeTriggered){ swipeTriggered=false; swipeActiveKey.classList.remove('alt-active'); if(!longPressKey)swipeActiveKey.textContent=swipeActiveKey.dataset.label; }
}, {passive:false});

keyboardEl.addEventListener('touchend', e=>{
  const key=swipeActiveKey||e.target.closest('.kb-key'); if(!key)return;
  const code=parseInt(key.getAttribute('data-code')); if(!code)return;
  if(key.id==='shiftKey'||key.dataset.code==='58'||key.dataset.code==='29')return;
  key.style.background='';
  if(swipeTriggered){ key.classList.remove('alt-active'); key.textContent=key.dataset.label; postData({type:'key_combo',value:'42,'+code}); }
  else if(longPressTimer){ clearTimeout(longPressTimer); longPressTimer=null;
    if(ctrlActive&&shiftActive){postData({type:'key_combo',value:'29,42,'+code});ctrlActive=false;ctrlKeyEl.classList.remove('alt-active');shiftActive=false;shiftKeyEl.classList.remove('shift-active');updateShiftDisplay();}
    else if(ctrlActive){postData({type:'key_combo',value:'29,'+code});ctrlActive=false;ctrlKeyEl.classList.remove('alt-active');}
    else if(shiftActive||(capsLockActive&&letterCodes.has(code))){postData({type:'key_combo',value:'42,'+code});if(shiftActive){shiftActive=false;shiftKeyEl.classList.remove('shift-active');updateShiftDisplay();}}
    else postData({type:'key',value:code});
  } else if(longPressKey===key){ key.classList.remove('long-press'); key.textContent=key.dataset.label; postData({type:'key_combo',value:'42,'+code}); longPressKey=null; }
  else {
    if(ctrlActive&&shiftActive){postData({type:'key_combo',value:'29,42,'+code});ctrlActive=false;ctrlKeyEl.classList.remove('alt-active');shiftActive=false;shiftKeyEl.classList.remove('shift-active');updateShiftDisplay();}
    else if(ctrlActive){postData({type:'key_combo',value:'29,'+code});ctrlActive=false;ctrlKeyEl.classList.remove('alt-active');}
    else if(shiftActive){postData({type:'key_combo',value:'42,'+code});shiftActive=false;shiftKeyEl.classList.remove('shift-active');updateShiftDisplay();}
    else postData({type:'key',value:code});
  }
  swipeActiveKey=null; swipeTriggered=false;
});

keyboardEl.addEventListener('touchcancel', ()=>{
  if(longPressTimer){clearTimeout(longPressTimer);longPressTimer=null;}
  if(longPressKey){longPressKey.classList.remove('long-press');longPressKey.textContent=longPressKey.dataset.label;longPressKey=null;}
  if(swipeActiveKey){swipeActiveKey.classList.remove('alt-active');if(swipeActiveKey.dataset.shift)swipeActiveKey.textContent=swipeActiveKey.dataset.label;swipeActiveKey=null;}
  swipeTriggered=false;
});

// Shift/Caps/Ctrl 点击切换
shiftKeyEl.addEventListener('touchstart', e=>{ e.preventDefault(); shiftActive=!shiftActive; shiftKeyEl.classList.toggle('shift-active',shiftActive); updateShiftDisplay(); });
capsLockKey.addEventListener('touchstart', e=>{ e.preventDefault(); capsLockActive=!capsLockActive; capsLockKey.classList.toggle('shift-active',capsLockActive); });
ctrlKeyEl.addEventListener('touchstart', e=>{ e.preventDefault(); ctrlActive=!ctrlActive; ctrlKeyEl.classList.toggle('alt-active',ctrlActive); });

// 鼠标事件（桌面端）
keyboardEl.addEventListener('mousedown', e=>{
  const key=e.target.closest('.kb-key'); if(!key)return; e.preventDefault();
  const code=parseInt(key.getAttribute('data-code')); if(!code)return;
  if(key.id==='shiftKey'){shiftActive=!shiftActive;shiftKeyEl.classList.toggle('shift-active',shiftActive);updateShiftDisplay();return;}
  if(key.dataset.code==='58'){capsLockActive=!capsLockActive;capsLockKey.classList.toggle('shift-active',capsLockActive);return;}
  if(key.dataset.code==='29'){ctrlActive=!ctrlActive;ctrlKeyEl.classList.toggle('alt-active',ctrlActive);return;}
  key.style.background='#555';
  if(ctrlActive&&shiftActive){postData({type:'key_combo',value:'29,42,'+code});ctrlActive=false;ctrlKeyEl.classList.remove('alt-active');shiftActive=false;shiftKeyEl.classList.remove('shift-active');updateShiftDisplay();}
  else if(ctrlActive){postData({type:'key_combo',value:'29,'+code});ctrlActive=false;ctrlKeyEl.classList.remove('alt-active');}
  else if(shiftActive||(capsLockActive&&letterCodes.has(code))){postData({type:'key_combo',value:'42,'+code});if(shiftActive){shiftActive=false;shiftKeyEl.classList.remove('shift-active');updateShiftDisplay();}}
  else postData({type:'key',value:code});
});
keyboardEl.addEventListener('mouseup', e=>{ const key=e.target.closest('.kb-key'); if(key)key.style.background=''; });
keyboardEl.addEventListener('contextmenu', e=>e.preventDefault());

// 数字键盘
document.querySelectorAll('.numpad .kb-key').forEach(key=>{
  key.addEventListener('touchstart', e=>{ e.stopPropagation(); e.preventDefault(); key.style.background='#555'; }, {passive:false});
  key.addEventListener('touchend', e=>{ e.stopPropagation(); key.style.background='';
    const code=parseInt(key.getAttribute('data-code')); if(!code)return;
    if(ctrlActive){postData({type:'key_combo',value:'29,'+code});ctrlActive=false;ctrlKeyEl.classList.remove('alt-active');}
    else if(shiftActive){postData({type:'key_combo',value:'42,'+code});shiftActive=false;shiftKeyEl.classList.remove('shift-active');updateShiftDisplay();}
    else postData({type:'key',value:code});
  }, {passive:false});
  key.addEventListener('mousedown', e=>{ e.preventDefault(); e.stopPropagation(); key.style.background='#555';
    const code=parseInt(key.getAttribute('data-code')); if(!code)return;
    postData({type:'key',value:code});
  });
  key.addEventListener('mouseup', e=>{ e.stopPropagation(); key.style.background=''; });
});
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
        self._app["password"] = self.config["server"].get("password", "")
        self._setup_routes()
        self._runner: web.AppRunner | None = None
        self._active_streams: int = 0

    # -- routes ---------------------------------------------------------------

    def _setup_routes(self) -> None:
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/stream", self._handle_stream)
        self._app.router.add_post("/login", self._handle_login)
        self._app.router.add_post("/api/input", self._handle_input)
        self._app.router.add_get("/api/config", self._handle_get_config)
        self._app.router.add_post("/api/config", self._handle_set_config)
        self._app.router.add_post("/api/stop", self._handle_stop)

    async def _handle_index(self, request: web.Request) -> web.Response:
        if not _check_auth(request):
            return web.Response(text=LOGIN_HTML, content_type="text/html")
        return web.Response(text=VIEWER_HTML, content_type="text/html")

    async def _handle_login(self, request: web.Request) -> web.Response:
        """处理密码登录。"""
        try:
            data = await request.json()
            password = data.get("password", "")
        except Exception:
            return web.json_response({"error": "无效请求"}, status=400)

        expected = request.app.get("password", "")
        if password == expected:
            session_id = secrets.token_hex(32)
            _sessions[_sha256_hex(session_id)] = True
            return web.Response(
                status=200,
                headers={"Set-Cookie": f"session={session_id}; Path=/; HttpOnly; SameSite=Strict"},
            )
        return web.json_response({"error": "密码错误"}, status=401)

    async def _handle_input(self, request: web.Request) -> web.Response:
        """处理远程输入请求。"""
        if not _check_auth(request):
            return web.json_response({"error": "未认证"}, status=403)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "无效请求"}, status=400)
        await asyncio.get_event_loop().run_in_executor(None, _handle_input, data)
        return web.json_response({"ok": True})

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        """MJPEG 流端点 — 持续推送 JPEG 帧。"""
        if not _check_auth(request):
            return web.Response(status=403, text="未认证")
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
