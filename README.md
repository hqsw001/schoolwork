# 剪贴板Pro · 单机剪贴板管理器

Windows 桌面端剪贴板管理器。**纯单机、不联网、零云端依赖** ——
把系统剪贴板从"一个临时缓冲区"变成"一个可检索、可组织的本地信息库"。

- 实现语言：**Python 3.12 + tkinter**（标准库 + Pillow + pywin32）
- 参照开源项目 [TieZ Clipboard](https://github.com/jimuzhe/tiez-clipboard)（Tauri + Rust）
  的分层设计重写，并按要求砍掉全部联网能力
- 详细设计与分阶段记录见 [`文档/`](文档/)

---

## 下载即用（不需要装 Python）

**⬇ [下载 pastePro-v0.01.zip](https://github.com/hqsw001/schoolwork/releases/download/1st/pastePro-v0.01.zip)**

1. 解压得到 `剪贴板Pro` 文件夹
2. 双击 `剪贴板Pro.exe` —— 没有主窗口，程序直接驻留系统托盘
3. 在任意软件里按 `Ctrl+Shift+V` 唤出面板，双击卡片即粘贴回去

> ⚠️ **必须整个文件夹一起用**：只拷 `剪贴板Pro.exe` 是跑不起来的，
> 旁边的 `_internal` 装着全部运行时依赖。
> 数据存放在 exe 同级的 `.data` 里，整个文件夹拷走就完成了迁移。

## 从源码运行

```bat
python -m pip install pillow pywin32
python -m app.main --show
```

也可以用 `启动剪贴板Pro.bat` 双击启动（脱离控制台、单实例守卫）。

## 打包成 exe

```bat
python -m pip install pyinstaller
python build_exe.py
```

产物在 `dist\剪贴板Pro\`。脚本会在构建完成后自动启动一次做真机验证，
验证不过就返回非零退出码 —— 不会给出一个"看起来打好了、双击起不来"的包。

## 跑测试

```bat
python -m tests.static_check       :: 静态自检（改完代码先跑这个）
python run_tests.py --all          :: 全量回归：27 个套件 / 1144 项断言
python run_tests.py --safe         :: 跳过会动系统剪贴板与窗口的套件
```

> 跑全量回归前请先确认没有实例在运行：本程序常驻托盘，
> 遗留实例会占着全局快捷键与单实例互斥体。

## 目录结构

| 路径 | 内容 |
|---|---|
| `app/` | 应用源码：`domain` / `infrastructure` / `services` / `app`(内核) / `ui` 五层 |
| `tests/` | 27 个测试套件（含真机剪贴板、真机粘贴、打包回归） |
| `sql/` | 数据库建表脚本 |
| `文档/` | 设计说明、测试报告、运行说明、重构差距清单、交接说明 |
| `build_exe.py` | 一键打包（PyInstaller 文件夹版） |
| `剪贴板Pro/` | 课程交付材料：需求分析报告、用例图、开题汇报（docx / pptx / drawio） |

## 说明

- **不联网**：代码中没有任何发起网络请求的路径。云同步、MQTT、局域网文件传输、
  主题商店、自动更新、公告全部不做。
- **隐私**：敏感内容用 Windows DPAPI 加密落盘；运行数据目录 `.data/`
  （剪贴板历史、附件、日志）已被 `.gitignore` 排除，**任何时候都不会提交**。
- **已知边界**：Emoji 在 tkinter 里渲染为单色轮廓、列表是分批渲染而非虚拟滚动、
  换 Windows 用户后旧加密记录解不开、未做代码签名等，
  都在 `文档/设计说明.md` 与 [`文档/README-详细版.md`](文档/README-详细版.md) 中如实列出。
