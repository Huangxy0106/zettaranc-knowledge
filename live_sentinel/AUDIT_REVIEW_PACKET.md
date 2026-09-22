# Live Sentinel 审计审阅包

> 版本：V1 / 2026-09-10
> 用途：把本文档连同仓库交给更强的代码与系统审阅模型，要求它做可复现、可定位、区分证据等级的审计。
> 范围：`live_sentinel/` 核心实现、`scripts/validate_bilibili_live.py`、测试和运行产物。

## 0. 给审阅模型的任务说明

你是本项目的独立高级审阅者。请先读取本文档，再自行检查仓库代码、测试、配置、
运行产物和 Git 工作区状态。不要把 README 中的“已实现”直接当作事实；每一个结论
都要标注证据来源和置信度。

请输出一份审阅报告，至少包含：

1. 一页结论：当前系统能否作为个人长期运行工具，哪些条件尚未满足；
2. 架构与数据流复述，检查 P0 音频归档和 P1 实时分析之间的隔离是否真的成立；
3. 按 P0/P1/P2/P3 分级的问题清单，每项包含文件、行号、复现步骤、影响、修复建议；
4. 代码声明、单元测试、模拟测试、真实外部运行之间的证据差异；
5. 安全、凭据、隐私、内容合规、成本、可靠性、可恢复性和数据保留审查；
6. ASR、规则评分和 LLM Judge 的方法风险，尤其是误报、漏报、时间对齐和可重复性；
7. 发布前必须补齐的测试，以及可以接受的已知限制；
8. 对下一阶段“watchlist + 自动发现直播 + 本地控制台”的设计建议。

审阅规则：

- 把结论分为 `CODE_EVIDENCE`、`TEST_EVIDENCE`、`LIVE_EVIDENCE`、`INFERENCE`、
  `UNKNOWN`；
- 不要因为一次成功的 5 分钟运行就推断长期稳定、准确率或成本已经得到证明；
- 不要因为适配器类存在就推断对应外部服务已经配置或真实发送过；
- 不要打印、复制或写入任何 API key、Webhook URL、ntfy topic/token；
- 发现问题时优先给出最小可复现命令，不要直接修改代码，除非用户另行授权；
- 检查文档、默认配置、运行时构造和实际产物是否相互一致。

## 1. 项目目标与边界

### 1.1 当前目标

Live Sentinel 监听已授权的 B 站直播音频，在直播过程中保留完整音频归档，并对实时
转写结果做滚动分析；识别可能有价值的片段后通知用户；直播结束后用离线 ASR 回填
更完整的逐字稿、字幕、章节、重点片段和摘要。

### 1.2 明确不属于当前保证范围

- 不负责绕过 B 站登录、DRM、付费限制或其他访问控制；
- 不保证任意直播页都能解析出可播放地址；
- 不保证实时 ASR 的字词准确率、方言覆盖或热点判断准确率；
- 不保证直播 CDN 中断后自动无缝恢复；
- 不负责多用户权限、远程 SaaS 部署或跨主机任务编排；
- 不把“规则命中”或“LLM Judge 输出”当成事实来源，最终证据仍是带时间轴的音频和转写。

### 1.3 用户层需求假设

当前使用场景是个人研究与资料积累：用户希望长期关注少量直播源，只在出现高信息密度
内容时被提醒，直播结束后能按主题回看，并保留可审计的原始音频、转写和判断过程。
这一假设仍需要产品化阶段与用户确认。

## 2. 仓库与 Git 状态

主要目录：

| 路径 | 责任 |
| --- | --- |
| `live_sentinel/models.py` | `AudioFrame`、`SpeechSegment`、`TranscriptSegment`、`InterestEvent`、`Highlight`、`Session` |
| `live_sentinel/audio/` | 输入 PCM、P0 分片归档、时间轴检查、Final Audio 合并与校验 |
| `live_sentinel/asr/` | VAD、实时 ASR、OpenAI/本地 FunASR 离线 ASR、DashScope Paraformer |
| `live_sentinel/analysis/` | 规则内容分析、评分、OpenAI-compatible/DeepSeek LLM Judge |
| `live_sentinel/interest/` | Candidate/Hot/Cooling 状态机和通知冷却 |
| `live_sentinel/session/manager.py` | 一场 Session 的生命周期与 P0/P1 协调 |
| `live_sentinel/storage/` | append-only JSONL 和 SQLite 审计表 |
| `live_sentinel/notification/` | Console、Feishu Webhook、ntfy、Memory 通知器 |
| `live_sentinel/config.py` | JSON/YAML 配置模型与默认值 |
| `live_sentinel/integrations.py` | 从配置和环境变量组装外部适配器 |
| `scripts/validate_bilibili_live.py` | B 站页面/公开房间接口解析、限时采集、真实服务组装入口 |
| `tests/live_sentinel/` | 单元测试、HTTP 模拟和本地 Session 测试 |
| `sessions/` | 运行时音频、转写、分析、数据库和最终文稿 |

截至本文档生成时，`live_sentinel/`、`scripts/`、`tests/` 等核心实现仍是当前工作区
中的未跟踪文件，不能把 Git `HEAD` 当成完整的实现基线。工作区还存在其他用户编辑，
审阅时应以文件内容和测试结果为准，并单独记录待提交/待归档状态。

## 3. 当前架构与生命周期

```text
B站 URL
  -> 页面解析；失败时按数字 room_id 调公开只读房间/播放地址接口
  -> FFmpeg 将已授权 HTTP-FLV 解码为统一 PCM
  -> AudioTee
       |-- P0 archive_queue: 阻塞写入，SegmentArchiveWriter 写 working 分片
       |-- P1 realtime_queue: 非阻塞，拥塞时允许丢实时帧
             -> RealtimeAudioPreprocessor
             -> EnergyVAD
             -> DashScope paraformer-realtime-v2 / 其他 Streaming ASR
             -> realtime/transcript.jsonl + SQLite realtime_transcripts
             -> 滚动窗口规则分析/评分
             -> 达到候选阈值时才调用 LLM Judge
             -> InterestStateMachine -> highlights/events -> 通知适配器
  -> 输入结束
  -> 停止实时线程、刷新 VAD、关闭 working 分片
  -> AudioFinalizer: 时间轴、解码、时长、SHA-256
  -> audio/final/<session>.flac 或 .wav
  -> FunASR/OpenAI Offline ASR
  -> final/ 文稿、字幕、章节、重点和摘要
  -> SQLite final_transcripts + 最终 session 元数据
  -> 关闭本 Session 自己启动的本地 ASR 子进程
```

关键失效边界：

- P0 归档、Final Audio 合并或时间轴校验失败，Session 应进入 `FAILED`；
- 实时 ASR、LLM Judge、通知、本地离线 ASR 故障会记录错误事件，原则上不应删除或
  破坏已经固化的 P0 音频；
- `AudioTee` 的实时队列允许丢帧，因此实时转写不是完整证据；完整证据是 Final Audio
  和离线转写（如果离线服务可用）；
- 当前 `SessionManager` 是单场运行器，不是长期任务调度器。

## 4. 产物契约

以 `sessions/<session_id>/` 为根：

| 产物 | 用途 | 审阅重点 |
| --- | --- | --- |
| `session.json` | 运行状态、房间、时长、Final Audio 路径 | 是否包含足够的配置/模型/版本快照 |
| `checkpoint.json` | 最近归档时间点和分片 | 是否能真正恢复，还是只能诊断 |
| `audio/working/segment_*.flac` | 采集过程分片 | 时间戳、空洞、checksum、部分文件清理 |
| `audio/final/<id>.flac` | 固化音频证据 | 解码、时长、时间轴、完整性 |
| `realtime/transcript.jsonl` | 实时 ASR 结果 | 是否允许丢帧、是否标注 provider/model |
| `realtime/analysis.jsonl` | 周期性规则/语义分析 | 特征、窗口、分数、语义结果是否可追溯 |
| `realtime/events.jsonl` | 状态、错误、通知等事件 | 某些运行可能没有文件，需与 SQLite 对照 |
| `session.sqlite` | 可查询审计数据库 | 表结构、事务、WAL、重放一致性 |
| `final/verbatim.jsonl` | 离线 ASR 结构化段 | `transcript_source`、时间戳、切块边界 |
| `final/verbatim.md` | 原始顺序逐字稿 | 与 JSONL/音频的一致性 |
| `final/readable.md` | 阅读稿 | 不应冒充逐字稿或事实来源 |
| `final/subtitles.srt` | 字幕 | 时间轴是否在播放器中可用 |
| `final/highlights.json` / `chapters.json` | 重点和章节 | 是否由状态机真实产生，还是空占位 |
| `final/summary.md` / `final/session.json` | 最终摘要和状态 | 是否说明转写来源、失败和缺口 |

## 5. 可复现运行手册

### 5.1 基础检查

在仓库根目录执行：

```bash
python3 -m compileall -q live_sentinel tests/live_sentinel
git diff --check
python3 -m unittest discover -s tests/live_sentinel -t . -v
```

当前最近一次结果：27 个测试通过，`compileall` 通过，`git diff --check` 通过。
这是本地代码与模拟服务证据，不等于外部服务长期可用性证明。

### 5.2 不触发外部服务的闭环

```bash
python3 -m live_sentinel demo \
  --output-dir /tmp/live-sentinel-demo \
  --duration-sec 8
```

该 demo 使用合成 PCM、WAV 和本地 Callable ASR，适合检查 Session 生命周期和产物
写出，不能证明 B 站抓流、云端 ASR、LLM 或通知已经接通。

### 5.3 真实限时运行

先通过环境变量提供所需密钥；不要把真实值写进配置文件或审计报告：

```bash
export DASHSCOPE_API_KEY='REDACTED'
export DEEPSEEK_API_KEY='REDACTED'
# 若启用本地 FunASR，确保 funasr-server 在 PATH，或在配置中填写绝对命令路径。

python3 scripts/validate_bilibili_live.py '<LIVE_BILIBILI_URL>' \
  --duration-sec 300 \
  --output-dir sessions \
  --config live_sentinel/config.paraformer-funasr.example.json
```

当前脚本会：解析公开页面，必要时按数字 room_id 调公开房间接口，选择 HTTP-FLV，
由 FFmpeg 解码，组装适配器并运行一场限时 Session。直播当前不在线时返回 2；采集
或适配器失败时返回 1；成功结束后打印诊断目录和 Final Audio 路径。

### 5.4 检查一次运行

```bash
sqlite3 -readonly sessions/<session_id>/session.sqlite '.tables'
ffprobe -v error -show_entries format=duration \
  -of default=noprint_wrappers=1:nokey=1 \
  sessions/<session_id>/audio/final/<session_id>.flac
```

也可以直接检查 `session.json`、`checkpoint.json`、`realtime/*.jsonl` 和 `final/*`，
并将 SQLite 计数与 JSONL 行数相互对照。

## 6. 现有运行证据

### 6.1 最近一次真实 5 分钟运行

证据目录：

[`sessions/20260909_170317_room620373/`](../sessions/20260909_170317_room620373/)

已观察到：

- `status=COMPLETED`，`duration_ms=300000`；
- 49 条实时转写，1 条离线最终转写；
- 5 条 SQLite `events`，0 条 `highlights`，0 条 `chapters`；
- Final Audio 为约 17.7 MB 的 FLAC，可正常解码；
- `final/session.json` 标记 `transcript_source=offline_asr`；
- FunASR 由任务管理的进程在结束后已停止。

这证明一条真实直播输入在该时间点完成了“抓流—实时 ASR—归档—离线回填”的闭环，
不证明其他直播间、长时间运行、断网恢复、热点召回率、通知送达率或成本稳定性。

### 6.2 模拟外部服务证据

测试使用本地 HTTP server 模拟 OpenAI-compatible LLM、FunASR、Feishu 和 ntfy；
已覆盖请求路径、认证 header、正文/JSON、时间戳、签名、优先级、标签和 token 传递。
这些测试验证的是适配器协议实现，不是第三方服务的真实送达。

### 6.3 尚需重新获取或持续记录的证据

- DashScope 不同网络、地域、模型版本下的延迟、断线和重连；
- FunASR 进程启动失败、OOM、模型首次加载耗时和长任务稳定性；
- DeepSeek Judge 的真实 JSON 合约、费用、超时和错误回退；
- ntfy/Feishu 的真实消息送达、重复消息、速率限制和权限撤销；
- 同一直播的实时转写与离线转写差异、术语召回和时间偏移；
- 多场并发、跨天运行、直播状态反复切换和进程重启后的幂等性。

## 7. 审计重点与潜在缺口清单

以下是审阅入口，不是已经确认的缺陷；审阅模型必须用代码和复现结果确认严重性：

### P0/P1：可靠性与证据完整性

- B 站播放地址失效或 CDN 短暂断开时，是否会立即结束 Session，是否有安全重试；
- `AudioTee` 实时丢帧是否被记录、展示并纳入质量指标；
- Final Audio 的时间轴空洞、重复、截断和音频内容是否能从 SQLite/JSONL 对账；
- 任务进程被 `SIGTERM`、机器休眠或 Python 异常终止时，分片和数据库是否可恢复；
- 一次通知失败、LLM 超时或 FunASR 退出是否可能掩盖原始 P0 错误；
- 同一房间重复启动是否会产生重复 Session、重复通知或覆盖产物。

### P1：分析与模型质量

- 规则评分、窗口长度、连续窗口数和冷却时间是否有标定数据；
- LLM Judge 只在规则候选触发的设计是否会漏掉规则未覆盖的新主题；
- 实时转写分段替换、VAD 边界、ASR 句级时间戳和离线切块边界是否一致；
- `readable.md`、摘要和通知是否清楚标明“模型推断”与“原始转写/音频证据”；
- 是否有人工标注样本、黄金问题和回归集，而不仅是一次成功 demo。

### P1/P2：安全、隐私与成本

- 环境变量、进程命令行、错误日志和 Session 元数据是否意外泄露密钥或播放 URL；
- 音频、逐字稿、SQLite 和通知正文的保留期限、访问权限和备份策略是什么；
- 云端 ASR/LLM 的请求是否包含超出必要范围的音频/文本，是否需要脱敏；
- 长时间 watchlist 运行是否有每日分钟数、并发数、预算或熔断上限；
- ntfy 公共 topic、Feishu Webhook、第三方 Bot token 的撤销和轮换流程是什么。

### P2：工程化与可维护性

- `session.json` 是否应该保存配置快照、provider/model、代码版本和源 URL 的规范化摘要；
- SQLite schema 是否有迁移版本、并发写入边界和 WAL 清理策略；
- `scripts/validate_bilibili_live.py` 是否应拆成 source resolver、scheduler 和 runner；
- 当前单场 CLI 是否需要变成长期运行的 desired-state/watchlist 服务；
- 文档中的能力列表、默认值和真实运行路径是否保持自动化校验。

## 8. 审阅报告的最低交付格式

请审阅模型按以下格式返回，方便后续修复：

```markdown
# 审阅结论
- verdict: READY / CONDITIONAL / NOT_READY
- scope_checked:
- commands_run:
- evidence_level:

# Findings
## [P0] 标题
- category:
- evidence: CODE_EVIDENCE / TEST_EVIDENCE / LIVE_EVIDENCE / INFERENCE / UNKNOWN
- file_and_lines:
- reproduction:
- impact:
- recommended_fix:
- confidence:

# Verified capabilities
# Unverified claims
# Security and privacy
# Model/data quality
# Release gates
# Minimal next patch
```

## 9. 审计结论的预期边界

本文档的作用是让审阅者能够复现、定位和质疑当前实现，不是为 V1 做质量背书。
当前最重要的产品缺口不是再增加一个 ASR provider，而是缺少“长期关注对象、自动发现
直播、可暂停/恢复、告警收件箱和回看入口”的任务层。下一阶段建议参阅
[`USAGE_DESIGN_RECOMMENDATION.md`](USAGE_DESIGN_RECOMMENDATION.md)。
