# Downie 4 功能逆向分析报告（REA）

> 对象：`/Applications/Downie 4.app`
> 版本：4.12.14（CFBundleVersion 5249，构建 2026-08-29）
> 厂商：Charlie Monroe Software（Copyright ©2013–2026）
> 方法：静态取证（bundle 结构 / plist / 本地化词条 / Mach-O 字符串 / 资源脚本），**不运行、不调试、不修改原始二进制**
> 目标产物：`omadown` —— 在 Omarchy（Arch + Hyprland）上提供同等功能的开源实现

---

## 0. REA 方法论

这次分析遵循 REA（Reverse Engineering Analysis）的取证链要求：**每一条功能结论都必须能回溯到一个可复核的证据源**，并标注证据强度。

| 级别 | 证据源 | 可信度 | 说明 |
|---|---|---|---|
| A | `Info.plist` / 可执行文件清单 / `manifest.json` | 极高 | 系统级声明，无法伪造 |
| B | `.nib` 文件名、`Localizable.strings` 词条 | 高 | 直接反映 UI 与交互文案 |
| C | 二进制字符串 / 类名 / 选择器 / 内嵌脚本 | 高 | 反映实现细节，但可能与运行时行为有偏差 |
| D | 推断 | 中 | 由 A–C 组合推导，报告中显式标注 |

本报告所有结论均标 `[A]`–`[C]`；仅第 5 节的重建方案包含 `[D]` 推断，且不用于声称 Downie 的内部实现。

---

## 1. 应用形态与集成面

| 结论 | 证据 | 级别 |
|---|---|---|
| Swift/AppKit 桌面应用，非 Electron | `Frameworks/` 含 `XUCore.framework`、`DownieCore.framework`、`libswift_Concurrency.dylib`；`NSPrincipalClass = XUCoreUI.XUApplication` | A |
| 注册了 5 个 URL scheme | `CFBundleURLTypes`：`downie`、`downie-support`、`downie-help`、`downie-license`，并把 `http`/`https` 声明为 `com.charliemonroe.downie.direct_url` | A |
| 断点续传以自定义文档类型落盘 | `CFBundleDocumentTypes`/`UTExportedTypeDeclarations`：`.downiepart`（`com.charliemonroe.downie.partial.download`，`LSTypeIsPackage=1`） | A |
| 存在"自定义集成"包格式 | UTType `com.charliemonroe.downie.custom.integration`，扩展名 `.dci` | A |
| 支持拖拽/服务菜单提交链接 | `NSServices`：`dropURLsOnDockIcon`、`dropStringOnDockIcon` | A |
| 支持 Automator 动作 | `Contents/Library/Automator/Open URLs in Downie.action` | A |
| 支持 AppleScript | `OSAScriptingDefinition = Downie.sdef`、`NSAppleScriptEnabled = true` | A |
| 支持系统快捷指令（Shortcuts） | `NSUserActivityTypes` / `INIntentsSupported`：`XUDownloadURLsIntent`、`XUDownloadURLsInTextIntent` | A |
| 菜单栏常驻模式 | 资源 `XUStatusBarItemPopoverContentViewController.nib` | B |
| 自动更新（Sparkle） | `Frameworks/Sparkle.framework`、`Updating.framework`、`SUFeedURL`、`SUPublicDSAKeyFile = dsa_pub.pem`、3 个 `org.sparkle-project.*.xpc` | A |
| 商业授权与支付 | `Frameworks/Licensing.framework`、`Paddle.framework`、`CMLicensingScheme = downie-license`、`XUBuildType = Trial` | A |
| 元数据处理独立进程 | `XPCServices/MetadataProcessor.xpc`（内置 `tag.framework`）—— 在沙箱进程中改写媒体标签 | A |
| 允许任意网络加载 | `NSAppTransportSecurity.NSAllowsArbitraryLoads = true` | A |

---

## 2. 下载引擎（核心）

`Frameworks/DownieCore.framework` 是全部下载逻辑所在。对其字符串做站点类名扫描，得到 **1611 个**形如 `…17IqiyiDownloader`、`…16Music163Downloader`、`…17YouPornDownloader` 的类名（Symbol 层面被 hash 前缀混淆，形如 `0289FB2BFF5C35996B27CB1F7637A76E16DisneyDownloader`）。`[C]`

这解释了产品宣称的"支持 1000+ 站点"：**不是通用嗅探，而是按站点手写抽取器 + 少数共享基类**（可辨识 `DisneyBaseDownloader`、`XVideosEmbedDownloader`、`IqiyiAltDownloader`、`POLYVConfigDownloader`、`VzanPageDownloader`）。`[C]`

### 2.1 原生 HLS/M3U8 引擎

| 结论 | 证据字符串 | 级别 |
|---|---|---|
| 有独立原生 HLS 引擎 | `Use native M3U8 (HLS) download engine`、`Use native HLS download engine`、`The native HLS Engine is faster, more powerful and more efficient.` | B |
| 自研 M3U8 解析 + AES-128 解密 | `_decryptM3U8Data(_:)`、`_aesEncryptionKeyURL`、`_aesEncryptionKeyData()`、`_calculateDecryptionKey(with:k:)`、`_decryptChunkData(_:)`、`_decryptURLs(from:)`、`#EXT-X-KEY:METHOD=(.*)`、`#EXT-X-KEY:METHOD=AES-128`、`#EXT-X-KEY:METHOD=NONE`、`aes128ShouldStripPKCS7Padding` | C |
| 支持 AES-128 之外的密钥派生 | `_loadDecryptionKey(for:deviceID:token:)`、`_decryptedKey(_:deviceID:cid:)`、`bulkaes`、`[media][tv][bulkaes][secureUrl]` —— 典型的 Bilibili 类"设备指纹 + bulk AES"授权 | C |
| 分片级加密数据的缓存 | `_cachedAESEncryptionData` | C |
| 直播流支持 | `_checkLiveStreamStall()`、`_checkRetryLiveStreamEnd()`、`Use MPEG-TS container for live streams`、`The TS container is resistent to e.g. power failure…` | B/C |
| DASH 感知 | `_dashQualities(from:)`、`Forced postprocessing of HLS and DASH streams`、`Force re-encode HLS and DASH streams` | B/C |
| 相关流合并 | `Merge related HLS Streams` | B |
| 多连接/分片并发 | `Limit Number of Connections`、`Enable chunked downloads`、`Chunked Downloads` | B |

### 2.2 签名与反爬绕过

| 结论 | 证据字符串 | 级别 |
|---|---|---|
| 内嵌并执行站点下发的 JS | `var cmd5x_func = %(cmd5x_func)s; var cmd5x_exporter = {}; cmd5x_func({}, cmd5x_exporter, {}); var cmd5x = cmd5x_exporter.cmd5x;` | C |
| 该 JS 用于生成播放地址签名 | `var dash_path = '/dash?' + enc_params.join('&'); dash_path += '&vf=' + cmd5x(dash_path);`、`var authKey = cmd5x(cmd5x('') + tm + '' + tvid);`、`dash_paths[bid] = dash_path; return JSON.stringify(dash_paths);` | C |
| 通用签名批处理框架 | `_performBatchSignature(for:signatureFunctionName:caller:)`、`_performBatchSignatures(for:)`、`signature requests:`、`will try it without the signature.` | C |
| YouTube 侧走 player response 解析 | `_qualityInPlayerResponseDictionary(_:configuration:signatureHandler:)` | C |
| YouTube 需要新版 JS 运行时 | `Downloads from YouTube now require more specific JavaScript-run signatures which are not fully operational on macOS 11.` | B |
| 支持 CloudFront 签名 | `CloudFront-Signature`、`[player][cloudFrontSignature]` | C |

> 结论 `[D]`：Downie 的"每个站点一个下载器"实质是「站点元数据 + 内嵌可更新 JS 签名函数」的组合，这也是它能在 YouTube 频繁改签名后靠软件更新维持可用的原因。

### 2.3 抽取失败时的兜底

| 结论 | 证据 | 级别 |
|---|---|---|
| 主流程失败 → 引导用户使用 User-Guided Extraction | `But do not give up! Downie has two more tricks up its sleeve: you can either try a user-guided extraction, where you load the link in Downie's browser and Downie detects supported media and links; or you can request support for the links…` | B |
| UGE 是一个内置浏览器 | `XUGuidedExtractionWindowController.nib`、`XUUGEBookmarksViewController.nib`、`XUUGEImagesViewController.nib`、`XUAuxiliaryWebViewWindowController.nib`、`Clear User-Guided Extraction History...`、`Show Extracted Links`、`UGEHistoryItemsController.nib` | B |
| 存在"从网页资源中挑一个记住"的自动化 | `You will now need to select resource loaded by the webpage to be automatically selected by Downie. This works similar to User-Guided Extraction.` | B |
| 内置登录浏览器（独立 Cookie 域） | `XULoginHelperWindow.nib`、`Show Login Browser...`、`Downie does not share login sessions with your browser.`、`Manage Website Data`、`Remove All Cookies...` | B |
| 不支持的链接可上报 | `XUURLReportingController.nib`、`CustomIntegrationSubmissionViewController.nib`、`CustomIntegrationOnlineRegistryBrowserViewController.nib` —— 存在在线集成注册表，可下载他人提交的 `.dci` 集成 | A/B |

---

## 3. 浏览器扩展

`PlugIns/Downie Extension New.appex` 与 `Downie Extension.appex` 均为 **Safari Web Extension**（`NSExtensionPointIdentifier = com.apple.Safari.web-extension`）。扩展本体是标准 MV3 包，可直接阅读。`[A]`

```json
"manifest_version": 3,
"version": "1.2.4 (85)",
"background": {"scripts": ["background.js"], "type": "module"},
"content_scripts": [{"js": ["content.js"], "matches": ["http://*/*", "https://*/*"]}],
"permissions": ["contextMenus", "tabs", "nativeMessaging", "scripting"],
"host_permissions": ["http://*/*", "https://*/*"]
```

`content.js`（全文 549 字节）只做一件事：监听快捷键并把当前标签页交给宿主应用，**同时把 `document.cookie` 一起带过去**。`[C]`

```js
if (keyCode == 68 && e.ctrlKey && !e.metaKey && !e.altKey && e.shiftKey) {
    browser.runtime.sendMessage({ action: "openCurrentTab", cookies: document.cookie ?? "" });
} else if (keyCode == 68 && e.ctrlKey && !e.metaKey && e.altKey && e.shiftKey) {
    browser.runtime.sendMessage({ action: "openAllTabs" });
}
```

即 **⌃⇧⌘D = 用 Downie 打开当前页，⌃⌥⇧⌘D = 打开所有标签页**；`background.js` 通过 `nativeMessaging` 转交本机应用（对应 `Pass Cookies to Downie` 词条）。

另外 `Share Extension.appex` 提供系统分享面板入口。`[A]`

---

## 4. 功能清单（UI 与设置项）

`en.lproj/Localizable.strings` 共 **914 条**词条、183 KB，是功能面最完整的证据源。归类如下。

### 4.1 队列与调度
- 并发数：`Max Number of Concurrent Downloads:` / `Number of Concurrent Downloads` / `Use 0 for unlimited.`
- 自动开始、完成后移出队列：`Start Downloads Automatically`、`Remove Completed Downloads from Queue`
- 队列顺序与重置：`Queue Order`、`Reset Queue...`、`Do you want to reset the download queue?`
- 崩溃自愈：`Downie detected that it's been crashing a lot on you lately. If this is caused by something that's saved in the queue, the queue can be reset now.`（对应启动时按住 Option）
- 优先级：`Priority`
- 无人值守模式：`Automated Mode` / `Run in automated mode` / `In automated mode, Downie will disable any prompts, such as login, quality selection…`
- 批量导入：`Select CSV or XML file from which Downie should import downloads to be added into the queue.`

### 4.2 画质与格式
- 画质上限枚举：`Limit to 4K` / `Limit to 1080p` / `720p` / `480p` / `360p` / `240p` / `144p`、`Automatically Select Best Quality`
- **重要语义（被本次实现原样复刻）**：`If you limit the downloads to e.g. 480p and only larger videos are available, then the smallest available video will be chosen.`
- 容器偏好：`Prefer MP4 Format` / `Force MP4 Format` / `Convert to MP4` / `Keep Original Format` / `Original Format`
- 同名分辨率下的容器取舍：`If enabled and multiple renditions with the same resolution are available, Downie will try to select the MP4 rendition, even if there are non-MP4 renditions that have higher framerate, bitrate and other attributes.`
- 仅音频：`Extract Audio Only`、`Audio Only`、`Enforce MP3`、`Preferred Audio Format`、`When using audio-only postprocessing option, Downie will try to use the preferred audio format.`
- 同名方案：`File Name Format:` / `The file name format must contain title.` / `The information in the format is insufficient.`

### 4.3 字幕
- `Download Subtitles` / `Download Without Subtitles` / `Download Subtitles if Available` / `Download Subtitles Only` / `Download Only Subtitles`
- 自动语言匹配：`Automatically Select Subtitles Labeled As:`、`Automatically Select Audio Labeled As:`、`The match is case insensitive.`
- 模式：`Subtitles Download Mode` — `Single mode downloads a single matching subtitles file. Multiple mode will download all matching subtitles.`
- 其他：`Include Generated Subtitles`（自动生成字幕）、`No Subtitles`、`Embedded`

### 4.4 后期处理
- `Postprocessing` / `Perform Postprocessing` / `No Postprocessing` / `Disable Postprocessing`
- **自定义脚本**：`Edit Custom Script...`、`XUPostprocessingPreferencesViewController.CustomScriptEditorController.nib`
- 失败保留原文件：`…enable this option to keep the original download file, so that you can try to re-run the conversion manually.`
- 与 Permute 联动：`Permute's Preset:`、`Permute is a powerful media conversion utility… Downie can integrate with Permute, allowing you to specify into which format to convert the downloaded file.`、`No Permute preset set.`
- 元数据导出：`Save Metadata to JSON File` / `Save Metadata JSON...`
- 自动导入媒体库：`Downie uses AppleScript to add converted items to Music/iTunes or other media apps.` `[A]`

### 4.5 下载后动作
`Post-Download Actions`、`Add to Queue`、`Reveal in Finder`、`Post Notification`、`Open in User-Guided Extraction`

### 4.6 网络
- 代理：`Use Proxy Server`、`Proxy Server`、`Downie can take advantage of a HTTPS proxy… or of a SOCKS proxy…`、`Note: User-Guided Extraction and Login Browser features do not yet support the proxy server.`
- Cookie / 登录：`Has Cookie`、`Pass Cookies to Downie`、`Login to %@`、`Cancel Login`、`Disable Login Requests For This Site`
- 限速与连接数、重试（由 `XUNetworkPreferencesViewController.nib` 承载）

### 4.7 历史与检索
`History`、`Show History`、`XUHistoryWindowController.nib`、`Clear History Automatically`、`Synchronize History`（iCloud）、`Automatically clear deleted files`（`Automatically removes files that are no longer on your hard drive.`）
站内搜索：`Downie can search for videos on various sites (YouTube, Vimeo, TED, ...) right from the main window.`、`Search and Top Downloads`、`XUSearchAndTopDownloadsViewController.nib`

### 4.8 站点支持管理
`XUSupportedSitesViewController.nib`、`Downie includes various smart extraction techniques which can detect content on sites that are not on the list, or the extraction is possible with the User-Guided Extraction.`、`Manage Website Data`

### 4.9 媒体处理依赖
`Contents/Resources/ffmpeg`：**66 MB 通用二进制**（x86_64 + arm64），`ffmpeg version 8.0.1`，编译参数 `--enable-static --disable-shared --enable-libmp3lame --enable-libvorbis --enable-libaom --enable-libopenh264`。`[A]`
→ 说明导出音频（MP3/Vorbis）、AV1 转码、解码/复用全部依赖内置 ffmpeg，与 Permute 的联动只是 UI 层扩展。

### 4.10 本地化
35 个 `.lproj`（含 `zh-Hans`、`zh-Hant`）。`[A]`

---

## 5. 重建方案：Downie → omadown

REA 的落点是"功能等价重建"，不是复制。以下为逐项映射；**未复用任何 Downie 代码、资源或字符串**。

| Downie 能力 | 证据锚点 | omadown 实现 | 文件 |
|---|---|---|---|
| 1000+ 站点抽取器 | 1611 个 `*Downloader` 类 `[C]` | 复用一个久经维护的开源抽取器生态：以 `yt-dlp` CLI 为主引擎，按 `--progress-template` 解析进度、`--print after_move` 取回落地路径 | `backends/ytdlp.py` |
| 原生 M3U8 引擎 | `Use native M3U8 (HLS) download engine` | 自研 M3U8 解析器（master/media、`EXT-X-KEY`、`EXT-X-BYTERANGE`、`EXT-X-MAP`、`EXT-X-MEDIA` 音轨组）+ 并发分片 + 断点 + 直播轮询 | `backends/hls.py` |
| AES-128 分片解密 | `_decryptM3U8Data(_:)`、`#EXT-X-KEY:METHOD=AES-128` | `crypto.py`：纯 Python AES-CBC 实现（无第三方依赖），优先走 `cryptography` 快路径；含 PKCS#7 去填充、按 media sequence 推导默认 IV | `crypto.py` |
| HLS 直播 + MPEG-TS 容器 | `Use MPEG-TS container for live streams` | 无 `EXT-X-ENDLIST` 判定为直播，循环拉取新分片，输出 `.ts` | `backends/hls.py` |
| User-Guided Extraction | `XUGuidedExtractionWindowController.nib` + 兜底文案 | 两级网页嗅探：① 静态挖掘 `<video>/<source>/og:video/JSON-LD/内联脚本（含 `\/` 转义还原）`；② 装了 Playwright 时用 headless Chromium 抓网络请求 | `backends/sniff.py` |
| 直链/断点续传 | `.downiepart` 包 + 断点续传 | HTTP Range 续传下载器，作为兜底引擎 | `backends/direct.py` |
| 浏览器扩展（带 Cookie） | `content.js` 的 `document.cookie` + `nativeMessaging` | Wayland 剪贴板守护：`wl-paste --watch` 实时捕获，按已知站点/媒体特征过滤，带去重 | `watch.py` |
| 画质上限语义 | `If you limit… the smallest available video will be chosen.` | 同一语义：`≤cap` 取最大，否则取最小；`-S res:N` 传给 yt-dlp，native HLS 侧由 `select_variant()` 实现 | `hls.py`、`ytdlp.py`、`engine.py` |
| Prefer / Force MP4 | `Prefer MP4 Format` / `Force MP4 Format` | `--merge-output-format mp4` + `-S ext:mp4:m4a`（Prefer）；`-f "bv*[ext=mp4]+ba[ext=m4a]/…"`（Force） | `backends/ytdlp.py` |
| 仅音频 / Enforce MP3 | `Extract Audio Only`、`Enforce MP3` | `-x --audio-format`，或本地 ffmpeg 抽取（m4a/mp3/flac/opus/ogg/wav） | `ytdlp.py`、`postproc.py` |
| 字幕（单/多、自动语言、仅字幕） | `Subtitles Download Mode`、`Download Subtitles Only` | `--write-subs/--write-auto-subs/--sub-langs …:1`、`--embed-subs`、`--skip-download`；本地封装 `mov_text` | `ytdlp.py`、`postproc.py` |
| 文件名方案 | `The file name format must contain title.` | 同规则校验（缺 `{title}` 直接报错）+ 逐段清洗（非法字符、隐藏文件、`..` 穿越、字节级截断） | `naming.py` |
| 后期处理 + 自定义脚本 | `Edit Custom Script...` | ffmpeg 复用/转码（VAAPI/NVENC/QSV/软件回退）/抽音轨/封装字幕/写元数据；自定义脚本以 `OMADOWN_FILE` 等环境变量注入 | `postproc.py` |
| 内嵌 ffmpeg | `Resources/ffmpeg` 8.0.1 | 改用发行版 ffmpeg（`omarchy-pkg-install ffmpeg`），`doctor` 检查版本可用性 | `install.sh`、`cli.py` |
| 队列/并发/优先级/重试 | `Max Number of Concurrent Downloads:`、`Priority` | SQLite 持久化队列 + 线程池调度 + 优先级排序 + 指数重试；启动时把中断任务复位（等价 `Reset Queue`） | `engine.py`、`db.py` |
| 无人值守模式 | `Run in automated mode` | `general.automated`，禁用交互式提示 | `config.py` |
| 历史记录 | `XUHistoryWindowController.nib` | SQLite 历史 + `--history` 列表 + 自动裁剪 | `db.py` |
| 菜单栏常驻 | `XUStatusBarItemPopoverContentViewController.nib` | Waybar 自定义模块，输出状态 JSON（含进度条、速度、ETA） | `status.py` + `integrations/omarchy/` |
| 通知 | `NSUserNotificationsUsageDescription` | `notify-send`（Dunst/Mako 均可） | `notify.py` |
| 拖拽 / 服务菜单 / 快捷指令 | `NSServices`、`NSUserActivityTypes` | `omadown://` 桌面条目 + `add` 子命令；Hyprland `bindd`/`exec` 直接投喂 | `integrations/omarchy/` |
| AppleScript 自动化 | `Downie.sdef` | Unix socket 控制通道（`ping/status/add/retry/cancel/remove/pause/resume/clear/shutdown`），任何脚本都能驱动 | `ipc.py` |
| 多语言 | 35 个 `.lproj` | 界面文案中文为主（本机使用场景） | `tui.py`、`cli.py` |
| 许可/支付/自动更新 | `Paddle.framework`、`Sparkle.framework` `[A]` | **不重建**：个人使用的开源实现，无授权与更新链路 | — |

### 5.1 未重建 / 有意不做

- **DRM 与 Widevine/PlayReady/FairPlay**：`hlsv6_widevine_nonclearlead`、`hlsv6_playready_psshv0`、`hlsv6_fairplay` 等字符串表明 Downie 会识别这些流但只做选择性处理。omadown 不包含任何 DRM 绕过。
- **站点专属签名 JS 的自研实现**：Downie 通过 `_performBatchSignature` 执行站点下发 JS。omadown 交由 yt-dlp 生态维护，不做逆向复刻。
- **iCloud 历史同步、Paddle 授权、Sparkle 更新**：属于平台/商业绑定，无对应需求。
- **Permute 联动**：闭源应用内集成，omadown 以自定义脚本钩子提供等价扩展点。

### 5.2 静态分析的边界

- 1611 个抽取器类的**内部算法无法从字符串得到**，只能确认其存在与命名规律。
- `.nib` 为二进制归档，只能确认控制器存在与承载的偏好页，不能还原精确控件布局。
- 未做动态分析（不运行程序），因此"某功能在哪个版本可用/是否加锁"这类运行期判定不在本报告范围内。

---

## 6. 结论

Downie 4 是一个**"站点抽取器矩阵 + 自研 HLS 引擎 + 内置浏览器兜底 + 后期处理流水线"**的桌面应用：

1. 广度靠 1611 个手写站点类，深度靠可按版本更新的内嵌 JS 签名函数；
2. 鲁棒性靠三层兜底：站点抽取器 → 通用嗅探 → 用户引导抽取（内置浏览器 + 登录会话隔离）；
3. 体验壁垒在桌面集成：URL scheme、服务菜单、扩展带 Cookie 投递、菜单栏常驻、通知、AppleScript。

`omadown` 在这三点上都给出了 Linux/Hyprland 语境下的对应物，并把 Downie 那些**容易被忽略的语义细节**（画质上限的降级规则、MP4 偏好与强制的区别、文件名方案必须含 `{title}`、失败保留原文件、无人值守模式）逐条复刻。落地验证见仓库 `tests/`（75 项 `unittest` + `tests/smoke_pty.py` 真终端冒烟：用 ffmpeg 生成真实 HLS 阶梯并通过 HTTP 端到端下载、`ffprobe` 校验可播放性、`forkpty` 跑 curses 界面与守护进程）。
