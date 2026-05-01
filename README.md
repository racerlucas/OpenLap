# OpenLap — 免费的赛车遥测视频叠加软件

**OpenLap** 是一款免费、开源的桌面应用，用于把遥测数据叠加到赛车视频中。  
支持 **RaceBox**、**AIM MyChron**、**MoTeC**、**GPX** 等数据源，所有处理都在本地完成：不订阅、不上云、无导出收费。

> 许可证：**GNU GPL v3**。永久免费，二次分发需保持开源。

---

## 快速开始（Windows）

无需 Python、无需单独安装 FFmpeg，开箱即用。

### 1）下载

前往 [Releases](https://github.com/racerlucas/OpenLap/releases/latest) 下载最新版压缩包。

### 2）解压

解压后会看到 `OpenLap.exe` 和 `_internal` 目录。

> 注意：`OpenLap.exe` 必须和 `_internal` 保持同目录，不能只移动 exe。

### 3）运行

双击 `OpenLap.exe`。

> 若出现 Windows SmartScreen：点击“更多信息” -> “仍要运行”。

---

## 便携与本地数据目录

OpenLap **不会**把你的节或视频上传到云端；处理与导出都在本机完成。你在 **Settings** 里配置的遥测/视频/导出文件夹仍由你自选（任意本地路径）。除此以外，**应用自身的配置与缓存**只写在一个「应用数据根」里，整块目录可与程序一起拷贝，达到**便携部署**语义：

| 运行方式 | 应用数据根 |
| :--- | :--- |
| Windows 解压版 / 打包目录里的 `OpenLap.exe` | 与 **`OpenLap.exe` 同一文件夹**（与 `_internal`、`Library/` 等并列） |
| 从源码 **`python main.py`** | `<仓库根>/.openlap/`（已加入 `.gitignore`，**不写用户主目录**） |

在同一应用数据根下通常会出现例如：`config.json`、`scan_cache.json`、`tracks/`（赛道 JSON）、`logs/`、`track_maps/`、`weather_cache.json`、`video_cache/`（多段视频拼接临时文件）、便携打包时的 **`Library/ffmpeg/`**、**`racebox_auth.json`**（RaceBox 网页登录缓存）、**`ms-playwright/`**（RaceBox 云下载用的 Playwright Chromium，约百兆级）。

- **`OPENLAP_DATA_DIR`**：设为绝对路径可强制指定应用数据根（测试、CI、自定义盘符）。
- **升级自旧版**：若曾使用 `~/.openlap/` 下的配置/扫描缓存，或旧版 `%APPDATA%\OpenLap\racebox_auth.json`，在新数据根里还没有对应文件时，会**各自动迁移一次**（不删除旧文件）。
- **命令行安装 Chromium（可选）**：若运行 `playwright install chromium`，请先在**同一终端**设置 `PLAYWRIGHT_BROWSERS_PATH` 指向 `<应用数据根>/ms-playwright`，以便与便携布局一致。**更简单**：直接使用应用内的 **Download Login Component**（Chromium 会落在上述目录）。

---

### 4）先在 Settings 配置路径

按需配置以下目录：

- RaceBox 数据目录（`.csv`）
- AIM 数据目录（`.xrk/.xrz/.drk`）
- MoTeC 数据目录（`.ld`）
- GPX 数据目录（`.gpx`）
- 视频目录
- 导出目录

只填你实际使用的数据源即可。

首次使用补充：

- AIM 用户：点击 **Download DLL**（仅首次需要）
- RaceBox 云下载用户：点击 **Download Login Component**，完成后再 **Check Auth**

### 5）在 Data 页扫描节

应用启动后会自动扫描，也可手动点 **Scan**。

节状态说明：

- `✓ user`：已人工确认同步偏移
- `~ auto`：自动检测到偏移（建议人工确认）
- `≈ unset`：未设置偏移
- `no vid`：未匹配到视频，可手动绑定

### 6）设置视频同步偏移

同步偏移决定仪表与视频时间轴的对齐。

- **自动同步（推荐）**：在 Settings 打开「扫描后启用自动同步」  
  默认用**元数据时间线**（遥测节起始时间 vs 视频的创建时间 / 内嵌 SMPTE 时间码等）估算偏移并吸附到视频帧，不做画面运动分析。  
  可选在 Settings 打开「**启用运动精细对齐**」：再对画面运动与 G 力做互相关（固定车载机位更合适；头盔/防抖画面易失败）。
- **手动同步**：在 Data 页对准 Lap 起点后点击 **Mark**

> GPX 不含 G 值，通常需要手动同步。

### 7）在 Overlay 页编辑仪表

- 通过 **Open in Overlay →** 打开编辑器
- 点击 **Add Gauge** 增加元素
- 拖动移动，拖拽角点缩放
- 切换主题（Dark/Light/Colorful/Monochrome）
- 可保存为预设（Preset）

### 8）导出视频

在 Data/Overlay 中加入导出项后，进入 Export 页：

- 选择范围：当前圈 / 最快圈 / 全部圈 / 全 Session
- 选择编码器：自动检测 GPU（NVENC/AMF/QSV），无则回退 CPU（libx264）
- 点击 **Start Export**

---

## 分圈（Lap Splitting）

Data 页提供“**切圈**”入口，包含两种方式：

- **选择赛道（JSON 起终线）**：从赛道 JSON 中读取 `label_info.direction` 与 `line_lonlat` 批量切圈
- **手动画起终点线**：启动 GUI 工具，在 GPX/VBO 轨迹上画线并保存

---

## 赛道配置（Track JSON）

建议把真实赛道配置放在 `tracks/` 下，例如 `tracks/guangzhou.json`。

- 仓库默认忽略真实 `tracks/*.json`
- 仅提交模板 `tracks/*.template.json`
- 可从 `tracks/track.template.json` 复制创建

---

## 主要功能

### 数据与节管理

- 按数据源独立配置目录
- 启动自动扫描 + 本地缓存
- 按日期分组展示节
- 自动匹配视频并支持手动重绑
- 多段视频自动拼接渲染
- 自动同步（可选）与手动同步共存
- AIM 原始文件自动转 CSV（依赖 AIM DLL）

### 叠加编辑器

- 实时预览与拖拽编辑
- 元素吸附与对齐辅助
- 多圈切换预览
- 4 套主题
- 多种仪表样式（数值、条形、表盘、折线、Delta、地图、图片等）
- 支持 Bike 模式与参考圈对比
- 支持预设保存与切换

### 导出能力

- 多种导出范围
- GPU 编码加速（NVENC/AMF/QSV）+ CPU 兜底
- 可调 CRF、并发、前后 padding
- 实时进度与日志

---

## 支持的数据源

- **RaceBox**：`.csv`
- **AIM Mychron**：`.xrk/.xrz/.drk`（扫描时转 CSV）
- **MoTeC**：`.ld`
- **GPX**：`.gpx`（可计算速度，但无原生 G 值）
- **VBOX**：`.vbo`（支持读取 `lap` 字段）

---

## 常见问题

支持 **Windows**、**macOS** 与 **Linux**。

### 扫描不到节

- 检查 Settings 路径是否正确
- 检查文件扩展名是否匹配
- 手动点击 Scan 重扫
- AIM 需先安装 DLL

### 视频匹配失败

- 先检查设备时间是否准确
- 用 Data 页的“手动指定视频”绑定

### 自动同步失败

- 可能置信度不足，改手动同步
- GPX 通常不适合自动同步
- `✓ user` 的偏移不会被自动结果覆盖

### 导出失败

- 查看 Export 页日志
- 检查导出目录可写
- 尝试切换到 libx264

---

## 从源码运行

支持 Windows / macOS。

### 依赖

- Python 3.10-3.13
- **FFmpeg**：系统 `PATH` 中能调用 `ffmpeg` / `ffprobe` 即可；或在仓库根目录执行下面的可选步骤，把二进制下载到 `third_party/ffmpeg/`（与 `OpenLap.spec` / 运行时的查找逻辑一致，便于不装全局 FFmpeg）。

### 安装

**建议**在虚拟环境里安装（避免与系统/其他项目的包冲突）：

```bash
# 仓库根目录下
python -m venv .venv
```

激活虚拟环境：

- **Windows（PowerShell）**：`.\.venv\Scripts\Activate.ps1`
- **Windows（cmd）**：`.\.venv\Scripts\activate.bat`
- **macOS / Linux**：`source .venv/bin/activate`

然后安装本项目为**可编辑模式**（改代码后无需重装即可 `python main.py` 生效）：

```bash
python -m pip install -U pip
python -m pip install -e .
```

可选（Windows，本地 FFmpeg 放入仓库，便于开发与打包）：

```bash
python tools/fetch_ffmpeg.py
```

可选（RaceBox 云下载）：

```bash
python -m pip install -e ".[racebox-download]"
```

安装 Playwright 自带 Chromium（二选一）：**推荐**直接使用应用内的 **Download Login Component**（会写入 `<仓库根>/.openlap/ms-playwright/`，与本节「便携」布局一致）。若你坚持在**独立终端**里跑 `playwright install chromium`，须先自行设置 `PLAYWRIGHT_BROWSERS_PATH` 指向该目录（可先 `mkdir .openlap`），例如 Windows cmd：`set PLAYWRIGHT_BROWSERS_PATH=%CD%\.openlap\ms-playwright`，再执行 `playwright install chromium`。

### 启动

```bash
python main.py
```

配置文件与缓存路径见上文「便携与本地数据目录」（开发：`<仓库根>/.openlap/`；打包后与可执行文件同目录；可用环境变量 `OPENLAP_DATA_DIR` 覆盖）。

**macOS / Linux 说明**

- AIM 的 `.xrk` / `.xrz` / `.drk`：通过 `pip install -e .` 使用 **libxrk** 读取。MatLabXRK DLL 为 **Windows 专用二进制**，在 macOS / Linux 上不可用；上述平台仅支持 libxrk。
- macOS 上硬件编码优先 **VideoToolbox**（`h264_videotoolbox`）。NVENC / AMF / QSV 仅在 Windows / Linux 可用。


---

## Windows 打包

建议先把 FFmpeg 放进仓库（否则需保证目标机器 PATH 上已有 ffmpeg/ffprobe，或与 `OpenLap.spec` 同目录放置可执行文件）：

```bash
python tools/fetch_ffmpeg.py
pip install pyinstaller
python tools/build_windows_portable.py
```

`build_windows_portable.py` 会先执行 `pyinstaller OpenLap.spec`，再把 `third_party/ffmpeg/...` 里的 `ffmpeg.exe` / `ffprobe.exe` 复制到 **`dist/OpenLap/Library/ffmpeg/`**（与 `OpenLap.exe` 同级，不在 `_internal` 里）。若只运行 `pyinstaller` 而未执行复制步骤，打包版会找不到自带 FFmpeg。

输出目录：`dist/OpenLap/`

### GitHub Actions：FFmpeg 更新后自动发版

仓库已配置 [`.github/workflows/release-on-ffmpeg-update.yml`](.github/workflows/release-on-ffmpeg-update.yml)（默认每天 UTC 06:00 跑一次，可在文件里改 `cron`）：

1. 查询 [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) 的 `releases/latest`，用 **GitHub 分配的稳定 id**（`release_id|asset_id`）与 [`.github/ffmpeg-release-pin.txt`](.github/ffmpeg-release-pin.txt) 对比；若未变则跳过。
2. 若有新包：将 `_version.py` 的 **patch** 自增（`x.y.z` → `x.y.(z+1)`），拉取 FFmpeg、执行 `build_windows_portable.py`、打 zip、提交版本与 pin、推送，并创建带附件的 **GitHub Release**；说明正文为：  
   `ffmpeg更新到 <zip 资源名>（<ffmpeg -version 首行>）`。
3. 在 Actions 里可 **手动 Run workflow**，勾选 **force** 可在 pin 未变时仍强制打包发版（同样会 patch+1，请慎用）。

**注意**：默认分支若开启「必须通过 PR / 禁止直接 push」，需为 `github-actions[bot]` 放宽规则或使用带 `repo` 权限的 PAT 写入分支；打包还依赖 `OpenLap.spec` 里声明的 DLL 等文件在仓库中已存在（与本地打包前提一致）。

---

## 许可证

GNU General Public License v3，详见 [LICENSE](LICENSE)。
