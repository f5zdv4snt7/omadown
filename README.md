# omadown

**Downie 4 的 Omarchy / Linux 等价实现。** 一个队列式多媒体下载器：站点抽取器矩阵 + 自研 HLS 引擎 + 网页兜底嗅探 + ffmpeg 后期处理，面向 Hyprland 的键盘驱动工作流。

逆向分析过程与逐项证据见 [`ANALYSIS.md`](ANALYSIS.md)。

---

## 为什么是「队列 + 守护进程」而不是一个 GUI

Downie 的核心不是界面，而是三件事：**多引擎抽取、可恢复的队列、无处不在的投喂入口**。omadown 把这三件事按 Linux 的方式重做：

```
   你复制一个链接            Super+D                Waybar
        │                      │                      │
   ┌────▼──────┐        ┌──────▼──────┐        ┌──────▼──────┐
   │ wl-paste  │        │  omadown    │        │  status.py  │
   │  --watch  │        │    tui      │        │  (JSON)     │
   └────┬──────┘        └──────┬──────┘        └──────▲──────┘
        │                      │                      │
        └──────────┬───────────┘                      │
                   ▼                                  │
        ┌────────────────────────┐   unix socket   ┌───┴────┐
        │   omadown daemon       │◄───────────────►│  CLI   │
        │  队列 / 调度 / 通知     │  control.sock   └────────┘
        └───────────┬────────────┘
                    ▼
   ┌────────┬────────────┬────────────┬────────────┐
   │ ytdlp  │ hls(原生)   │ uge(嗅探)  │ direct     │   4 个后端
   └────┬───┴─────┬──────┴─────┬──────┴─────┬──────┘
        └─────────┴────────────┴────────────┘
                    ▼
        ffmpeg 后期处理 → 命名方案 → 输出目录 → 通知
```

---

## 安装（Omarchy）

```bash
git clone <this-repo> ~/.local/share/omadown-src
cd ~/.local/share/omadown-src/omadown
./install.sh
```

脚本会：

1. 用 `omarchy-pkg-install` 安装 `yt-dlp`、`ffmpeg`、`wl-clipboard`、`libnotify`；
2. 把 Python 包复制到 `~/.local/share/omadown/`，在 `~/.local/bin/` 放置 `omadown` 启动器（**不碰系统 pip**，规避 Arch 的 externally-managed 限制）；
3. 安装 Omarchy 集成：Waybar 模块、Hyprland 键位（Lua 模块）、`omarchy-menu` 入口、`.desktop`、systemd user 服务；
4. 首次写入 `~/.config/omadown/config.toml`。

Hyprland 键位与窗口规则装在 `~/.config/hypr/omadown.lua`，并由安装脚本在
`~/.config/hypr/hyprland.lua` 末尾追加一行 `require("hypr.omadown")`。

**不要**把绑定写成 `.conf` 再丢进 `~/.config/omarchy/extensions/`：新版 Omarchy 的
Hyprland 配置已改为 Lua（`hyprland.lua` + `require("hypr.*")`），那个目录只放菜单
`.jsonc` 和 `.sh` 钩子，没有任何东西会去 source `*.conf`——文件放进去只会被静默忽略，
表现为「Super+D 按了没反应」。旧版（仍是 `hyprland.conf` + `source`）的系统安装脚本也会
自动改走 `.conf` 分支，两者都测过。

```bash
./install.sh --no-deps      # 依赖自己装
./install.sh --no-integrations
./install.sh --status       # 检测是否已安装并报告安装详情（已安装退出码 0）
./install.sh --uninstall    # 卸载（保留配置与下载记录）
```

**重复执行 `./install.sh` 即为更新**：脚本会检测已安装的版本并与源码对比，更新前停止守护进程、完成后自动恢复，按安装清单（`~/.local/share/omadown/install-manifest.txt`）清理新版本不再提供的旧文件；`config.toml`、下载队列与历史记录（`omadown.db`）原样保留。

Omarchy 集成装到 `~/.config/hypr/omadown.lua`（键位与窗口规则）与
`~/.config/omarchy/extensions/omadown-menu.sh`（菜单钩子），**不会覆盖 Omarchy 自带的默认文件**。

---

## 快速上手

```bash
omadown doctor                      # 环境检查
omadown add 'https://…'             # 入队
omadown add 'https://…' -f          # 入队并前台跑完
omadown add 'https://…' -q 1080p    # 画质上限 1080p
omadown add 'https://…' -x          # 只要音频
omadown add 'https://…' --subs      # 下字幕并封装
omadown add 'https://…' --playlist --playlist-items 1-20
omadown probe 'https://…'           # 只看格式表，不下载
omadown tui                         # 队列界面（Super+D）
omadown daemon                      # 常驻：队列 + 剪贴板 + 控制套接字
```

### 队列界面（`Super+D`）

| 键 | 动作 |
|---|---|
| `j` / `k` | 上下移动 |
| `a` | 添加链接（可一次贴多个） |
| `空格` | 暂停 / 继续（对进行中的任务=取消） |
| `x` | 取消 |
| `r` | 重试（无选中项时重试全部失败项） |
| `d` | 删除记录 |
| `c` | 清理已完成 |
| `o` / `O` | 打开文件 / 打开下载目录 |
| `g` / `G` | 首 / 尾 |
| `q` | 退出 |

---

## 四个下载后端

按优先级自动选择；也可用 `--backend` 强制。

| 优先级 | 名称 | 对应 Downie 的什么 | 说明 |
|---|---|---|---|
| 10 | `ytdlp` | 1611 个站点抽取器 | 驱动 `yt-dlp` CLI，支持播放列表、Cookie、代理、限速、并发分片、签名维护 |
| 20 | `hls` | "Use native M3U8 (HLS) download engine" | 自研 M3U8 引擎：master/media 解析、`EXT-X-KEY` AES-128 解密、`EXT-X-BYTERANGE`、`EXT-X-MAP`、音轨组、直播轮询、并发分片 |
| 30 | `uge` | User-Guided Extraction | 网页兜底嗅探：`<video>`/`<source>`/OpenGraph/JSON-LD/内联脚本（含 `\/` 转义还原）/iframe 跟一层；加 `--render` 用 Playwright 抓真实网络请求 |
| 90 | `direct` | 直链下载 | HTTP Range 断点续传，任何链接的最后兜底 |

强制原生引擎（Vimeo 这类对 HLS 更稳的站点）：

```bash
omadown add 'https://…/master.m3u8' --backend hls --max-resolution 1080
```

---

## 从 Downie 迁移过来的行为细节

这些是分析里逐条抠出来、并被测试覆盖的语义，不是"看起来像"：

| 行为 | 说明 |
|---|---|
| 画质上限的降级规则 | 限制 480p 但站点只有更高画质时，**选最小的那个**，而不是失败或给最高画质 |
| Prefer MP4 ≠ Force MP4 | 前者在同分辨率下优先 MP4（可用 `-S ext:mp4:m4a`）；后者忽略更高分辨率的其它容器 |
| 文件名方案必须含 `{title}` | 缺 `{title}` 直接报错，和 Downie 一致 |
| 文件名逐段清洗 | 非法字符替换；标题里的 `/` **不会**变成目录；不能生成隐藏文件；不能拼出 `..`；按 UTF-8 字节截断 |
| 字幕单/多模式 | `single` 只取第一个匹配语言，`multiple` 全取 |
| 失败保留原文件 | `postprocessing.keep_original`，转换失败还能手动重来 |
| 无人值守模式 | `general.automated`，关闭所有交互提示 |
| 队列自愈 | 启动时把 `downloading/postprocessing` 的中断项复位（等价 Downie 的 Reset Queue） |

---

## 配置

`~/.config/omadown/config.toml`（`omadown config --edit` 打开）：

```toml
[general]
download_dir = "~/Videos/omadown"
max_concurrent = 3
file_name_format = "{title}.{ext}"        # 必须含 {title}
group_playlists = false
clipboard_watch = false
notify = true
automated = false
remove_completed = false
max_retries = 2

[quality]
max_resolution = 0                        # 0 = 不限制
prefer_mp4 = true
force_mp4 = false
audio_only = false
preferred_audio_format = "m4a"
enforce_mp3 = false

[subtitles]
enabled = false
languages = ["zh-Hans", "en"]
mode = "single"                           # single | multiple
include_generated = true
embed = true

[network]
proxy = ""
cookies_from_browser = ""                 # firefox / chrome / brave / edge
connections = 8
chunked = true
rate_limit = ""                           # 如 "5M"

[postprocessing]
enabled = true
hls_mode = "remux"                        # remux | reencode | none
transcode = ""                            # "" | h264 | h265
hardware_accel = "auto"                   # auto | vaapi | nvenc | qsv | none
keep_original = false
write_metadata = true
custom_script = ""
save_metadata_json = false
```

命令行覆盖：`omadown config --set quality.max_resolution=1080`。

### 自定义脚本钩子

对应 Downie 的 `Edit Custom Script...`。脚本通过环境变量拿到结果：

```bash
postprocessing.custom_script = "rsync -a \"$OMADOWN_FILE\" nas:/media/ && curl -s \"$OMADOWN_URL\" >/dev/null"
```

可用变量：`OMADOWN_FILE`、`OMADOWN_TITLE`、`OMADOWN_URL`、`OMADOWN_EXTRACTOR`、`OMADOWN_PLAYLIST`、`OMADOWN_ID`。

---

## 脚本化 / 自动化

常驻守护进程监听 `$XDG_RUNTIME_DIR/omadown/control.sock`，换行分隔 JSON：

```bash
omadown status --waybar            # Waybar 模块
omadown status --json              # 完整快照
omadown list --history --json      # 历史
omadown cancel / retry / clear     # 队列操作
```

```python
# 或者直接说协议
import json, socket, os
sock = socket.socket(socket.AF_UNIX)
sock.connect(f"{os.environ['XDG_RUNTIME_DIR']}/omadown/control.sock")
sock.sendall(json.dumps({"cmd": "add", "urls": ["https://…"]}).encode() + b"\n")
print(sock.recv(65536).decode())
```

---

## 开发

```bash
python -m unittest discover -s tests -t .    # 75 项测试
python tests/smoke_pty.py                    # TUI / 守护进程真终端冒烟（需要 pty）
```

测试包含真实的 ffmpeg HLS 阶梯生成 + 本地 HTTP 服务 + 端到端下载 + `ffprobe` 校验可播放性、AES-128 的 NIST SP 800-38A 已知答案向量、Unix socket 控制通道、以及 curses 界面在宽字符下不越界。`smoke_pty.py` 用 `forkpty` 起真终端跑 TUI 和守护进程，不在 `unittest` 自动发现范围内。

零第三方运行时依赖：只用标准库（`tomllib`、`sqlite3`、`curses`、`urllib`）。`cryptography`、`playwright` 都是可选加速项。

```
omadown/
  cli.py        命令行
  tui.py        curses 队列界面
  engine.py     队列 / 调度 / 重试 / 落盘
  db.py         SQLite 队列与历史
  ipc.py        unix socket 控制通道
  watch.py      剪贴板守护
  status.py     Waybar / 文本状态
  config.py     配置
  naming.py     文件名方案
  postproc.py   ffmpeg 流水线
  crypto.py     AES-CBC（纯 Python + 可选快路径）
  net.py        HTTP（代理 / Cookie / Range）
  backends/     ytdlp · hls · sniff · direct
  integrations/omarchy/    Waybar / Hyprland / 菜单 / systemd / desktop
```

---

## 边界

- 不含任何 DRM 绕过（Widevine / PlayReady / FairPlay 一律不处理）。
- 站点专属的签名 JS 不自研复刻，交给 yt-dlp 生态维护。
- 请遵守各站点的服务条款与当地法律使用。
