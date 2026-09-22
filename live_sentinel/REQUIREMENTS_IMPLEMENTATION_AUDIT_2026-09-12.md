# Live Sentinel 完整需求与实现审计

> 审计日期：2026-09-12（Asia/Shanghai）
> 审计对象：`/Users/huangxingyu/workspace/zettaranc-knowledge` 当前工作树
> 需求来源：原始概要设计、2026-09-09 至 2026-09-11 的用户指令与修正、当前代码/配置/运行产物/外部连通性证据
> 报告用途：交给独立模型复核；本文不是发布背书，也不授权审阅者修改代码或外部系统。

## Executive Summary

- **结论：CONDITIONAL / 尚不适合无看护长期运行。** 核心的录音、实时分析、离线转写、Watchlist、通知/文档/备份适配器已经形成一条可运行骨架，当前 45 个本地测试全部通过，后台发现服务也在运行；但真实目标直播间尚无完整端到端样本，实时 ASR 与 LLM Judge 当前缺少运行凭据，飞书群结束摘要未配置。
- **最强证据是归档主链路，不是生产可用性。** 已有普通直播的 5 分钟 Final Audio、实时/离线转写产物和可解码 FLAC；也有 ntfy 手机收件确认、飞书云文档创建、T5 跨卷提升和百度网盘假归档上传后删除的单项真实验证。它们不证明 4–8 小时直播、网络波动、进程重启、误报/漏报或一年保留策略已经通过。
- **当前最高风险是可恢复性与发布基线。** FFmpeg 输入结束会直接结束 Session，没有原设计要求的断线重连与 `AUDIO_GAP`；归档写入失败不会切新 Segment 继续；服务重启只恢复 Watchlist 状态，不恢复未完成 Session；全部核心实现仍未被 Git 跟踪，而 launchd 正直接运行这份可变工作树。
- **会后加工的“文件齐全”大于“语义能力完成”。** 逐字稿、SRT、摘要等文件会生成，但专有名词修正、真实章节划分、Highlight 边界精修、音频—文稿点击联动尚未实现或尚无真实质量证据。`keep_segments_after_finalize_days` 也没有执行逻辑，Working Segment 与 Final Audio 会重复占空间。

## 1. 审计口径

### 1.1 证据等级

| 等级 | 含义 |
| --- | --- |
| `REQUIREMENT` | 用户明确提出，或原始概要设计明确规定 |
| `CODE_EVIDENCE` | 当前源代码可直接证明存在相应控制流或数据结构 |
| `TEST_EVIDENCE` | 本轮实际执行的自动化测试或本地模拟通过 |
| `LIVE_EVIDENCE` | 本机真实第三方服务、设备或真实直播输入已完成一次受限验证 |
| `CONFIG_STATE` | 当前配置、凭据名称、后台进程或数据库状态的现场快照 |
| `INFERENCE` | 从有限样本推导的容量、风险或行为，必须保留不确定性 |
| `UNKNOWN` | 当前没有足够证据，不能当作已完成 |

### 1.2 状态定义

| 状态 | 含义 |
| --- | --- |
| `VERIFIED` | 有与需求匹配的代码/测试或真实验证，且没有已知关键缺口 |
| `IMPLEMENTED_UNVALIDATED` | 代码存在，但目标环境或真实外部链路尚未验证 |
| `PARTIAL` | 只完成需求的一部分，或实现语义弱于需求 |
| `GAP` | 尚未实现或当前配置导致需求不可用 |
| `SUPERSEDED` | 后续用户指令已明确替代 |
| `OUT_OF_SCOPE` | 用户明确暂不纳入当前阶段 |

### 1.3 本轮实际检查

已执行：

```text
.venv/bin/python -m compileall -q live_sentinel tests/live_sentinel
.venv/bin/python -m unittest discover -s tests/live_sentinel -t . -v
git diff --check
git status --porcelain=v2
launchctl print gui/<uid>/com.zettaranc.live-sentinel.watchlist
sentinel --config config.user.json --env-file .live-sentinel.env status
SQLite 只读查询：Watchlist、各历史 Session 表计数
ffprobe：历史 Final Audio 时长与大小
```

结果：45 个测试通过，编译检查通过，`git diff --check` 通过。自动化测试主要是单元/模拟证据，不能替代真实目标直播验证。

## 2. 完整需求版本史

这一节先保留需求演进，再在第 3 节给出最终权威口径。后续修正优先于早期说法。

| ID | 日期/阶段 | 用户需求或设计要求 | 最终解释 |
| --- | --- | --- | --- |
| H01 | 初始 | 阅读《B站直播智能监听与内容归档系统——概要设计》，简述目标与技术路线，然后开始实现 | 原始设计是功能与工程边界基线 |
| H02 | 初始验证 | 用普通 B 站直播 `5436512` 验证 5 分钟 | 仅为测试源，不能作为正式 Watchlist |
| H03 | 服务接入 | 接入真实 Streaming ASR、Offline ASR、LLM Judge、通知；推荐两类 ASR；通知用飞书；Judge 在 Codex/DeepSeek 中选择 | 最终默认：DashScope Paraformer 实时、本地 FunASR 离线、DeepSeek Judge；通知即时走 ntfy、结束摘要计划走飞书群 A |
| H04 | 本地资源 | 本地部署 FunASR，并评估硬盘、内存、CPU | 已部署独立环境并做 60 秒真实音频基准；见第 6 节 |
| H05 | 可理解性 | 画当前部署架构，说明已具备能力、实现方式、缺口和计划 | 已在对话中交付；本报告重述最终架构与缺口 |
| H06 | ASR 选择 | 比较 OpenAI 之外的平价 Streaming ASR 与本地方案后推荐 | Paraformer Realtime 成为当前基线；其他新模型进入 TODO，不应未经回归直接替换 |
| H07 | 生命周期 | Offline ASR 跟随整体任务启动，平时不常驻 | 当前配置为本地 FunASR `manage_process=true`, `start_mode=lazy`，会后首次调用时启动，Session 结束时停止 |
| H08 | Judge 解释 | 解释 LLM Judge 的作用 | 两级判定：规则先筛候选，LLM 只处理复杂语义并返回主题、摘要、评分、黑名单判断 |
| H09 | 飞书验证 | 模拟/验证飞书通知，利用本机已有登录状态 | 后续分化为机器人 Webhook 群通知与自建应用云文档两条能力；当前仅云文档真实验证成功，群 Webhook 未配置 |
| H10 | 完整离线文稿 | 用已完成的 `funasr_local` 跑通完整离线文稿 | 普通测试直播有离线结果；真实目标直播尚无样本 |
| H11 | 实时模型 | 接入 `paraformer-realtime-v2` | 已有适配器与模拟协议测试；当前部署缺 `DASHSCOPE_API_KEY`，运行时会降级为空 ASR |
| H12 | 模型演进 | 调研百炼其他新模型，作为后续替换候选 | 已记录 `TODO.md`；尚未做同音频对比回归 |
| H13 | DeepSeek | 使用用户提供的 DeepSeek API key | 曾做过短暂真实调用；当前部署不再含该 key。用户曾在对话中明文提供，必须轮换，本文不记录值 |
| H14 | 飞书群 A | 配置飞书 Webhook，并把结束摘要发到 A 群 | 群名保留；群 ID 与 Webhook 均不写入本报告。当前摘要 Webhook 环境变量缺失，需求未完成 |
| H15 | 第二测试源 | 使用普通直播 `620373` 做新的 5 分钟端到端验证 | 产生了可解码 5 分钟 FLAC、49 条实时转写、1 条最终转写；产物未保存 provider/model 快照，无法仅凭产物证明具体供应商 |
| H16 | 可审计性 | 说明处理流程和关键中间产物；为强模型编写可审计文档 | 旧 `AUDIT_REVIEW_PACKET.md` 已有早期版本；本报告覆盖之后所有需求与现场状态 |
| H17 | 通知比较 | 评估飞书之外更合适的通知方式，并新增 ntfy | 已实现 ntfy；用户确认手机收到真实测试消息 |
| H18 | 更优使用方式 | 不希望每次在对话框粘 URL；从需求端推荐更优雅方式 | 最终采用持久化 Watchlist + launchd 后台发现服务 |
| H19 | MarkItDown | 研究 MarkItDown 对项目的帮助 | 结论为异步外部资料转 Markdown 的可选增强；后续用户明确当前不关注，状态为 `OUT_OF_SCOPE` |
| H20 | 使用习惯 | 只关注一个 zettaranc 直播源，通常周三/周日约 20:00 开始，持续到次日 01:00–02:00 | Watchlist 只启用一个正式源；发现时段到 24:00，开播后 Session 可持续到次日，单场最大 12 小时 |
| H21 | 高价值通知 | 高价值内容用飞书或 ntfy 提醒；频率不高于约 10 分钟一次 | 当前即时路由为 ntfy，状态机全局冷却 600 秒；不是按主题分别冷却 |
| H22 | 结束摘要 | 直播结束摘要发送到飞书 A 群 | 代码存在，当前环境缺摘要 Webhook，未完成 |
| H23 | 文稿保存 | 完整文稿与摘要进入飞书云文档或推荐位置 | 飞书自建应用与上传器已就绪，并成功创建最小测试文档；尚无正式直播上传证据 |
| H24 | 音频保留 | 全量音频优先 T5，百度网盘异地备份；评估飞书云盘；至少覆盖一年增量 | 当前选择为本地 staging → T5 主档 → 百度备份；原始音频不上飞书。容量推算基本满足一年，但没有保留/清理守护机制 |
| H25 | 云端兼容格式 | 若进网盘，考虑更兼容、更高效的格式 | 当前主档保留无损 FLAC；尚未自动生成 Opus/AAC 试听副本，网盘各终端播放兼容性也未形成回归证据 |
| H26 | 手机连调 | 手机安装 ntfy 后做真实联调 | `LIVE_EVIDENCE`：用户确认收到；topic 视同发布凭据，本文不记录 |
| H27 | 可调规则 | 高价值判断引擎逻辑应易于修改 | 关键词、结构标记、权重、阈值、语义混合均在配置中；仍缺标注集与调参反馈闭环 |
| H28 | 误解与回退 | 早期“无需实时转写的外部音频检测旁路”是误解，应撤回 | `SUPERSEDED`：不实现外部检测旁路 |
| H29 | 正确暂停语义 | 通过配置暂停实时转写、价值检测和即时通知，同时保留录音、离线转写、文档和备份 | 已实现 `asr.realtime.enabled=false` 整条实时智能链路开关，默认开启 |
| H30 | Watchlist 实现 | 实现自动调度服务，并解释 Watchlist | 已实现持久化源、发现、同场去重、重试、CLI、launchd |
| H31 | 正式身份纠正 | 正式关注源不是“天降呦呦”，而是 zettaranc：UID `326246517`、固定短房间号 `1616`，规范房间号 `11163068` | 当前 Watchlist 中错误源已禁用，正确源唯一启用；后续任何测试源都不得被当作正式源 |
| H32 | 飞书权限 | 创建 `Live Sentinel` 自建应用，只授予最小云文档写权限 | 已创建/发布，只有新版文档创建与只写权限；没有通讯录/消息/读取权限 |
| H33 | 本地 staging | 先写本地磁盘，直播结束及后处理完成后再移入 T5 | 已实现跨文件系统 copy → 大小/SHA-256 校验 → T5 内原子发布 → 删除 staging |
| H34 | 百度假归档 | 上传一份虚假直播归档测试，然后删除 | 已真实完成 upload/list/delete；仅证明当时授权与基本命令，不证明长期自动备份和远端完整性 |
| H35 | 实时默认 | 实时提醒链路默认开启，但可配置关闭 | 当前 `config.user.json` 为 `enabled=true`；因缺实时 ASR/Judge key，逻辑开启但供应商不可用 |
| H36 | 轮询早期版 | 未开播 30 分钟一次；周三/周日 19:30–24:00 每 5 分钟 | 被 H37 替代 |
| H37 | 最终轮询 | 每天 00:00–19:00 每 3 小时；每天 19:00–24:00 每 1 小时；周三/周日 19:00–24:00 覆盖为每 5 分钟；不可跨边界错过高频时段 | 当前配置与测试匹配；直播已发现后每 5 分钟复查状态 |
| H38 | 当前任务 | 汇总完整需求，生成需求+实现审计报告，并创建 `gpt-5.6-sol / xhigh` 新任务独立审阅 | 本报告即前两项交付物之一；新任务必须读取本报告并独立复核 |

## 3. 最终权威需求基线

### 3.1 用户体验与运行边界

1. 系统长期关注 **唯一正式源 zettaranc**，以 UID `326246517`、短房间号 `1616`、规范房间号 `11163068` 作为身份约束。
2. 不需要每次粘贴 URL；后台服务自动发现开播并启动单场 Session。
3. 最终轮询规则以 H37 为准，并按 `Asia/Shanghai` 处理边界。
4. 一场直播同时最多启动一个 Session；完成后必须先观察到 OFFLINE，下一次 LIVE 才能再次启动。
5. 实时智能链路默认开启；关闭时只停实时 ASR、价值检测、LLM Judge 和即时通知，不得停止录音和会后加工。
6. 当前不做 MarkItDown、llm-wiki 深度联动、RAG、视频录制、自动发布或完整 Web 后台。

### 3.2 P0 完整录音

1. 原始/完整直播音频是 Source of Truth，优先级高于实时转写和模型输出。
2. Archive 与 Realtime 必须隔离；实时拥塞允许降级，不得反压导致录音丢失。
3. 48 kHz stereo FLAC 为长期主档；内部以 1–2 小时 Working Segment 容错，用户最终看到一场一个 Final Audio。
4. 所有音频、转写、Highlight、通知、章节统一使用 Session 相对时间轴。
5. Final Audio 需要覆盖检查、解码测试、时长校验和 SHA-256。
6. 输入断线、归档写入错误、整体进程异常应尽量重连/切新段/恢复，并明确记录空洞；不能把不完整录音伪装成完整。

### 3.3 实时识别与价值判断

1. 实时音频降为 16 kHz mono，经 VAD 后送 `paraformer-realtime-v2`。
2. 维护 30 秒、3 分钟、5 分钟、15 分钟语义窗口，按约 60 秒周期分析。
3. 价值评分至少考虑白名单、黑名单、信息密度、表达结构、主题连续性、新颖性和语音占比。
4. 先规则筛选候选，再调用 LLM Judge 做复杂语义判断；LLM 不持续吞完整直播。
5. 使用 IDLE → CANDIDATE → HOT → COOLING 状态机；稳定进入 HOT 才通知并打开 Highlight。
6. 即时通知当前默认走 ntfy；同一 Session 全局冷却约 10 分钟，避免轰炸。
7. 规则与阈值必须能通过配置修改，并保留“为何触发”的特征证据。

### 3.4 会后处理

1. 本地 FunASR 只随 Session 生命周期启动，平时不常驻。
2. 从 Final Audio 生成带时间轴的忠实逐字稿、阅读稿、SRT、章节、重点区间和整场摘要。
3. 阅读稿不得添加主播未说过的观点；摘要/模型推断不能混入原始逐字稿。
4. 需要支持专有名词修正、章节划分和 Highlight 边界修正。
5. 文稿、章节、Highlight 最终应能定位到 Final Audio 的准确时间点。

### 3.5 交付、存储与保留

1. 直播采集和会后处理先在内置盘 staging 完成。
2. 成功后复制到 `/Volumes/T5/Archives/zettaranc-live/sessions`，逐文件检查大小与 SHA-256，在 T5 内原子发布，确认后再删 staging。
3. T5 是正式主档；百度网盘是异地备份，失败不得删除主档，并应具备可重试、可核验状态。
4. 飞书云文档保存摘要和完整文稿；结束时向飞书 A 群发送一条摘要与文档入口。
5. 存储容量及保留策略至少支撑一年；长期主档保持 FLAC，可选生成 Opus/AAC 试听副本。
6. 凭据仅放入权限为 `0600` 的 gitignored 环境文件，不进入代码、报告、日志或 Session 元数据；对话中曾暴露的 key 必须轮换。

## 4. 当前部署架构与真实数据流

```text
launchd 用户服务（当前 running）
        │
        ▼
Watchlist SQLite
  ├─ disabled: 测试源 5436512 / 天降呦呦
  └─ enabled : zettaranc 1616 → 11163068 / UID 326246517
        │ 按最终三档轮询发现 LIVE
        ▼
Bilibili room/play URL resolver
        │
        ▼
FFmpeg HTTP-FLV → 48 kHz stereo PCM
        │
        ▼
AudioTee
  ├─ P0 blocking archive queue
  │      └─ 内置盘 staging / Working FLAC segments
  │              └─ Finalizer: timeline/decode/duration/SHA-256
  │                      └─ Final Audio
  │
  └─ P1 lossy realtime queue（仅 enabled=true）
         └─ downmix/resample → EnergyVAD
                 └─ DashScope Paraformer（当前缺 key，实际为空适配器）
                         └─ rolling transcript
                                 └─ rules → DeepSeek Judge（当前缺 key）
                                         └─ state machine → ntfy

Final Audio
  └─ lazy start local FunASR / SenseVoice
         └─ verbatim/readable/SRT/placeholder chapters/highlights/summary
                ├─ 飞书云文档（自建应用凭据当前存在）
                ├─ 百度 bypy（当前已授权，代码实际在提升到 T5 前执行）
                └─ 飞书群 A 结束摘要（当前缺 Webhook）

Session Manager 返回 COMPLETED
  └─ staging → T5 incoming
         ├─ 文件清单/大小/SHA-256 校验
         ├─ 元数据路径 rebase + SQLite integrity_check
         ├─ T5 内原子 rename 为正式 Session
         └─ 删除 staging
```

关键说明：当前代码的百度备份、飞书文档和结束摘要都在 `SessionManager.run()` 内执行，**发生在 `runner.py` 的 T5 promotion 之前**。因此文档中“上传已经落到 T5 的 Session”的表述与真实调用顺序不一致。

## 5. 需求—实现追踪矩阵

### 5.1 发现与生命周期

| ID | 要求 | 证据 | 状态 | 审计结论 |
| --- | --- | --- | --- | --- |
| W01 | 唯一正确关注源 | Watchlist DB：错误测试源 disabled；zettaranc `1616→11163068` enabled，UID 匹配 | `CONFIG_STATE` / `VERIFIED` | 当前身份配置正确 |
| W02 | 三档轮询和边界感知 | `watchlist/supervisor.py` + `test_offline_polling_switches_at_focus_boundaries` | `CODE_EVIDENCE` + `TEST_EVIDENCE` / `VERIFIED` | 与最终规则匹配 |
| W03 | 同一直播 epoch 只启动一次 | store claim 条件、`session_started_for_live`、对应测试 | `CODE_EVIDENCE` + `TEST_EVIDENCE` / `VERIFIED` | 已覆盖基本状态机 |
| W04 | 发现错误指数退避 | Supervisor/store + 测试 | `VERIFIED` | 仅发现与 Session 失败退避，不含 T5 promotion 自动重试 |
| W05 | 后台常驻 | launchd state=`running`，PID 存活，未退出 | `CONFIG_STATE` / `VERIFIED` | 只是现场存活，不是长期稳定性证明 |
| W06 | 服务重启可继续 | 将孤儿 run 标为 INTERRUPTED，允许仍在线房间新建 Session | `PARTIAL` | 不恢复原 Session/原时间轴/未完成分片，只是重新开一场 |
| W07 | 跨午夜长场 | 单场最大 720 分钟，开播后 runner 不受发现时段限制 | `CODE_EVIDENCE` / `IMPLEMENTED_UNVALIDATED` | 尚无跨午夜真实验证 |

### 5.2 P0 录音与完整性

| ID | 要求 | 证据 | 状态 | 审计结论 |
| --- | --- | --- | --- | --- |
| A01 | 归档优先、实时可丢 | `AudioTee` archive 队列阻塞、realtime 队列满时丢帧；测试通过 | `VERIFIED` | 进程内基本隔离成立 |
| A02 | 48 kHz stereo FLAC | 默认/用户配置与历史 Final Audio | `VERIFIED` | 历史 5 分钟 FLAC 可解码 |
| A03 | 1–2 小时 Working Segment | `segment_minutes=120` | `IMPLEMENTED_UNVALIDATED` | 没有 2 小时以上真实样本验证分段切换 |
| A04 | 一场一个 Final Audio | Finalizer 与历史产物 | `VERIFIED` | 多段无损 concat 只有本地测试，长场未验证 |
| A05 | Final Audio 校验 | timeline、gap、decode、duration、SHA-256 代码与测试 | `VERIFIED` | 对已知帧时间轴有效；真实 CDN 断线语义不足 |
| A06 | 输入断线重连并记录 GAP | `FFmpegAudioSource.read()` EOF 直接返回 `None` | `GAP` | 断线会被当作正常结束，未重连、未记录 `AUDIO_GAP` |
| A07 | Archive Writer 失败切新段继续 | 写入异常直接抛 `ArchiveError`，外层将 Session 置 FAILED | `GAP` | 与原设计明确要求不符，威胁长场完整性 |
| A08 | 进程异常后恢复当前 Session | 只有 checkpoint 写入与 Watchlist 孤儿状态恢复 | `PARTIAL` | checkpoint 目前主要用于诊断，不能续写/修复/合并原 Session |
| A09 | Working Segment 延迟清理 | 配置有 `keep_segments_after_finalize_days=1`，无任何消费逻辑 | `GAP` | 现有样本 Working 与 Final 各占一份，长期容量近似翻倍 |
| A10 | 归档不足时按 P4→P0 降级 | 实时队列可丢；其他磁盘/内存降级策略有限 | `PARTIAL` | staging 空间不足会拒绝启动，没有容量预测/自动清理告警 |

### 5.3 实时 ASR、判断与通知

| ID | 要求 | 证据 | 状态 | 审计结论 |
| --- | --- | --- | --- | --- |
| R01 | Paraformer Realtime 适配器 | WebSocket task 协议实现与模拟测试 | `TEST_EVIDENCE` / `IMPLEMENTED_UNVALIDATED` | 当前 env 缺 key，正式 Watchlist 运行时会用 Null ASR |
| R02 | 16 kHz mono + VAD | preprocessor、EnergyVAD、测试 | `VERIFIED` | EnergyVAD 是基线算法，真实噪声/音乐/方言效果未知 |
| R03 | 多窗口 Transcript Buffer | Buffer 与配置 | `PARTIAL` | 有 15 分钟上下文和按秒查询，但业务分析主要使用 LLM window；四个窗口并未分别驱动独立检测逻辑 |
| R04 | 可解释规则评分 | analyzer/scorer；特征进入 analysis JSONL/SQLite | `VERIFIED` | 缺真实标注集标定 |
| R05 | 复杂黑名单按主题判断 | 规则层仍是词项命中，LLM 只在候选或命中黑名单时调用 | `PARTIAL` | 当前缺 LLM key 时退化为简单词项，可能误伤“提到但否定”的句子 |
| R06 | LLM Judge | DeepSeek/OpenAI compatible 适配器与模拟测试 | `IMPLEMENTED_UNVALIDATED` | 当前 key 缺失；模型合同、成本、超时和质量未做持续验证 |
| R07 | HOT 状态机与 Highlight | 状态机/测试；replay 样本产生 1 条 Highlight | `VERIFIED`（机制） | 真实目标召回/精度未知 |
| R08 | 10 分钟防轰炸 | `notification_cooldown_sec=600`，全 Session 单一时间戳 | `VERIFIED` | 是全局而非按主题/严重度冷却；用户要求可接受，但扩展性有限 |
| R09 | ntfy 手机提醒 | 适配器模拟测试 + 用户确认真机收到 | `LIVE_EVIDENCE` / `VERIFIED` | topic 视同凭据；当前未设 token，安全性取决于不可猜测 URL |
| R10 | 实时链路可整体暂停 | `asr.realtime.enabled=false` 阻止 worker/Judge/notifier，测试确认归档和 Offline ASR 保留 | `VERIFIED` | 正确回退了误解的外部检测旁路 |
| R11 | 默认开启 | `config.user.json enabled=true` | `CONFIG_STATE` / `PARTIAL` | 逻辑开启，但缺实时 ASR/Judge key，不等于生产可用 |

### 5.4 离线加工与产物

| ID | 要求 | 证据 | 状态 | 审计结论 |
| --- | --- | --- | --- | --- |
| P01 | FunASR 跟随任务生命周期 | `manage_process=true`, `start_mode=lazy`；启动/停止测试 | `VERIFIED`（机制） | 正式目标长场未验证 |
| P02 | 忠实逐字稿 | OfflineFinalizer 结构化 JSONL/Markdown；历史普通直播有 1 条最终转写 | `PARTIAL` | 当前 SenseVoice 返回粒度和时间戳质量需人工抽样；单条覆盖 5 分钟不等于高质量逐句稿 |
| P03 | 阅读版 | 当前基本复用 transcript text | `PARTIAL` | 未实现明确的有限口语整理流程与差异审计 |
| P04 | SRT | formatter 生成 SRT | `IMPLEMENTED_UNVALIDATED` | 未用播放器验证长场时间轴、切句与可读性 |
| P05 | 专有名词词典/修正 | 原设计有 global/up/session dictionary；代码无对应模块 | `GAP` | 对 zettaranc 专有术语质量是实际风险 |
| P06 | Chaptering | 仅按 Highlight 派生章节 | `PARTIAL` | 无 Highlight 时章节为空，不是真正的全场主题分章 |
| P07 | Highlight 边界修正 | 复用实时状态机范围 | `GAP` | 没有基于 Final Audio/Offline ASR 的二次边界校准 |
| P08 | 整体摘要 | 目前 summary 主要是 Session 元信息、来源和 Highlight 数 | `PARTIAL` | 不是对整场内容的语义总结 |
| P09 | 音频—文稿联动 | JSONL/SRT 有时间字段 | `PARTIAL` | 数据前提存在，但没有播放器、seek 链接或回看 UI |
| P10 | provider/model/config/代码版本可追溯 | Session 元数据未保存这些快照 | `GAP` | 现有 49 条实时转写无法仅凭产物证明由哪个 provider/model 生成 |

### 5.5 飞书、T5、百度与一年保留

| ID | 要求 | 证据 | 状态 | 审计结论 |
| --- | --- | --- | --- | --- |
| D01 | 飞书自建应用最小权限 | 应用仅有 docx create/write-only；真实最小文档创建成功 | `LIVE_EVIDENCE` / `VERIFIED` | 不具备读权限，符合最小权限目标 |
| D02 | 正式 Session 上传飞书文档 | uploader 代码与模拟测试 | `IMPLEMENTED_UNVALIDATED` | 还没有 zettaranc 正式直播上传；部分失败会残留已创建的空/半成品文档，无幂等重试 |
| D03 | 结束摘要发 A 群 | 代码存在独立 summary notifier | `GAP` | 当前缺 `FEISHU_SUMMARY_WEBHOOK_URL`；自建应用文档权限不能代替群机器人 Webhook |
| D04 | 内置盘 staging → T5 安全提升 | 跨卷复制、大小/SHA-256、路径 rebase、SQLite integrity、T5 内原子发布；真实 smoke 后清理 | `LIVE_EVIDENCE` / `VERIFIED`（单次） | promotion 失败会留 staging，但后台没有自动重试队列 |
| D05 | 百度异地备份 | bypy 可用；假归档真实 upload/list/delete | `LIVE_EVIDENCE` / `PARTIAL` | 自动代码只依据退出码，不做远端清单/哈希验证；`required=false`，失败仍 COMPLETED |
| D06 | 备份顺序以 T5 主档为源 | backup 在 SessionManager 内、promotion 在 manager 返回后 | `CODE_EVIDENCE` / `GAP` | 实际从 staging 上传，与代码注释/文档“已落 T5 后上传”不一致 |
| D07 | 一年容量 | T5 约 416 GiB 可用；历史 FLAC 为 17.7–23.2 MB/5min | `INFERENCE` / `PARTIAL` | 按每周 2 场×每场 6h，单份 Final FLAC 约 132–174 GiB/年；若 Working 永不清理则约 264–348 GiB/年，余量显著缩小 |
| D08 | 百度一年容量 | 当时账户约 3.09 TB 剩余 | `LIVE_EVIDENCE`（时点） / `PARTIAL` | 容量名义充足，但配额会变化且自动备份完整性未验证 |
| D09 | FLAC 云端兼容/试听格式 | 主档 FLAC | `PARTIAL` | 未生成 Opus/AAC 试听副本；百度各端播放兼容性不应只依据一次上传推断 |
| D10 | promotion/backup/文档可重试 | 发现/Session 有退避 | `GAP` | 会后交付阶段没有持久化任务队列和幂等键，失败需人工处理 |

### 5.6 安全、发布与可维护性

| ID | 要求 | 证据 | 状态 | 审计结论 |
| --- | --- | --- | --- | --- |
| S01 | 凭据不进 Git | `.live-sentinel.env` 与 `config.user.json` 均被 ignore；env mode `0600` | `CONFIG_STATE` / `VERIFIED` | 当前文件层面合格 |
| S02 | 对话中暴露 key 的处置 | 用户曾在聊天中粘贴多个 key/OAuth code | `REQUIREMENT` / `GAP` | 无证据证明所有长期 key 已轮换；应立即在服务商侧撤销/重建 |
| S03 | 日志/产物不泄密 | 代码一般不打印 key；外部错误正文可能进入 event/detail | `PARTIAL` | 第三方返回若回显敏感信息，当前没有统一脱敏器 |
| S04 | 可复现发布基线 | Git `HEAD=4512a34`，核心目录/测试/pyproject 全部未跟踪 | `CONFIG_STATE` / `GAP` | 无法从 Git 可靠回滚/复现当前实现 |
| S05 | 后台服务使用稳定版本 | launchd 工作目录与解释器直接指向当前 checkout | `CONFIG_STATE` / `GAP` | 编辑未跟踪代码即可改变生产服务，且没有 release/版本锁定 |
| S06 | 配置与文档一致 | README、示例配置、用户配置存在部分不同默认值/顺序描述 | `PARTIAL` | 需要配置 schema 测试与“实际适配器状态”启动审计输出 |
| S07 | SQLite 演进 | Watchlist 通过按列 ALTER 做轻量迁移；Session DB 无显式 schema version | `PARTIAL` | 后续升级/回滚审计困难 |

## 6. 已验证事实与不可扩大解释的证据

### 6.1 普通直播 Session 证据

`sessions/20260909_170317_room620373/`：

- `session.json` 标记 `COMPLETED`，时长 300 秒；
- Final Audio 可完整解码，时长 300 秒，大小约 17.7 MB；
- SQLite 有 49 条 `realtime_transcripts`、1 条 `final_transcripts`、5 条 events；
- 0 条 highlights、0 条 chapters；
- `final/session.json` 标记 `transcript_source=offline_asr`。

它证明普通直播输入曾完成“抓流—实时文本—Final Audio—离线文本”的一次短闭环。它**不能**单独证明：

- 这 49 条文本一定来自当前配置的 DashScope 模型；
- 目标 zettaranc 房间可正常解析和录制；
- 4–8 小时稳定性、断线恢复和跨午夜处理；
- 高价值判断准确率、10 分钟限流和通知送达；
- 飞书文档、飞书群摘要、T5 promotion、百度备份在同一真实 Session 中都成功。

### 6.2 Replay 证据

`sessions/replay_20260909_164511_5m_liveasr/` 有 46 条实时转写和 1 条 Highlight，并发生 ENTER_CANDIDATE → ENTER_HOT → LEAVE_HOT。它证明状态机在一个回放样本中可触发，不证明真实直播的召回率或低误报。

### 6.3 FunASR 资源基准

在 Apple M4、10 核 CPU、16 GB 内存的本机，用 60 秒真实直播音频测得：

- SenseVoice 模型缓存约 901 MB；
- 常驻服务空闲 RSS 约 1.7–1.8 GiB，请求时峰值约 1.9 GiB；
- 60 秒音频推理约 1.6 秒，RTF 约 0.027；
- Paraformer 单模型 60 秒推理约 1.9 秒，冷启动峰值约 2.9 GiB；
- 当前生产配置最终选择 `sensevoice` + CPU + lazy 生命周期。

这是单机、单并发、60 秒样本的时点结果；不能外推到 6 小时切块、并发任务、内存碎片和长期稳定性。

### 6.4 外部服务单项证据

- ntfy：用户确认手机收到真实测试消息；
- 飞书：`Live Sentinel` 自建应用创建并发布，以最小权限成功创建一篇无直播内容的验证文档；
- 百度网盘：bypy 授权可用，假归档真实上传、列出并删除；
- T5：真实跨卷 copy/校验/publish smoke 成功，测试目录之后删除；
- DeepSeek：早期短 smoke 曾成功，但当前环境不再提供 key；
- DashScope：历史运行被操作过程认为成功，但 Session 元数据缺 provider/model，当前也缺 key，故本报告不把它标为可复现的当前生产证据。

## 7. 当前现场状态（2026-09-12）

### 7.1 Watchlist 与服务

| 项目 | 当前值 |
| --- | --- |
| launchd | loaded / running，PID 22924，last exit = never exited |
| 测试源 | `5436512`，name=天降呦呦，disabled |
| 正式源 | `1616→11163068`，name=zettaranc，UID=`326246517`，enabled |
| 正式源观测 | OFFLINE，无 active session |
| 正式源真实 Session | 无 |

### 7.2 外部适配器构造结果

```text
realtime = dashscope/paraformer-realtime-v2 (missing key)
offline  = funasr_local/sensevoice (lazy)
judge    = deepseek/deepseek-v4-flash (missing key)
notify   = ntfy
summary  = feishu_summary (missing URL)
docs     = feishu_docs
backup   = baidu_bypy
```

这意味着：即使配置写着实时链路 `enabled=true`，当前真正会运行的是录音、离线 FunASR、ntfy 适配器、飞书文档和百度适配器；没有实时文字时，规则/Judge/HOT 通知也不会形成有效输入。

### 7.3 磁盘与保留估算

| 位置 | 现场可用空间 | 用途 |
| --- | ---: | --- |
| 内置盘 | 约 69 GiB | staging；足以容纳若干长场，但不是一年主档 |
| T5 | 约 416 GiB | 正式主档 |
| 百度网盘 | 约 3.09 TB（早期时点） | 异地备份 |

估算假设：历史 FLAC 约 3.54–4.65 MB/分钟，全年 104 场、每场 6 小时。

- Final Audio 单份：约 132–174 GiB/年；
- Working Segment 永久保留且与 Final Audio 内容重复：约 264–348 GiB/年；
- 再考虑 SQLite、文稿、临时文件和编码波动，当前 T5 只有在落实 Working 清理后才有较稳妥的一年余量。

这只是容量场景，不是精确预测。直播时长、音频内容复杂度和 FLAC 压缩率会改变结果。

### 7.4 Git 状态

- 当前分支 `main`，`HEAD=4512a34`；
- `.gitignore`、根 README 等有未提交修改；
- `live_sentinel/`、`tests/`、`scripts/`、`pyproject.toml`、概要设计文档均为未跟踪；
- `.obsidian` 还有与本任务无关的用户修改，任何后续提交都必须排除或单独处理。

## 8. Findings（按严重度）

### [P0] 输入断线会被当作正常 Session 结束，无法保证长场完整录音

- evidence: `CODE_EVIDENCE`
- location: `live_sentinel/audio/source.py:156-161`, `live_sentinel/session/manager.py:429-432`
- reproduction: 在录制过程中让上游 FLV/CDN 断开；FFmpeg stdout EOF 后 `read()` 返回 `None`，主循环退出并走正常 Finalize/COMPLETED。
- impact: 一场 6 小时直播可能只保留前半段，却没有明确 `AUDIO_GAP` 或“不完整”状态。
- required fix: 区分“主播正常下播”“瞬时输入失败”“本地 stop”；增加有界重连、重新解析播放 URL、Session 时间轴 gap 事件与完整性标志；只有确认下播或达到停止条件才正常结束。

### [P0] 归档写入故障没有切新 Segment 恢复

- evidence: `CODE_EVIDENCE`
- location: `live_sentinel/audio/archive.py:171-178`, `live_sentinel/session/manager.py:578-586`
- impact: 一次 BrokenPipe/OSError 会终止整场；与原设计“重新打开并记录异常，继续当前 Session”冲突。
- required fix: 封装 writer recovery policy；关闭/隔离损坏段，记录确切 gap，打开新段继续；设置最大连续失败次数并显式将最终完整性降级。

### [P1] 整个实现没有 Git 发布基线，而后台服务直接运行当前工作树

- evidence: `CONFIG_STATE`
- impact: 无法可靠复现、审阅、回滚或证明 launchd 在运行哪个版本；误编辑文件即可改变长期服务。
- required fix: 在排除 `.obsidian` 等无关修改后建立最小可审阅提交，记录版本号/commit；launchd 指向固定 release/venv，并在 Session 元数据记录代码版本。

### [P1] 当前实时 ASR 与 Judge 实际不可用，但配置表面显示开启

- evidence: `CONFIG_STATE`
- impact: 用户可能误以为会收到高价值提醒，实际 Null ASR 不会产生文本，也不会进入 HOT。
- required fix: Watchlist 生产启动默认执行 capability preflight；若 `enabled=true` 且缺 key，应发送一次明确的部署告警并进入 `DEGRADED`，而不是安静降级；补充目标直播 smoke 后再解除告警。

### [P1] 正式目标 zettaranc 尚无真实端到端 Session

- evidence: Watchlist DB 与 T5/staging 均无正式源 Session
- impact: 房间解析、长时音频、术语识别、价值规则、ntfy、会后文档、T5、百度在真实目标上的组合行为均未知。
- required fix: 下一次直播做受控全链路观测，保存 provider/config/version 快照和人工抽样验收；首场不应完全无人值守。

### [P1] 百度备份顺序与主档语义不一致，且无远端完整性验证/自动重试

- evidence: `CODE_EVIDENCE`
- location: `session/manager.py:575-576`, `runner.py:183-206`, `backup/baidu.py:61-75`
- impact: 实际备份 staging 而非已发布 T5 主档；bypy 退出码 0 不能证明远端逐文件完整；失败仍将 Session 标为 COMPLETED。
- required fix: 将交付阶段拆成持久化状态机：`T5_PROMOTED → BAIDU_UPLOADED → BAIDU_VERIFIED → SUMMARY_SENT`；每步具备幂等键、重试次数、最后错误和人工补偿命令。

### [P1] 会后加工与原始设计的“高质量”目标仍有明显差距

- evidence: `CODE_EVIDENCE` + `LIVE_EVIDENCE`
- impact: 单条 5 分钟最终转写、空章节、无术语修正、无 Highlight 边界二次校准，无法满足高质量回看和引用。
- required fix: 先建立一段 zettaranc 人工校对黄金样本；实现句级切分/时间戳、术语词典、全场主题章节与 final-pass highlight refinement；分别报告 WER/术语召回/时间偏移，而不是只检查文件存在。

### [P1] Working Segment 保留配置没有执行，危及一年容量余量

- evidence: `CODE_EVIDENCE` + 现有文件大小
- impact: Final 与 Working 重复占用；按当前样本，一年可能达到约 264–348 GiB，T5 余量变薄。
- required fix: Final/T5/百度验证状态达到策略阈值后再清理 Working；先 dry-run、保留删除清单和审计事件；磁盘不足时提前告警，不做静默删除。

### [P1] 飞书群 A 结束摘要未配置

- evidence: `CONFIG_STATE`
- impact: 直播完成后用户不会在指定群收到总结，即使飞书云文档创建成功。
- required fix: 为群 A 创建/配置独立自定义机器人 Webhook，写入本地 env；发送不含敏感内容的测试摘要并由用户确认收件。

### [P2] 飞书文档与 Session 产物缺少幂等性和部分失败治理

- evidence: `CODE_EVIDENCE`
- impact: token/网络/Block 批次失败可能留下重复或半成品文档；重跑会创建新文档。
- required fix: 用 Session ID + 文档类型做幂等登记，保存 upload state、document id、完成 block count；支持断点续传或显式新版本。

### [P2] Session 元数据不足以支持可复现审计

- evidence: 历史 `session.json` / SQLite schema
- impact: 无法回答“用了哪个模型/配置/代码/词表/通知路由”，也无法复现实验或解释结果变化。
- required fix: 保存脱敏配置快照、provider/model、模型版本、代码 commit、源 canonical ID、实时丢帧计数、重连/gap、外部交付状态；绝不保存 key 或完整发布 URL。

### [P2] 当前全局通知冷却无法表达不同主题/严重度

- evidence: `interest/state_machine.py:44-46`
- impact: 一个低价值 HOT 通知可能压制 10 分钟内另一个更重要主题；同时服务重启后冷却状态丢失。
- required fix: 短期保持用户要求的 10 分钟上限；后续按 `(source, topic/severity)` 持久化冷却，并允许更高分数升级通知。

### [P2] ntfy 公共 topic 的安全边界较弱

- evidence: 当前 notifier 有 URL，无 token
- impact: topic URL 即发布凭据；可猜测或泄露会导致垃圾消息或内容暴露。
- required fix: 使用高熵 topic，定期轮换；若内容敏感，使用自托管/认证 token，并限制通知正文为摘要与时间点，不发送大段逐字稿。

### [P2] 数据库缺显式 schema version 和统一迁移策略

- evidence: Watchlist 只按缺列 ALTER；Session SQLite 固定建表
- impact: 后续字段/约束升级难以回滚和审计。
- required fix: 引入 schema version + 向前迁移测试；启动前备份小型状态 DB；Session DB 保持不可变或声明版本。

## 9. 发布前验收门槛

### Gate 0：建立可审阅基线

- [ ] 把 Live Sentinel 实现、测试、概要设计和本报告纳入一个独立、可回滚的 Git 提交；
- [ ] 明确排除 `.obsidian` 等无关用户修改；
- [ ] Session 元数据写入 commit/config/provider/model 快照。

### Gate 1：恢复与完整性

- [ ] 模拟 CDN/网络中断，证明有界重连和 `AUDIO_GAP`；
- [ ] 模拟 ffmpeg/FLAC writer BrokenPipe，证明能切段继续或明确 fail-closed；
- [ ] 强制终止服务，证明未完成分片可诊断、修复或安全续录；
- [ ] 2 小时以上合成/真实授权流验证 Segment 切换与最终 concat；
- [ ] 最终完整性摘要明确 `complete / complete_with_gaps / failed`。

### Gate 2：当前生产能力预检

- [ ] 轮换所有曾在聊天中明文暴露的长期 key；
- [ ] 恢复 DashScope/DeepSeek key，但只存本地安全 env；
- [ ] `enabled=true` + 缺凭据时不再静默；
- [ ] 飞书 A 群摘要 Webhook 真机/真群确认；
- [ ] ntfy topic 安全性复核。

### Gate 3：正式目标首场直播

- [ ] 仅 zettaranc 正式源启动；
- [ ] 至少一次 4–8 小时或完整跨午夜 Session；
- [ ] 实时转写、规则/Judge、通知、Final Audio、Offline ASR、飞书文档、T5、百度全部形成同一个 Session 的审计链；
- [ ] 人工抽查至少 30 分钟：术语、断句、时间偏移、HOT 误报/漏报；
- [ ] 验证“关闭实时链路”模式仍完整产出会后结果。

### Gate 4：一年保留与补偿流程

- [ ] Working Segment 按验证状态安全清理；
- [ ] staging/T5 空间阈值与提前告警；
- [ ] T5 promotion 失败自动重试或可见待办；
- [ ] 百度上传后的远端清单/大小（可行时哈希）核验；
- [ ] 飞书/百度/摘要失败均有幂等重试与人工补偿入口；
- [ ] 每月随机恢复一场音频和文稿，验证备份可用而非仅“上传过”。

## 10. 建议的最小修复顺序

1. **先固定版本与告警真相**：纳入 Git；Session 保存版本；实时链路缺 key 时显式 `DEGRADED` 并通知。
2. **补 P0 恢复语义**：输入重连/GAP、writer 切段、异常 Session 恢复与完整性状态。
3. **调整交付状态机**：先 T5 promotion，再百度上传/验证，再飞书摘要；每一步持久化、幂等、可重试。
4. **完成一年保留闭环**：Working 安全清理、容量预测、低空间预警、恢复抽检。
5. **用真实 zettaranc 样本做质量回归**：术语词典、句级时间、章节、Highlight 精修、摘要质量。
6. **最后做体验层**：音频—文稿 seek、回看入口和简洁控制台；不应在 P0 完整性之前建设重 UI。

## 11. 给独立审阅模型的任务说明

你是独立高级代码与系统审阅者。请先阅读本报告，再自行检查当前工作树、Git 状态、配置、测试、Session 产物、Watchlist 数据库和后台服务状态。**不要直接修改代码或外部系统。**

必须完成：

1. 核对第 2–3 节是否遗漏、误解或错误合并了用户需求；
2. 对第 5 节每一项重新找代码/测试/现场证据，不得把 README 声明当成证明；
3. 重点验证第 8 节的 P0/P1 Findings，提供准确文件与行号、最小复现和置信度；
4. 检查是否存在本文遗漏的更严重问题，尤其是数据丢失、错误 `COMPLETED`、线程/进程泄漏、路径覆盖、凭据泄漏和长时资源问题；
5. 区分 `CODE_EVIDENCE`、`TEST_EVIDENCE`、`LIVE_EVIDENCE`、`INFERENCE`、`UNKNOWN`；
6. 输出 `READY / CONDITIONAL / NOT_READY` 判定，并给出最小修复批次与发布门槛；
7. 不打印、复制或写入任何 key、OAuth code、Webhook、ntfy topic、群 ID、应用 secret；
8. 若运行命令，只做只读检查和本地测试；不得发送通知、创建飞书文档、上传网盘、删除文件、提交 Git 或更改 launchd。

推荐输出格式：

```markdown
# Independent Review
- verdict:
- scope_checked:
- commands_run:
- evidence_limitations:

## Requirement corrections
## Confirmed findings
### [P0/P1/P2/P3] Title
- evidence_level:
- file_and_lines:
- reproduction:
- impact:
- recommended_fix:
- confidence:

## New findings
## Verified capabilities
## Unverified claims
## Security and privacy
## Release gates
## Minimal patch plan
```

## 12. 最终审计判断

Live Sentinel 已经从“单次粘 URL 的实验脚本”演进成有 Watchlist、后台发现、P0/P1 分流、离线加工和多出口交付的个人工具骨架。当前最值得肯定的是：代码明确把完整音频放在最高优先级，外部智能服务大多按可降级组件处理，测试也覆盖了关键 happy path。

但“骨架完整”不等于“可以放心无人值守”。只要 CDN 断线仍会被误判为正常结束、归档 writer 不能恢复、未完成 Session 不能续接、实时 key 缺失却没有显式告警、目标直播还没有一次完整组合验证，就不能把系统判定为长期生产就绪。下一阶段应优先补数据完整性与交付状态机，而不是继续增加模型供应商或 UI 功能。
