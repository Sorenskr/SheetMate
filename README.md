# 暗号表助手（SheetMate）v2.9.1

[![最新版本](https://img.shields.io/badge/Release-v2.9.1-0A84FF?style=flat-square&logo=github)](https://github.com/Sorenskr/SheetMate/releases/latest)
[![运行平台](https://img.shields.io/badge/Platform-Windows%2010%20%2F%2011%20x64-34C759?style=flat-square&logo=windows)](https://github.com/Sorenskr/SheetMate/releases/latest)
[![云端远程入口](https://img.shields.io/badge/Cloud%20Web-点击打开远程控制台-5856D6?style=flat-square&logo=googlechrome)](https://sorenskr.github.io/SheetMate/)

> 办公数据自动化与多端协同桌面助手：自动登录后台抓数、一键清洗写入 WPS 在线汇总表、高清智能截图、无坐标钉钉静音发群，支持手机与异地网页端到端加密聊天遥控。

---

## 📥 立即下载与在线访问

不管您是第一次拿到这个工具的新同事，还是需要在外出差用手机查看电脑跑表进度，都可以直接通过下方入口下载或打开：

| 用途 | 直达链接 | 特点说明 |
| :--- | :--- | :--- |
| **Windows 纯净单文件客户端**（推荐直接下载） | [**下载 SheetMate.exe**](https://github.com/Sorenskr/SheetMate/releases/latest/download/SheetMate.exe) | **就单个 EXE 程序**，下载后放在任意位置双击直接运行，不在软件旁生成杂乱文件夹，关闭窗口直接退出结束，支持一键检查更新 |
| **云端异地远程控制台**（手机 / 外地网页） | [**打开 https://sorenskr.github.io/SheetMate/**](https://sorenskr.github.io/SheetMate/) | 手机浏览器或外地电脑打开，填好电脑上的**唯一名字**和**远程密码**即可通过双节点加密实时通道秒连电脑聊天、查数据、遥控跑表（网页内也提供客户端下载） |
| **全部历史版本与发行说明** | [**查看 GitHub Releases**](https://github.com/Sorenskr/SheetMate/releases) | 查看完整迭代记录、版本更新详情与文件 SHA-256 校验信息 |

---

## ✨ v2.9.1 核心功能一览

- **智能语音指令精准识人与默认自动确认**
  - 在钉钉发消息指令中准确理解 **「我 / 发给我」**（自动匹配本机钉钉实名会话或文件小助手，绝不把系统随机趣味昵称拿去钉钉乱搜），识别 **「发群 / 发群里」** 自动对应已启用的默认群聊，自动剔除聊天记录文本干扰并清洗错误记忆。
  - 默认自动按 Enter 发送并跳过非登录类的繁琐二次确认弹窗，全流程畅通直发。

- **全自动抓数写表 & 自定义格位**
  - 自动登录后台拉取三份源数据，自动规范格式并精准写入 WPS 金山在线汇总表，同步自动清理多余旧标签页。
  - 支持自由配置单元格规则（如日期按天加减、指定团队或筛选词），并具备软件自检、数据自检与每轮异常归档功能。
- **4K / 多缩放自适应钉钉静音投递**
  - 基于 UIA 控件定位与多尺度图像模板双重识别，适配不同屏幕分辨率与系统缩放比，不依赖写死坐标。
  - 支持多群组顺序连发、**静音发送**（群成员收得到消息但不响铃打扰）、发送前群名二次核验与防键鼠误触护栏。
- **大白话排期 & 单点独立联动**
  - 支持白天 / 夜间双时段、工作日 / 每天 / 自定义星期，内置桌面灵动岛悬浮进度胶囊与值守防电脑休眠。
  - 点击排期列表中的任意时间点，可用大白话直接单独调整：**「按默认 / 发钉钉 / 不发钉钉」** 与 **「今天 / 昨天 / 前天」**；选中「不发钉钉」后弹窗与列表零延迟同步隐藏多余配文与静音项。
- **云端异地远程连接 & 多人防撞排队**
  - 每一位使用成员设置唯一名字并挑选可爱的萌宠头像，支持通过云端协作空间在多人共用同一张线上表格时**自动互锁排队防撞**。
  - 无论在家还是出差，通过 [云端远程网址](https://sorenskr.github.io/SheetMate/) 输入本机唯一名字与自设远程密码即可端到端加密连回电脑（PBKDF2-SHA256 密钥派生 + 认证加密 + 防重放防篡改），电脑与手机聊天记录实时双向同步。
- **内置智能伙伴与数据管家**
  - 每轮跑完表自动对照上一轮播报数据增量亮点；支持用自然语言让智能助手帮忙改排期、复盘异常或做数据自检；支持「换一句」清爽问候语刷新。
- **Apple iOS 18 昼夜动态光效界面**
  - 随清晨、白昼、黄昏、夜晚自动变换磨砂玻璃动态天光背景，卡片化分组设置修改即生效。

---

## 🚀 三步快速上手

1. **下载运行**：点击上方下载 [**SheetMate.exe**](https://github.com/Sorenskr/SheetMate/releases/latest/download/SheetMate.exe)，在 Windows 电脑上双击直接运行（同目录不生成杂乱文件夹，关窗直接退出）。首次运行若未安装浏览器组件会自动静默补齐。
2. **填好配置与起名字**：在右上角设置一个**全队不重名的专属名字**并挑选萌宠头像；填好 WPS 汇总表链接、数据账号与目标钉钉群名称（若需手机或外地电脑遥控，在【远程网站】中设置好您自己的远程密码即可）。
3. **一键启动或定时值守**：点击控制中心底部的 **「立即全流程」** 即可自动完成抓数、写表、截图并投递钉钉；首次使用如 WPS 需要登录，手机扫码确认一次后后续即可全自动定时执行。

---

## 🛠 开发者本地运行与打包

如果您希望查看或二次修改源码运行：

```bash
# 1. 克隆仓库
git clone https://github.com/Sorenskr/SheetMate.git
cd SheetMate

# 2. 安装 Python 依赖与 Playwright Chromium 内核
pip install -r requirements.txt
python -m playwright install chromium

# 3. 直接启动图形界面控制中心
python auto_report.py

# 4. 打包本地极速目录版（产物位于 dist/SheetMate_bundle）
pyinstaller SheetMate.spec --clean --noconfirm
```

---

## 📂 仓库主要文件说明

| 文件 / 目录 | 功能说明 |
| :--- | :--- |
| `auto_report.py` | 核心自动化引擎（后台抓取、表格读写、选区截图、钉钉无坐标投递、AI 对话与定时调度原语） |
| `ios_ui.py` | iOS 18 昼夜动态光效桌面控制中心前端界面与本地安全 API 服务 |
| `mobile_bridge.py` | 云端异地端到端加密中继通信、远程网页控制台模板生成与静态站点导出工具 |
| `team_hub.py` | 成员唯一昵称档案、萌宠头像体系、云端多人防撞锁 / 排队与凭据脱敏管理 |
| `neu_ui.py` | 高分屏 Win32 桌面灵动岛悬浮进度胶囊组件 |
| `docs/` | GitHub Pages 托管的云端异地远程控制台在线站点 |
| `templates/` | 钉钉界面控件视觉识别模板图与客户端应用图标资源 |
| `SheetMate.spec` | PyInstaller 桌面客户端打包规格文件 |
| `version.json` | 客户端在线检查更新清单 |
