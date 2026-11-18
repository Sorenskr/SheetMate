# 📊 暗号表助手（SheetMate）

> 自动拉取数据日报 → 写入 WPS 金山在线表格 → 高清框选汇总表复制为图片 → 精准推送至钉钉群聊。

---

## 📥 下载与使用（Windows 免安装版）

### 👉 **[点击直接下载最新版 SheetMate.exe (v2.0.0)](https://github.com/Sorenskr/SheetMate/releases/latest/download/SheetMate.exe)**

> 也可以前往 **[Releases 发布页](https://github.com/Sorenskr/SheetMate/releases)** 查看所有历史版本。

### 首次使用步骤
1. 下载 `SheetMate.exe` 到任意文件夹（首次运行不会在桌面铺开杂乱缓存，浏览器数据自动存放在 `%LOCALAPPDATA%\SheetMate\browser_data`）。
2. 双击 `SheetMate.exe` 打开 **Apple iOS 18 风格控制中心**（首次运行会在后台自动补齐 Chromium 组件）。
3. 在控制中心设置好「钉钉目标群聊 / 会话」与运行计划，点击 **「立即跑一次」**。
4. 首次运行若 WPS 弹出扫码登录页面，请使用手机扫码登录一次，后续即全程全自动执行。

---

## ✨ v2.0.0 核心特性与优化

- **全新 Apple iOS 18 风格控制中心**
  - 粘性毛玻璃导航栏、支持浅色 / 深色模式一键切换。
  - 顶部 **Dynamic Island 灵动岛状态卡片** + 桌面置顶灵动岛悬浮进度胶囊，实时查看步骤、百分比和倒计时，运行中可随时点击「停止」。
  - 标准 iOS 分段滑块、苹果绿弹簧开关、时间选择器胶囊与手风琴折叠设置组，修改参数**自动保存并实时热生效**。
- **WPS 汇总表复制为图片高可靠性修复**
  - 检测末行前原生清空系统剪贴板，避免读到上一轮 TSV 残留数据导致选区行数错乱。
  - 增加 `D8` 姓名列兜底探测，并在画布绿框内部动态计算右键点击坐标（不再写死固定屏幕坐标）。
  - 复制后自动校验剪贴板位图尺寸，杜绝误复制空单元格或单格。
- **钉钉精准搜群、静音发送与防误贴四重护栏**
  - 自动剥离群名中的 Emoji 表情，优先使用纯文字核心词段进行精准搜索与多词匹配，大幅提升含 Emoji 长群名命中率。
  - 搜索期间剪贴板不放图片；通过 Win32 检测全局搜索弹窗（`ToolSaveBits`）彻底消失、自动定位底部聊天输入区并在粘贴图片与文字前双重核验当前系统焦点控件，**100% 防止将图片误贴入搜索框**。
  - **发送前自动清空发送框残留草稿**，防止把未删完的内容误发出去；支持自动点击发送右侧菜单勾选 **「设为静音（静音发送）」**，且默认贴好后先暂不发出，方便确认后手动发送。

---

## 🛠 源码运行与本地打包

```bash
# 1. 克隆仓库
git clone https://github.com/Sorenskr/SheetMate.git
cd SheetMate

# 2. 安装依赖与浏览器内核
pip install -r requirements.txt
python -m playwright install chromium

# 3. 直接运行
python auto_report.py

# 4. 打包成单文件 SheetMate.exe
pyinstaller SheetMate.spec --noconfirm
```

---

## 📂 项目结构

| 文件 | 说明 |
| :--- | :--- |
| `auto_report.py` | 核心自动化引擎（后台下载、格式转换、WPS 写入与截图、钉钉发送、单实例控制） |
| `ios_ui.py` | iOS 18 风格控制中心前端界面、本地接口服务及动态定时值守调度器 |
| `neu_ui.py` | Windows 高分屏适配与桌面灵动岛悬浮进度胶囊组件 |
| `sheetmate_ui.py` | 界面入口兼容转发层 |
| `SheetMate.spec` | PyInstaller 单文件打包配置文件 |
| `一键运行.bat` | Windows 一键安装依赖并无黑框启动脚本 |
| `version.json` | 在线自动检测新版本清单 |
