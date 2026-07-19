# ScreenToBrowser

将屏幕的全部或局部区域共享到局域网端口上，供浏览器直接打开并查看的 Python 软件。

## 功能

- 🖱️ **可视化框选** — 全屏透明覆盖层，鼠标拖拽选择共享区域，实时显示选区尺寸
- 🖱️ **鼠标光标捕获** — 共享画面中自动显示鼠标光标，支持所有光标类型
- 📡 **MJPEG 流推送** — 基于 `aiohttp` + `mss` 高速截屏，浏览器无需插件即可观看
- 🌐 **局域网访问** — 自动获取本机局域网 IP，同一网络下任意设备均可查看
- ⚙️ **可配置** — `config.json` 记录捕获区域坐标、端口、帧率等参数
- 📦 **单文件 exe** — PyInstaller 打包，双击即用，无需 Python 环境

## 快速开始

### 方式一：直接运行 exe（推荐）

从 [Releases](../../releases) 下载 `ScreenToBrowser.exe`，双击运行。

### 方式二：从源码运行

```bash
# 克隆仓库
git clone https://github.com/shandianchengzi/ScreenToBrowser.git
cd ScreenToBrowser

# 安装依赖
pip install -r requirements.txt

# 运行
python main.py
```

## 使用流程

1. 双击运行后弹出 **全屏半透明覆盖层**
2. **拖拽鼠标**框选要共享的屏幕区域，松开鼠标确认
3. 程序自动写入配置并启动 HTTP 服务
4. 弹出的 **状态窗口**中可查看局域网地址、复制链接、调整帧率、或停止共享
5. 同一局域网下的其他设备打开该地址即可查看

> 按 `ESC` 可随时取消选择。

## 项目结构

```
ScreenToBrowser/
├── main.py            # GUI 入口（Tkinter 全屏覆盖层 + 服务启动）
├── server.py          # HTTP 服务端（MJPEG 流 + Web 查看页面 + REST API）
├── config.json        # 运行时配置（捕获区域、端口、帧率）
├── build.py           # PyInstaller 打包脚本
├── requirements.txt   # Python 依赖
└── dist/
    └── ScreenToBrowser.exe   # 打包输出（运行 build.py 生成）
```

## 配置说明

`config.json` 在首次框选后自动生成，也可手动编辑：

```json
{
  "capture_region": {
    "left": 0,
    "top": 0,
    "width": 1920,
    "height": 1080,
    "include_cursor": true
  },
  "server": {
    "host": "0.0.0.0",
    "port": 8080,
    "fps": 30
  }
}
```

| 字段 | 说明 |
|------|------|
| `capture_region.left` | 捕获区域左上角 X 坐标（像素） |
| `capture_region.top` | 捕获区域左上角 Y 坐标（像素） |
| `capture_region.width` | 捕获区域宽度（像素） |
| `capture_region.height` | 捕获区域高度（像素） |
| `capture_region.include_cursor` | 是否在共享画面中显示鼠标光标，默认 `true` |
| `server.host` | 监听地址，`0.0.0.0` 表示所有网卡 |
| `server.port` | 监听端口，被占用时自动递增 |
| `server.fps` | 帧率，越高画面越流畅，CPU 占用也越高，默认 `30` |

## API 端点

| 路径 | 方法 | 说明 |
|------|------|------|
| `/` | GET | Web 查看页面 |
| `/stream` | GET | MJPEG 实时流（`<img src="/stream">`） |
| `/api/config` | GET | 获取当前配置 |
| `/api/config` | POST | 更新配置（JSON body） |
| `/api/stop` | POST | 停止服务 |

## 打包

```bash
python build.py
# 输出: dist/ScreenToBrowser.exe（约 30MB）
```

## 依赖

| 包 | 用途 |
|----|------|
| [aiohttp](https://docs.aiohttp.org/) | 异步 HTTP 服务器 |
| [mss](https://python-mss.readthedocs.io/) | 高速屏幕截图 |
| [Pillow](https://pillow.readthedocs.io/) | 图像编码（JPEG） |
| [PyInstaller](https://pyinstaller.org/) | 打包为 exe |

## 许可证

[Apache License 2.0](LICENSE)
