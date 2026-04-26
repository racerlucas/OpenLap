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

### 5）在 Data 页扫描会话

应用启动后会自动扫描，也可手动点 **Scan**。

会话状态说明：

- `✓ user`：已人工确认同步偏移
- `~ auto`：自动检测到偏移（建议人工确认）
- `≈ unset`：未设置偏移
- `no vid`：未匹配到视频，可手动绑定

### 6）设置视频同步偏移

同步偏移决定仪表与视频时间轴的对齐。

- **自动同步（推荐）**：在 Settings 打开 Auto Sync  
  应用会根据视频运动信号与 G 值做相关匹配自动求偏移。
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

### 数据与会话管理

- 按数据源独立配置目录
- 启动自动扫描 + 本地缓存
- 按日期分组展示会话
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

### 扫描不到会话

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

- Python 3.10+
- FFmpeg 在 PATH 中可用

### 安装

```bash
pip install -e .
```

可选（RaceBox 云下载）：

```bash
pip install -e ".[racebox-download]"
playwright install chromium
```

### 启动

```bash
python main.py
```

配置文件默认位于：`~/.openlap/config.json`

---

## Windows 打包

```bash
pip install pyinstaller
pyinstaller OpenLap.spec --clean -y
```

输出目录：`dist/OpenLap/`

---

## 许可证

GNU General Public License v3，详见 [LICENSE](LICENSE)。
