# 实时多语言字幕

[English](README.md) | [简体中文](README.zh-CN.md)

Windows 本地实时字幕工具。捕获默认扬声器、耳机或 HDMI 的系统输出，使用 Whisper 自动识别多语言语音，通过本地 Qwen 翻译为中文或英文。

> 多语言指源语言识别。目标语言目前支持中文、英文和无翻译。当前为实验版本，识别准确率与延迟受音频、驱动及硬件影响。

## 功能

- WASAPI 系统音频采集，跟随默认输出设备切换和重连。
- 源语言支持自适应、中文、English；目标语言支持中文、English、无翻译。
- 实时原文与预翻译持续更新，整句完成后确认，避免音频窗口滚动切成半句。
- 双语或仅译文，运行中切换语言与显示设置。
- 无边框置顶字幕，拖动移动、左右边缘调宽度、锁定后鼠标穿透。
- 字号、颜色、文字与背景透明度独立设置，背景可全透明。
- 历史字幕逐句换行；最新指定条数受保护，其余按进入历史区的时间移除。
- 当前字幕行和按钮固定，窗口随内容调整高度；设置自动保存。

## 环境要求

- Windows 10/11 x64，NVIDIA 显卡和近期驱动。
- 推荐 12GB 以上显存；开发验证机器为 RTX 5070 Ti 16GB。
- 建议至少 20GB 可用磁盘空间，用于依赖、模型和下载缓存。
- 首次初始化需要访问 PyPI、PyTorch、Hugging Face 和 GitHub；之后可离线运行。
- 源码安装使用 Python 3.11 以上，推荐 Python 3.14 x64，必须包含 Tkinter。

## Windows 安装包

从本仓库 Releases 下载 Windows x64 安装器，安装到有写入权限的目录。安装包包含本地 Python 和程序，但不包含大型模型及推理依赖。

首次运行快捷方式打开初始化窗口，安装依赖并下载默认模型、llama.cpp CUDA 运行时。完成后启动字幕，后续启动直接进入字幕窗口。

网络错误时再次运行快捷方式重试。初始化日志在 `logs/bootstrap.log`，运行日志在 `logs/runtime.log`。安装不需要管理员权限；卸载清理程序文件，模型、日志和用户设置保留。

## 源码运行

```powershell
git clone https://github.com/grzhx/realtime-multilingual-subtitles.git
cd realtime-multilingual-subtitles
powershell -NoProfile -ExecutionPolicy Bypass -File .\initialize.ps1
.\run.ps1
```

`initialize.ps1` 一次完成虚拟环境、依赖、默认模型和 CUDA 运行时安装。所有下载写入项目目录。

```powershell
.\run.ps1 -Config config.no_translation.toml
```

上述配置只运行识别，不读取默认界面记忆。运行中启用翻译会加载本地模型。

`config.fallback_0_6b.toml` 是可选 Transformers 回退配置，需要另行下载 Qwen3-0.6B；默认初始化只下载 large-v3-turbo 和 4B Q4 GGUF。

## 模型和运行时

| 用途 | 默认组件 | 来源 |
|---|---|---|
| ASR | faster-whisper large-v3-turbo | [Hugging Face](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo) |
| 翻译 | Qwen3-4B-Instruct-2507 Q4_K_M | [Hugging Face](https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF) |
| 推理 | llama.cpp b11461 CUDA 12.4 | [GitHub](https://github.com/ggml-org/llama.cpp) |

下载固定模型 revision，校验主要权重和运行包 SHA-256。ASR 约 1.5GiB，GGUF 约 2.3GiB。服务只监听本地回环地址的随机端口，使用临时 API key，关闭程序时退出。

## 使用与配置

未锁定时拖动主体移动，左右边缘调宽。鼠标进入字幕区域显示设置、关闭按钮；锁定按钮位于当前句右侧。锁定后只保留解锁按钮可点击，其余区域鼠标穿透。

语言和双语开关立即生效；字号、宽度、颜色、透明度和历史参数点击“应用”生效。默认设置保存至 `cache/subtitle_settings.json`。

历史条数是保护数量，不是硬上限。例如保留 2 条、存在 5 秒：最新 2 条不移除，其他句子进入历史区满 5 秒后移除。当前开放句不计入历史。

采集跟随 Windows 默认播放设备，播放器单独指定其他输出时请统一设备。

高级参数在 `config.toml`：

| 参数 | 作用 |
|---|---|
| `asr.compute_type` | 默认 `float16`，显存不足可测试 `int8_float16` |
| `stream.update_interval_ms` | 原文 partial 更新目标间隔 |
| `stream.commit_interval_ms` | 稳定词时间戳识别目标间隔 |
| `stream.max_segment_seconds` | 音频滚动窗口，不等于句子长度 |
| `stream.sentence_end_ms` | 明显停顿结束句子的时长 |
| `stream.preview_translation_seconds` | 允许预翻译的稳定音频时长 |
| `translation.max_new_tokens` | 输出基础预算，长字幕按长度增加 |
| `ui.history_count` | 保护的最新历史条数 |
| `ui.history_display_seconds` | 历史字幕存在时间 |

## 已知限制

- 音乐、噪声、多人讲话、口音可能错识别。过滤减少音乐幻觉，不能完全排除高置信度错误。
- 预译文随整句内容修改；句末标点和停顿判断可能有误。
- 受保护音频、独占模式、部分驱动可能不支持 loopback。
- 屏幕空间不足时字幕缩小，必要时移动固定行以避免截断。
- 当前重点支持 NVIDIA CUDA，未验证跨平台或 CPU 低延迟体验。

## 开发与打包

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\packaging\build.ps1 -PythonHome C:\Python314 -IsccPath path\to\ISCC.exe
```

测试保留在仓库。Windows UI 测试需要交互式桌面，模拟测试不依赖 GPU。

构建生成源码 ZIP、便携初始化包和 Inno Setup 安装器；输出均位于项目内 `build/`、`dist/`。首次初始化包包含 Python，不包含依赖和模型，所以不是离线完整包。

## 许可证

源码采用 [MIT](LICENSE)。模型、Python、llama.cpp、CUDA 和其他依赖保留各自许可证，项目许可证不替代第三方条款。详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

