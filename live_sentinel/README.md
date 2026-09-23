# live_sentinel V1

这是《B站直播智能监听与内容归档系统——概要设计》的第一阶段可运行骨架。它先把
与服务商无关的核心链路落地，实际 B 站音频输入、Streaming ASR、Offline ASR、LLM
Judge 和通知渠道都通过接口注入。

如果要交给更强模型做独立审阅，先阅读
[`AUDIT_REVIEW_PACKET.md`](AUDIT_REVIEW_PACKET.md)；如果要从“长期关注来源”而不只是
“手工输入 URL”重新设计使用方式，参阅
[`USAGE_DESIGN_RECOMMENDATION.md`](USAGE_DESIGN_RECOMMENDATION.md)。

## 已实现

- `AudioSource` / `IterableAudioSource` / `QueueAudioSource`：授权 PCM 输入抽象；
- `FFmpegAudioSource`：将调用方提供的、已授权的音频输入转为 PCM；不负责获取权限或绕过 DRM；
- `AudioTee`：归档队列优先，实时队列拥塞时允许降级；
- `SegmentArchiveWriter`：小时级 working segment，支持 FLAC（需 ffmpeg）或 WAV；
- `AudioFinalizer`：时间轴检查、无损合并、解码测试、时长校验和 SHA-256；
- `RealtimeAudioPreprocessor` 与无依赖 `EnergyVAD` 基线；
- 30 秒至 15 分钟滚动转写缓存、规则内容分析、兴趣评分和干货状态机；
- SQLite 中的 sessions、audio、transcript、highlight、chapter、event 表；
- 原子写入的时间轴 checkpoint，便于异常后判断最后已归档位置；
- 文稿/字幕/章节/重点片段/摘要产物写出器；
- 可在 JSON/YAML 中修改的关键词、结构标记、权重和语义混合比例；
- 可独立暂停的实时智能链路；关闭后仅保留录音和会后离线转写；
- 飞书新版云文档上传（摘要文档 + 全文转写文档）；
- T5 主归档后的 bypy/百度网盘异地备份适配器；
- 持久化 Watchlist、B 站直播发现、同房间 Session 去重和指数退避；
- `sentinel` source/status/service CLI 与 macOS launchd 用户服务；
- 本地 mock 端到端演示。

## 运行 mock 闭环

在仓库根目录执行：

```bash
python3 -m live_sentinel demo --output-dir /tmp/live-sentinel-demo --duration-sec 8
```

演示使用合成 PCM 和 WAV，是为了不把真实抓流和 ASR 服务伪装成已经接通。输出目录
包含 `audio/final/*.wav`、`final/verbatim.md`、`final/readable.md`、`final/subtitles.srt`、
`final/chapters.json`、`final/highlights.json`、`final/summary.md` 和 `session.sqlite`。

需要读取 `config.example.yaml` 时安装可选依赖 `PyYAML`；只使用 JSON 配置则无需额外依赖。

## 测试

```bash
python3 -m unittest discover -s tests/live_sentinel -t . -v
```

对一个仍在直播的 B 站页面做五分钟采集验证：

```bash
python3 scripts/validate_bilibili_live.py '<直播页 URL>' \
  --duration-sec 300 --output-dir sessions
```

## 真实服务接入

默认推荐这一组组合：

| 能力 | 默认实现 | 原因 |
| --- | --- | --- |
| Streaming ASR | Apple `SpeechTranscriber` | 本机处理 VAD 语音段，低延迟、无云端音频费用；用于实时语义判断，不承担最终逐字稿 |
| Offline ASR | 本地 FunASR `SenseVoice` | 会后按任务启动，生成完整逐字稿；不占用常驻内存，也不产生云端音频费用 |
| LLM Judge | DeepSeek `deepseek-v4-flash` | OpenAI-compatible API，JSON 输出适合高频小请求 |
| 通知 | 飞书自定义机器人 Webhook 或 ntfy | 飞书适合群协作；ntfy 适合个人跨设备推送 |

如需改用云端离线转写，可将 Offline ASR 配置为 OpenAI `gpt-transcribe`；上层只依赖
`offline_asr(path)`，无需改 Session Manager。

适配器只从环境变量读取凭据，不会把密钥写入配置文件：

```bash
export OPENAI_API_KEY='...'
export DASHSCOPE_API_KEY='...'
export DEEPSEEK_API_KEY='...'
export FEISHU_WEBHOOK_URL='https://open.feishu.cn/open-apis/bot/v2/hook/...'
# 飞书机器人开启“签名校验”时再设置：
export FEISHU_WEBHOOK_SECRET='...'
# 使用 ntfy 时，填写完整发布地址（例如 https://ntfy.sh/<随机 topic>）：
export NTFY_URL='https://ntfy.sh/<随机 topic>'
# 自托管 ntfy 且开启认证时再设置；公共随机 topic 可以不设置：
export NTFY_TOKEN='tk_...'
# 飞书云文档（租户应用；不是自定义机器人 Webhook）
export FEISHU_APP_ID='cli_...'
export FEISHU_APP_SECRET='...'
export FEISHU_DOCS_FOLDER_TOKEN='fldcn...'
export FEISHU_DOCS_HUMAN_MEMBER_ID='ou_...'
# 文档链接模板可按企业域名设置，例如：
export FEISHU_DOC_URL_TEMPLATE='https://your-tenant.feishu.cn/docx/{document_id}'
# 直播结束摘要发送到飞书群 A；可与高价值事件 Webhook 分开
export FEISHU_SUMMARY_WEBHOOK_URL='https://open.feishu.cn/open-apis/bot/v2/hook/...'
```

安装实时 WebSocket 依赖：

```bash
python3 -m pip install -e '.[providers]'
```

直接使用内置配置时，脚本会自动启用已具备凭据的适配器；缺少凭据的单项能力会
降级，完整音频归档仍继续：

```bash
python3 scripts/validate_bilibili_live.py '<直播页 URL>' \
  --duration-sec 300 --output-dir sessions
```

要强制检查已配置的外部适配器凭据、飞书云文档和 bypy，可增加 `--strict-integrations`。也可以
从 `config.example.json` 复制一份自己的 JSON 配置，用 `--config` 指定；不要在
该文件中写入真实密钥，只写环境变量名。

`provider` 可选值：

- `asr.realtime.provider`: `apple_speech`（别名 `apple` / `speechtranscriber`）、`dashscope`（别名 `aliyun` / `paraformer`）、`openai`、`none`；
- `asr.offline.provider`: `openai`、`funasr_local`、`none`；
- `llm.provider`: `deepseek`、`openai`、`codex`、`none`。`codex` 是 OpenAI API 的模型别名配置，默认仍建议用通用文本模型；
- `notification.provider`: `feishu_webhook`、`ntfy`、`none`。

ntfy 配置示例（`NTFY_URL` 必须是完整的 publish endpoint，不要把 token 写进 URL）：

```yaml
notification:
  enabled: true
  provider: ntfy
  ntfy_url_env: NTFY_URL
  ntfy_token_env: NTFY_TOKEN
  ntfy_title: Live Sentinel
  ntfy_priority: high
  ntfy_tags: [fire]
  # 可选：点击通知时打开的地址
  ntfy_click_url: null
  timeout_sec: 10
```

ntfy 的正文是纯文本事件摘要，标题、优先级、标签和可选点击地址通过 HTTP
headers 发送。`high` 适合热点提醒；`max` 应只用于确实需要立即打断的告警。
如果使用公共 ntfy 服务，topic 名等同于发布凭据，应使用不可猜测的随机 topic；
对更高的隐私和访问控制要求，可以改用自托管 ntfy 并设置 `NTFY_TOKEN`。

手机 ntfy 连调：手机应用先订阅与 `NTFY_URL` 相同的 topic，然后执行：

```bash
NTFY_URL='https://ntfy.sh/<随机 topic>' \
python3 scripts/test_ntfy.py
```

脚本只打印请求是否成功，不打印 token。当前机器没有预置 `NTFY_URL`，因此真实手机
送达必须由部署者提供自己的 topic；仓库测试只验证 HTTP 请求头、正文和认证逻辑。

## 暂停实时转写、高价值判断和即时通知

把 `asr.realtime.enabled` 设为 `false`，即可暂停整条实时智能链路：

```yaml
asr:
  realtime:
    enabled: false
```

关闭后不会启动实时音频工作线程，不做 Streaming ASR、规则/LLM 高价值判断，也不会发送
即时高价值通知；P0 录音、Final Audio、会后 Offline ASR、飞书文档、异地备份和直播结束
摘要仍照常执行。也可以沿用 `asr.realtime.provider: none`，其效果相同。

## 高价值判断逻辑如何修改

不必修改 `analyzer.py` 或 `scorer.py`。在配置文件中调整：

- `interest.whitelist` / `blacklist`：词表；
- `structure_markers` / `density_markers`：结构化表达和信息密度特征；
- `weights`：各特征权重；
- `semantic_blend`：规则分与 LLM Judge 分的混合比例；
- `blacklist_multiplier`：黑名单主题惩罚；
- `candidate_threshold` / `hot_threshold`：状态机阈值。

所有分析事件会保留特征值，便于之后回看“为什么触发”。

## 飞书云文档上传

开启 `feishu_docs` 后，每场 Session 完成 OfflineFinalizer 后会创建两篇新版文档：

1. `直播摘要｜<session_id>`：摘要、重点和章节；
2. `直播全文转写｜<session_id>`：`readable.md` 全文。

文档创建使用飞书新版 Docx Open API 的 `POST /open-apis/docx/v1/documents`，再按
Block 写入正文。生产配置要求指定目标目录和人类账号；不会再退回到应用私有空间。
每篇文档写完后，系统授予并回读人类账号的 `full_access`，随后转移所有权，同时
保留应用后续维护权限。归档目录会保存 `final/feishu_docs.json`，记录 document ID、
链接、目录、权限验收和所有权状态。

按最小权限拆分时，只需给应用身份开通：

- `docx:document:create`：创建新版文档；
- `docx:document:write_only`：向文档写入 Block；
- `docs:permission.member:create`：授予目标人类账号权限；
- `docs:permission.member:retrieve`：回读并验证目标权限；
- `docs:permission.member:transfer`：将最终所有权转给人类账号；
- `space:folder:create`：仅用于一次性创建应用可写的交付目录。

不需要通讯录、消息、机器人或对外公开分享权限。当前实现不读取文档正文；原始音频
也不会上传飞书。缺少目标目录或人类账号时，只禁用飞书交付，本地采集和 T5 主归档
继续运行。

首次部署可从一篇已经能被目标账号打开的应用文档发现其 `open_id`，创建专用目录，
转移目录所有权并生成无敏感内容的验收文档：

```bash
uv run python scripts/setup_feishu_delivery.py \
  --existing-doc-id '<existing_document_id>'
```

脚本成功后，把输出的 `FEISHU_DOCS_FOLDER_TOKEN` 和
`FEISHU_DOCS_HUMAN_MEMBER_ID` 写入私有 `.live-sentinel.env`。

授权后可用一篇不含直播内容的最小文档做连通性验证：

```bash
uv run python scripts/test_feishu_docs.py
```

## T5 主存储与百度网盘备份

正式部署配置建议：

```yaml
storage:
  root_dir: /Volumes/T5/zettaranc-live/sessions
  require_mounted_path: true
  minimum_free_gib: 20
backup:
  enabled: true
  provider: baidu_bypy
  command: bypy
  remote_root: /live-sentinel
```

使用该配置时不要再传 `--output-dir sessions`；省略 `--output-dir`，脚本会采用
`storage.root_dir`，例如：

```bash
python3 scripts/validate_bilibili_live.py 'https://live.bilibili.com/1616' \
  --config config.user.json --duration-sec 21600
```

首次使用前安装 `bypy` 并手工完成一次授权；Session 完成后系统执行
`bypy upload <session_dir> <remote_root>/<session_id>`，结果写入 `backup.json`。
找不到 `bypy` 或上传失败不会删除 T5 主档，但会记录 `BACKUP_UNAVAILABLE` 或
`BACKUP_ERROR`。百度网盘官方帮助页对个人版音频格式的说明较保守；企业版/开放平台资料
明确列出 FLAC，但不同终端可能有差异，故建议保留 FLAC 主档并在需要时生成 Opus/AAC
试听副本。

对已有目录可以单独试传：

```bash
python3 scripts/backup_session.py /Volumes/T5/zettaranc-live/sessions/<session_id>
```

## Watchlist 自动调度

Watchlist 不是一次执行的 URL 参数，而是一个长期保存的“关注源清单”。一行通常包括：

```text
关注对象 + 主播 UID/短号 + 运行时段 + 内容规则 + 通知路由 + 保留策略
```

例如 `B 站 UID 326246517 / 短房间号 1616 / 每周三、日 19:30–24:00 / ntfy 高价值提醒 / 飞书云文档 / T5+百度备份`。
后台服务按 Watchlist 自动解析直播、启动和停止 Session；用户不需要每次重新复制 URL。
`validate_bilibili_live.py` 继续保留为单场诊断入口。

当前发现频率按 `Asia/Shanghai` 分为三档：每天 00:00–19:00 每 3 小时一次；
每天 19:00–24:00 每 1 小时一次；周三和周日 19:00–24:00 覆盖为每 5 分钟一次。
调度器不会跨过时段边界：例如 18:30 的低频检查会把下一次检查安排在 19:00，
而不是 21:30。已经发现直播后，Session 会立即启动；直播状态复查保持每 5 分钟一次。

安装项目依赖后，一次登记来源：

```bash
sentinel --config config.user.json --env-file .live-sentinel.env \
  source add 'https://live.bilibili.com/1616' --name 'zettaranc' --uid 326246517

sentinel --config config.user.json --env-file .live-sentinel.env status
```

安装并立即启动 macOS 用户级后台服务：

```bash
sentinel --config config.user.json --env-file .live-sentinel.env service install
sentinel --config config.user.json --env-file .live-sentinel.env service status
```

服务把 desired state 和 observed state 分开保存在
`watchlist.state_db`。同一规范 room_id 同时最多一个 Session；一次 Session 完成后，必须先
观测到 OFFLINE，再观测到下一次 LIVE 才会重启，避免重复录制同一场。发现错误采用指数退避；
每次发现结果或错误都会追加到 `service_events`，因此历史轮询不再只剩最后一次快照。
服务重启会把遗留 RUNNING 标记为 INTERRUPTED，同时同步对应 Session 的 SQLite 和 JSON，
并允许仍在线房间恢复。`source disable`
停止后续自动启动，但不会粗暴中断正在归档的本场 Session。

本机凭据可放在 gitignored 的 `.live-sentinel.env`，每行 `KEY=VALUE`；建议权限为 `0600`。
launchd 日志位于 `~/Library/Logs/LiveSentinel/`，plist 位于
`~/Library/LaunchAgents/com.zettaranc.live-sentinel.watchlist.plist`。主服务日志
`watchlist.err.log` 每日轮转并保留 14 天；launchd 原始 stderr 单独写入
`watchlist.launchd.err.log`，避免干扰轮转。日志、Watchlist 错误和 Session 事件共用
同一套凭据脱敏规则。

普通公开直播默认使用匿名播放接口。对于付费、充电专属或其他需要登录权限的直播，
解析器会在新版接口无流时读取登录直播页的预载数据，并支持 `HLS/fMP4`；登录 Cookie
只保存在 gitignored 的本机凭据文件，不写入日志或 Session 产物。已在 Edge 登录 B 站时，
可以只读导入最小 Cookie 集：

```bash
pip install -e '.[credentials]'
python3 scripts/import_bilibili_cookie.py --from-edge \
  --env-file .live-sentinel.env
```

Cookie 到期后需要重新导入，并重启 Watchlist 服务使新进程重新加载凭据。

如果登录页只提供 DRM 流，系统不会尝试破解 DRM。可以把已获授权播放器的系统音频
输出到虚拟设备，再由 AVFoundation 回采。以下配置会在 Session 开始时把系统输出
切到 BlackHole，确认当前输出精确为 `BlackHole 2ch` 后才将系统输出音量设为
100% 并取消静音。Session 结束、失败或受控停止后继续保持 BlackHole 及
当前音量，不自动恢复 Mac mini 扬声器或原音量。该 fail-safe 策略避免无人值守时
突然外放；但其他应用和
系统通知的声音也可能进入录音。需要扬声器时，必须由用户手动切回：

```bash
brew install switchaudio-osx
printf '%s\n' 'BILIBILI_DRM_AUDIO_DEVICE=BlackHole 2ch' >> .live-sentinel.env
```

对于同一场直播会从公开流切换为充电/DRM 流的房间，应从开场就固定使用
已登录浏览器 + BlackHole，避免中途更换采集传输。房间级配置示例：

```json
{
  "bilibili_capture": {
    "browser_audio_room_ids": ["11163068"],
    "status_poll_interval_sec": 30,
    "offline_confirmations": 3,
    "offline_confirmation_interval_sec": 5,
    "continuous_silence_timeout_sec": 180,
    "silence_rms_threshold": 100
  }
}
```

该模式每 30 秒独立核验房间状态；只有连续 3 次确认下播才正常结束。
若房间仍在直播却发生音频 EOF，或连续 180 秒静音，Session 会失败并
保持 Watchlist 可重试，不会误报整场完成。`events.jsonl` 和 SQLite `events`
会记录 `CAPTURE_MODE_SELECTED`、`STREAM_ACCESS_MODE_CHANGED`、
`AUDIO_SILENCE_WHILE_LIVE` 和 `ROOM_OFFLINE_CONFIRMED` 等审计事件。
录制、实时 ASR、离线 ASR、摘要、T5 发布和飞书交付统一使用
`STAGE_STARTED`、`STAGE_SUCCEEDED`、`STAGE_FAILED`、`STAGE_SKIPPED`；阶段名位于
事件 payload 的 `stage` 字段。阶段事件在首帧前就开始落盘，因此音频路由、FFmpeg、
浏览器打开或开场音频探测失败也会留下 Session 级证据。

无人值守模式可在切换输出设备后，用当前 macOS 用户已经登录并获授权的浏览器打开
直播页。默认使用 Microsoft Edge；音频输入会先在内存中缓存最多 20 秒，累计检测到
至少 500ms 非静音音频后才交给归档。探测失败会把 Session 标为失败，但仍保持
BlackHole 输出，不会留下看似成功的静音归档：

```bash
BILIBILI_DRM_OPEN_BROWSER=true
BILIBILI_DRM_BROWSER_APP=Microsoft Edge
BILIBILI_DRM_BROWSER_BACKGROUND=false
BILIBILI_DRM_AUDIO_PROBE_MS=20000
BILIBILI_DRM_AUDIO_RMS_THRESHOLD=100
BILIBILI_DRM_REQUIRED_SOUND_MS=500
```

该控制器只调用系统 `open` 打开获授权的直播页，不导出或规避 DRM，也不会在结束时
关闭用户已有浏览器窗口。浏览器自动播放策略或登录过期仍可能阻止播放；非静音探针
负责 fail closed，下一场真实直播仍需完成一次端到端验收。

若浏览器已经能合法播放但系统音频设备不能可靠重绑定，可用
`scripts/browser_audio_receiver.py` 在 `127.0.0.1` 接收页面内 `MediaRecorder`
生成的 Opus/WebM 音频，并把其 `/stream` 地址配置为
`BILIBILI_DRM_AUDIO_URL`。该变量只接受 `127.0.0.1` 或 `::1` 的明文 HTTP 地址，
优先级高于 `BILIBILI_DRM_AUDIO_DEVICE`；页面端应同时使用 Web Audio 的无物理输出
sink，以确保不从扬声器播放。浏览器桥只处理播放器已经解码、用户有权观看的音频，
不获取、导出或规避 DRM 密钥。

需要在下播后自动停止浏览器桥和备用录制时，必须使用
`scripts/launch_browser_capture_watchdog.py` 创建一次性 LaunchAgent。不要用
`launchctl submit` 启动 watchdog；macOS 会把这类 submitted job 推断为 KeepAlive，
正常退出后也可能被持续重启。一次性 plist 明确设置 `KeepAlive=false`，且 terminal
`watchdog.json` 采用只写一次的原子回执，旧结果不会被后续误启动覆盖；看门狗
同样保持 BlackHole 输出，不恢复物理扬声器。

本机配置采用两级落盘：直播采集和会后处理先写入内置盘
`~/Library/Application Support/LiveSentinel/staging`；完成后复制到
`/Volumes/T5/Archives/zettaranc-live/sessions` 下的临时目录，
逐文件核对大小和 SHA-256，再在 T5 内原子发布为正式 Session，最后才删除 staging。
如果复制、校验或发布失败，唯一可用副本会留在 staging，并写出
`promotion_error.json`，不会把尚未安全落到 T5 的源目录删掉。启用 staging 后，
T5 不在直播采集的关键路径上：即使直播开始时未挂载或直播中途断开，采集与会后处理
仍在内置盘继续；系统只会在最终提升阶段检查 T5 的挂载点和剩余空间。

启用 staging 时，Session 在提升完成前保持 `DELIVERY_PENDING`。只有 T5 逐文件大小/
SHA-256 校验和原子发布成功后，才上传飞书并发送“直播归档完成”；提升失败则进入
`RETAINED_IN_STAGING` 并发送降级通知。可选异地备份在完成通知之后执行，不再延迟或
改变主归档完成语义。

离线故障注入 canary 可重复验证慢 Judge、通知断网、T5 不可用、静音输入和 FFmpeg
异常 EOF：

```bash
PYTHONPATH=. .venv/bin/python scripts/run_reliability_canary.py
```

当前本机生产配置的 Streaming ASR 是 `apple/speechtranscriber`：每个 VAD 语音段
交给本机 Swift 辅助程序处理，结果继续进入原有规则/LLM 判断链路。它不读取麦克风，
不使用 `DASHSCOPE_API_KEY`，数字和标点质量也不作为实时阶段的验收门槛；完整逐字稿
仍由会后的本地 SenseVoice 生成。转写失败只记录 `ASR_ERROR`，不会阻塞音频归档。

通用配置的兼容默认值仍保留 `dashscope/paraformer-realtime-v2`，便于非 macOS 部署。
若要切回 DashScope，将 `provider` 设为 `dashscope` 并提供 `DASHSCOPE_API_KEY`；若要
改为 OpenAI，则将 `provider` 设为 `openai`，并恢复 `sample_rate=24000`、
`model=gpt-live-transcribe` 和 `OPENAI_API_KEY`。

### 浏览器页去重与静音熔断

受保护房间仍只在确认开播并启动 Session 时初始化浏览器，普通定时发现不会打开
网页。Microsoft Edge 控制器会用房间长号和短号查找已有直播页：找到时复用并刷新，
找不到时才新建；如果系统拒绝浏览器自动化，则退化成普通 `open`，但 Watchlist 的
持久状态仍保证同一个 LIVE 周期只打开一次。

连续静音失败达到 `watchlist.capture_silence_failure_limit`（默认 3）后，服务写入
`CAPTURE_SILENCE_CIRCUIT_OPEN`，发送一次可用的摘要/文本通知，并按
`capture_silence_circuit_break_sec`（默认 3600 秒）冷却。冷却后允许重新初始化一次
浏览器；房间确认 OFFLINE 后会清空本场的浏览器和熔断状态。

### Apple Speech 生产实时转写与影子对照

Apple `SpeechTranscriber` 已用于本机生产实时语义转写。辅助程序只读取已有音频文件
或 VAD PCM，不申请麦克风权限；最终逐字稿和飞书交付仍使用离线 ASR 结果。辅助程序
应编译到长期稳定目录：

```bash
mkdir -p "$HOME/Library/Application Support/LiveSentinel/bin"
xcrun swiftc -parse-as-library scripts/apple_speech_transcriber.swift \
  -o "$HOME/Library/Application Support/LiveSentinel/bin/apple-speech-transcriber"
"$HOME/Library/Application Support/LiveSentinel/bin/apple-speech-transcriber" \
  --status --locale=zh-CN
```

同源音频对照命令：

```bash
PYTHONPATH=. .venv/bin/python scripts/compare_realtime_asr.py <audio-file> \
  --config live_sentinel/config.paraformer-funasr.example.json \
  --env-file .live-sentinel.env \
  --apple-command "$HOME/Library/Application Support/LiveSentinel/bin/apple-speech-transcriber" \
  --output-dir reports/asr-shadow/<run-id>
```

报告权限为 `0600`。字符一致度只表示两套模型彼此接近；没有人工标注逐字稿时，不应
把它表述为准确率。

用于“Paraformer + 本地 FunASR + DeepSeek Judge”的无密钥示例配置见
[`config.paraformer-funasr.example.json`](config.paraformer-funasr.example.json)；
其他云端 ASR 的比较与验证项记录在 [`TODO.md`](TODO.md)。

本地 FunASR 可以作为一次任务拥有的离线服务，不需要平时常驻：

```yaml
asr:
  offline:
    provider: funasr_local
    local:
      base_url: http://127.0.0.1:18000/v1
      health_url: http://127.0.0.1:18000/health
      model: sensevoice
      manage_process: true
      # lazy = 开始离线加工时启动；session = 整体任务开始时启动；
      # external = 只复用已经运行的服务
      start_mode: lazy
```

`session` 模式由 `SessionManager` 在任务开始时启动 `funasr-server`，在任务成功、失败或
中断的 `finally` 路径中关闭本进程自己创建的子进程；如果服务本来已由 supervisor 启动，
适配器只复用健康实例，不会误杀它。默认 `lazy` 更省内存，因为离线 ASR 只在 Final Audio
生成后才需要。

LLM Judge 只会在规则层达到候选阈值时调用，且要求返回固定 JSON 字段；DeepSeek V4
默认关闭 thinking，把有限输出预算留给 JSON 合约：
`topic`、`summary`、`score`、`is_blacklist_topic`、`is_high_information_density`。
服务不可用时记录 `LLM_ERROR` 并回退到规则分数，不阻塞 P0 音频归档。

## 接入真实服务的边界

实现一个 `AudioSource.read()` 适配器即可接入已授权的观众端 PCM；真实服务实现
位于 `live_sentinel/asr/`、`live_sentinel/analysis/llm_judge.py` 和
`live_sentinel/notification/notifier.py`。任何适配器故障都不应绕过或阻塞 P0 的
音频归档链路；密钥只通过环境变量传入。
