# Live Sentinel 更优雅的使用方式

> 结论先行：当前“在对话框输入一个 URL，然后启动监听”适合验证单场链路，
> 不适合长期使用。更合适的产品形态是一个**本地 watchlist 服务**：一次登记关注对象，
> 后台自动发现直播并按策略启动 Session；用户只在需要时处理提醒和回看结果。

## 1. 从需求端重新定义问题

不要把需求定义为“抓取一个 URL”。更接近真实使用目标的是：

1. **持续关注**：我关心的是主播/频道/主题，而不是某一次 URL；
2. **低打扰**：只在出现值得我中断当前工作的内容时提醒；
3. **可回看**：提醒必须能跳到时间点、音频、原始转写和分析依据；
4. **不漏关键场次**：直播开始、网络短暂中断、页面 URL 变化，不应要求我重新手工输入；
5. **可调整**：不同来源有不同关键词、主题、阈值、ASR、通知和保留期限；
6. **可审计**：我能知道为什么提醒、用了什么模型、原始证据在哪里；
7. **低运维和可控成本**：不需要每天手动打开终端，也不能因长期运行失控地产生云费用；
8. **权限边界清楚**：只监听用户有权访问的内容，不把凭据、私密音频和通知密钥混在代码里。

这些需求决定了系统的中心对象应当是 `Watch`/`Source`/`Policy`，而不是一次性
`validate(url)` 进程。

## 2. 当前入口的问题

现有 `scripts/validate_bilibili_live.py` 是一个有价值的验证入口，但它仍然是一次性
运行器：

- 每次都要人工提供 URL；
- 只在当前运行时知道要关注什么，没有持久化关注清单；
- 没有“启用/暂停/静音/只在某时段运行”的目标状态；
- 没有统一的运行状态页、告警收件箱和历史回看入口；
- 直播开始前无法自动发现，直播结束后也没有统一的收尾队列；
- 配置文件更像部署参数，不像用户可以管理的关注策略；
- 出错时依赖终端和日志，无法用“需要我处理什么”来呈现；
- 目前的通知是事件推送，不是带有确认、忽略、稍后处理和反馈的工作流。

因此，下一步不应只是把对话框换成另一个输入框，而应增加一个**任务层**。

## 3. 推荐的目标产品：本地 Watchlist Console

### 3.1 用户看到的四个页面

#### A. 关注清单（Watchlist）

每行是一个长期关注对象：

| 字段 | 示例 |
| --- | --- |
| 名称 | `差评硬件部` |
| 来源 | B站房间 URL / room_id |
| Profile | `硬件新品` |
| 状态 | `启用 / 暂停 / 当前直播 / 失败` |
| 运行时段 | `工作日 18:00–01:00` |
| 最近一次 | `2026-09-10 17:03，300 秒，已完成` |
| 通知 | `ntfy 紧急 + 飞书摘要` |

用户只需首次点击“添加关注”，之后不再手动复制 URL。

#### B. 运行台（Live Operations）

显示当前自动发现的直播：

- `OFFLINE → DISCOVERED → STARTING → RUNNING → STOPPING → COMPLETED`；
- 当前音频时长、实时转写数量、最近分析时间、队列丢帧数；
- 当前正在分析的主题和最近一次通知；
- 是否发生 CDN、ASR、LLM、通知或离线后处理错误；
- 一键“暂停本场”“只归档不分析”“立即结束”。

#### C. 告警收件箱（Alert Inbox）

每条告警不是普通群消息，而是一条可处理的任务：

- 主题、评分、摘要、直播源、时间点；
- “打开直播”“打开 Final Audio”“打开逐字稿”；
- `保存重点`、`稍后提醒`、`静音此源`、`标记误报`；
- 显示触发理由：规则特征、LLM Judge 结果、使用的窗口和模型；
- 相同主题在冷却窗口内合并，避免连续刷屏。

#### D. 归档与搜索（Archive）

按来源、日期、主题、关键词、是否人工保存筛选：

- 播放 Final Audio 并跳到时间点；
- 查看原始逐字稿、阅读稿、字幕和摘要；
- 查看实时转写与离线转写差异；
- 查看通知和状态事件；
- 导出 Markdown/SRT/JSONL。

### 3.2 后台运行方式

推荐把当前 Session Runner 放在一个**本地长期运行服务**后面：

```text
Watchlist DB
  -> Scheduler / Live Discovery
  -> Session Supervisor（每个房间最多一个活跃 Session）
  -> 当前已有 SessionManager
  -> Artifact Store + SQLite
  -> Alert Router
  -> ntfy/Bark/飞书/本地通知
```

在 macOS 上可以先用 `launchd` 保持本地服务运行；不需要立即引入 Docker、Kubernetes
或远程 SaaS。浏览器打开 `http://127.0.0.1:<port>` 即可管理。

## 4. 推荐的交互方式

### 4.1 首次配置：一次性表单

```text
添加关注源
--------------------------------
名称：       差评硬件部
URL：        https://live.bilibili.com/620373
内容策略：   硬件新品 / 科技产业
运行时段：   每天 08:00–02:00
重点阈值：   高信息密度且连续两次命中
实时 ASR：   Paraformer Realtime
离线 ASR：   本地 FunASR
提醒：       ntfy 高优先级
保留：       音频 30 天，重点永久保留
[保存并启用]
```

### 4.2 日常使用：收件箱，而不是终端

用户每天只需要：

1. 打开告警收件箱；
2. 点开值得看的提醒；
3. 选择保存、忽略或静音；
4. 在归档页回看。

终端命令保留给调试和批处理，不再是主要入口。

### 4.3 高级用户：CLI/TUI 作为快捷入口

可以同时提供一个稳定的命令接口，但它应操作持久化对象：

```bash
sentinel source add \
  --name '差评硬件部' \
  --url 'https://live.bilibili.com/620373' \
  --profile hardware

sentinel source enable '差评硬件部'
sentinel status
sentinel inbox --unread
sentinel session open 20260909_170317_room620373
sentinel source mute '差评硬件部' --until tomorrow
```

上面的命令是目标交互草案，不代表当前 CLI 已经实现。

## 5. 把“内容策略”做成 Profile

不要让用户直接修改一大段模型配置。用户应该选择 Profile，系统内部再展开配置。

建议先提供三个 Profile：

### `ai_industry`

- 关键词：AI、Agent、大模型、算力、模型发布、产业链；
- 高信息密度、明确观点、结构化讲解权重较高；
- 只对高置信事件发送即时提醒；
- 直播结束后保留完整音频和可搜索逐字稿。

### `investment_research`

- 关键词：行业、公司、订单、竞争格局、估值、政策；
- 默认不把单个股票名称直接视为高价值；
- 提醒中强制附原始音频时间点和证据文本；
- 需要额外的“事实/观点/推测”标记。

### `hardware_release`

- 关键词：新品、发布会、芯片、手机、电脑、价格、参数；
- 允许更低延迟的通知；
- 重点保存产品名、参数、价格和对比句；
- 直播结束后生成章节和产品索引。

每个 Profile 应包含：

```yaml
profile_id: hardware_release
rules:
  whitelist: [...]
  blacklist: [...]
  candidate_threshold: 0.65
  hot_threshold: 0.78
analysis:
  interval_sec: 60
  llm_window_sec: 300
providers:
  realtime_asr: dashscope/paraformer-realtime-v2
  offline_asr: funasr_local/sensevoice
notifications:
  urgent: ntfy
  digest: feishu_webhook
retention:
  audio_days: 30
  transcript_days: 365
```

## 6. 通知设计：即时提醒、协作摘要、审计记录分离

推荐三层路由：

| 事件 | 即时通知 | 协作通知 | 永久记录 |
| --- | --- | --- | --- |
| 普通分析窗口 | 不通知 | 不通知 | `analysis.jsonl` + SQLite |
| 候选热点 | 通常不通知 | 不通知 | 分析事件 |
| 确认高价值片段 | ntfy/Bark | 可选飞书 | highlight + 原始证据 |
| 直播完成 | 不打扰 | 一次摘要 | Final Audio + final/ |
| ASR/LLM/通知错误 | 个人高优先级 | 可选 | 错误事件 |
| 服务连续失败 | 高优先级 + 状态页 | 可选 | 运行日志/事件 |

这样既保留飞书的群协作价值，又不会把飞书群当作唯一的状态数据库。

## 7. 后台服务必须拥有的状态和约束

### 7.1 Desired state 与 observed state 分离

用户设置的是：

```text
source.enabled = true
profile = hardware_release
schedule = every day
```

系统观测的是：

```text
live_status = LIVE
session_status = RUNNING
last_error = none
```

服务重启后应从 desired state 重建 observed state，而不是依赖内存变量。

### 7.2 幂等与去重

- 以规范化 `room_id` 作为长期来源 ID，不以 URL 字符串作为唯一键；
- 同一 room 在同一时间最多一个活跃 Session；
- Session ID、最终音频路径和通知事件必须幂等；
- 重试不能重复发送同一高价值告警；
- 页面 URL 变化不能丢失历史关联。

### 7.3 成本和资源护栏

- 每个来源设置每日最长监听分钟数；
- 设置并发 Session 上限，个人模式默认为 1；
- 仅在发现直播后启动音频和 ASR；
- LLM Judge 保持候选触发，增加每日调用上限和熔断；
- 本地 FunASR 采用 lazy/session 生命周期管理；
- 音频、逐字稿和中间文件分开设置保留期；
- 连续失败使用指数退避，不要固定高频轮询。

## 8. 推荐实施顺序

### Phase 1：任务层（优先）

> 实现状态（2026-09-11）：Watchlist SQLite、B 站状态发现、Supervisor、指数退避、
> 同房间去重、`sentinel source/status` 和 launchd 用户服务已落地。Profile 目前作为
> source 字段持久化，per-source 策略覆盖与本地控制台仍属于后续阶段。

目标：不用改变现有分析链路，就能长期运行。

- `sources` / `watch_rules` / `sessions` 数据模型；
- B 站直播状态发现器；
- `SessionSupervisor`：启动、停止、重试、去重；
- `launchd` 本地服务；
- `sentinel status`、`sentinel source add/enable/disable`；
- 现有 `validate_bilibili_live.py` 保留为单场诊断工具。

验收：用户登记一次源后，服务重启仍能恢复关注状态；同一房间不会重复启动。

### Phase 2：本地控制台

目标：让日常使用不再依赖终端或对话框。

- Watchlist 页面；
- Live Operations 页面；
- Alert Inbox；
- Archive 搜索和时间点回放；
- Server-Sent Events 或轮询展示状态，不急于引入复杂前端框架。

验收：用户可以在浏览器完成添加源、启停、静音、处理告警和打开归档。

### Phase 3：策略和反馈

- Profile 管理；
- 告警反馈：保存/误报/忽略；
- 基于反馈生成离线评估集；
- per-source 通知路由和保留策略；
- 生成每日/每周摘要。

验收：误报和漏报可以被记录，后续阈值调整有数据依据。

### Phase 4：可选扩展

- 更多直播平台或 RSS/视频源；
- 远程访问和移动端；
- 多用户权限；
- GPU/云端队列和更复杂的模型路由。

这些不应在 Phase 1 之前引入，否则会把“能长期可靠关注”变成基础设施项目。

## 9. 下一阶段的产品验收标准

在认为系统从“验证脚本”升级为“个人工具”之前，至少满足：

- [ ] 首次登记源后，不需要每场重新输入 URL；
- [ ] 服务重启后能恢复启用的 Watchlist；
- [ ] 同一房间不会重复 Session 或重复告警；
- [ ] 直播开始/结束/中断都有状态可见；
- [ ] 高价值提醒可以打开对应时间点的音频和证据文本；
- [ ] 普通分析不打扰用户，告警有冷却和去重；
- [ ] 运行错误、模型、配置和来源信息可以审计；
- [ ] 有每日分钟数、并发数和 LLM 调用上限；
- [ ] 音频、逐字稿、通知和凭据有明确保留/撤销策略；
- [ ] 至少有一组人工标注样本用于评估热点判断质量。

## 10. 最终建议

短期不要继续堆叠“输入 URL 后运行”的参数。建议把当前代码定位为：

- `SessionManager`：可靠的单场执行内核；
- `validate_bilibili_live.py`：诊断和回归入口；
- 新增 `Watchlist + Supervisor`：长期运行任务层；
- 新增本地控制台：日常使用入口；
- ntfy/飞书：告警和协作出口；
- SQLite + Session artifacts：审计和回放底座。

这样用户思维会从“我要启动一个脚本”变成“我维护一个关注清单，系统替我观察，
我只处理值得看的内容”。
