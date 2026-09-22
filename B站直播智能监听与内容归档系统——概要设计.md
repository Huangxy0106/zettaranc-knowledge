# 目录

1. [项目概述](#1-项目概述)
   - [1.1 项目背景](#11-项目背景)
2. [建设目标](#2-建设目标)
   - [2.1 实时监听](#21-实时监听)
   - [2.2 干货检测](#22-干货检测)
   - [2.3 完整音频归档](#23-完整音频归档)
   - [2.4 直播后加工](#24-直播后加工)
3. [设计原则](#3-设计原则)
   - [3.1 原始音频是 Source of Truth](#31-原始音频是-source-of-truth)
   - [3.2 最终一场直播一个音频文件](#32-最终一场直播一个音频文件)
   - [3.3 Segment 是容错机制，不是业务对象](#33-segment-是容错机制不是业务对象)
   - [3.4 实时链路与归档链路分离](#34-实时链路与归档链路分离)
   - [3.5 实时 ASR 与最终 ASR 分离](#35-实时-asr-与最终-asr-分离)
   - [3.6 所有数据统一时间轴](#36-所有数据统一时间轴)
4. [系统总体架构](#4-系统总体架构)
5. [Session Manager](#5-session-manager)
   - [5.1 职责](#51-职责)
   - [5.2 Session 状态](#52-session-状态)
6. [Audio Source](#6-audio-source)
7. [Audio Tee](#7-audio-tee)
8. [Archive Pipeline](#8-archive-pipeline)
   - [8.1 音频格式](#81-音频格式)
   - [8.2 Segment 策略](#82-segment-策略)
   - [8.3 为什么仍保留 Segment](#83-为什么仍保留-segment)
   - [8.4 Final Audio](#84-final-audio)
   - [8.5 Segment 生命周期](#85-segment-生命周期)
9. [Realtime Audio Pipeline](#9-realtime-audio-pipeline)
10. [VAD 模块](#10-vad-模块)
11. [Streaming ASR](#11-streaming-asr)
12. [Transcript Buffer](#12-transcript-buffer)
13. [Content Analyzer](#13-content-analyzer)
14. [白名单检测](#14-白名单检测)
15. [黑名单检测](#15-黑名单检测)
16. [Information Density](#16-information-density)
17. [Structured Speech Detection](#17-structured-speech-detection)
18. [Topic Continuity](#18-topic-continuity)
19. [Interest Engine](#19-interest-engine)
20. [两级判定](#20-两级判定)
21. [干货状态机](#21-干货状态机)
22. [推荐初始参数](#22-推荐初始参数)
23. [Notification](#23-notification)
24. [Highlight](#24-highlight)
25. [Offline Finalization](#25-offline-finalization)
26. [最终逐字稿](#26-最终逐字稿)

- [26.1 Verbatim Transcript](#261-verbatim-transcript)
- [26.2 Readable Transcript](#262-readable-transcript)

27. [专有名词修正](#27-专有名词修正)
28. [Chaptering](#28-chaptering)
29. [音频与文稿联动](#29-音频与文稿联动)
30. [第一阶段输出物](#30-第一阶段输出物)
31. [文件目录设计](#31-文件目录设计)
32. [SQLite 数据设计](#32-sqlite-数据设计)

- [sessions](#sessions)
- [audio\_segments](#audio_segments)
- [audio\_final](#audio_final)
- [realtime\_transcripts](#realtime_transcripts)
- [final\_transcripts](#final_transcripts)
- [topic\_segments](#topic_segments)
- [highlights](#highlights)
- [chapters](#chapters)
- [events](#events)

33. [配置设计](#33-配置设计)
34. [异常恢复](#34-异常恢复)

- [34.1 Audio Source 中断](#341-audio-source-中断)
- [34.2 ASR 崩溃](#342-asr-崩溃)
- [34.3 LLM 不可用](#343-llm-不可用)
- [34.4 Archive Writer 异常](#344-archive-writer-异常)
- [34.5 整体进程异常](#345-整体进程异常)

35. [Final Audio 校验](#35-final-audio-校验)

- [Duration Check](#duration-check)
- [Segment Coverage](#segment-coverage)
- [Decode Test](#decode-test)
- [Checksum](#checksum)

36. [性能目标](#36-性能目标)

- [实时 ASR](#实时-asr)
- [内容判断](#内容判断)
- [高价值提醒](#高价值提醒)

37. [资源控制](#37-资源控制)
38. [数据完整性](#38-数据完整性)
39. [第一阶段暂不实现](#39-第一阶段暂不实现)
40. [llm-wiki 的第一阶段定位](#40-llm-wiki-的第一阶段定位)
41. [V1 完整流程](#41-v1-完整流程)
42. [推荐模块拆分](#42-推荐模块拆分)
43. [核心伪代码](#43-核心伪代码)
44. [Audio Finalizer 伪代码](#44-audio-finalizer-伪代码)
45. [最终交付视角](#45-最终交付视角)
46. [后续演进](#46-后续演进)
47. [总结](#47-总结)

# B站直播智能监听与内容归档系统——概要设计

## 1. 项目概述

### 1.1 项目背景

目标直播通常持续数小时，但真正具有信息价值的内容可能只集中在部分时间段。用户无法持续守候直播，因此需要一套能够长期运行的自动化系统，对直播口播内容进行实时理解，并在检测到高价值内容时主动提醒。

与此同时，为方便直播结束后的完整回听和内容整理，需要：

- 保存整场直播的完整音频；
- 生成带时间戳的高质量口播文稿；
- 自动划分主题章节；
- 标记高价值内容区间；
- 支持从文稿和 Highlight 精确定位到原始音频。

本系统定位为：

**基于观众端授权音频的实时内容理解、价值检测与直播内容归档系统。**

第一阶段重点解决：

```text
听直播
→ 实时理解
→ 判断是否进入干货
→ 主动提醒
→ 保存完整音频
→ 生成完整口播文稿
→ 生成章节和重点区间
```

第一阶段暂不重点建设：

- `llm-wiki` 深度联动；
- 全文搜索；
- 向量检索；
- 混合检索；
- RAG 问答；
- 跨直播知识聚合。

这些能力作为后续扩展方向保留。

---

# 2. 建设目标

系统第一阶段需要实现四项核心能力。

## 2.1 实时监听

- 持续接收直播音频；
- 实时语音识别；
- 维护最近若干分钟的口播上下文；
- 分析当前主题、信息密度及内容状态。

---

## 2.2 干货检测

根据以下信号判断当前内容价值：

- 用户兴趣白名单；
- 排除主题黑名单；
- 当前主题；
- 信息密度；
- 结构化表达程度；
- 主题持续性；
- 必要时使用 LLM 进行语义判断。

系统需要检测：

```text
普通内容
   ↓
疑似进入干货
   ↓
确认进入干货
   ↓
发送提醒
   ↓
持续记录
   ↓
检测干货结束
```

---

## 2.3 完整音频归档

系统需要保存一场直播的完整高质量音频。

从用户使用视角看：

> **一场直播最终对应一个完整音频文件。**

例如：

```text
2026-09-09_UP主名称_直播.flac
```

内部为了：

- 防止长时间运行异常；
- 避免单文件损坏；
- 支持断点恢复；

可以进行临时 Segment 分片，但 Segment 原则上不得过短。

推荐：

```text
1～2 小时 / Segment
```

直播结束后自动合并：

```text
segment_000.flac
segment_001.flac
segment_002.flac
        ↓
   Finalize
        ↓
2026-09-09_UP主名称_直播.flac
```

最终完整音频作为系统长期保存的正式文件。

---

## 2.4 直播后加工

直播结束后使用完整音频进行高质量离线处理：

- 高精度 ASR；
- 时间戳校准；
- 标点恢复；
- 专有名词修正；
- 忠实逐字稿；
- 阅读版文稿；
- 字幕；
- 自动章节；
- Highlight 边界修正；
- 直播整体摘要。

---

# 3. 设计原则

## 3.1 原始音频是 Source of Truth

整个系统最重要的数据是：

```text
完整直播音频
```

以下内容全部属于可重新生成的数据：

- 实时 ASR；
- 最终 ASR；
- LLM 判断；
- 摘要；
- Highlight；
- 章节；
- 阅读版文稿。

因此：

> **任何实时分析组件发生故障，都不应影响完整音频保存。**

---

# 3.2 最终一场直播一个音频文件

系统区分：

```text
运行时存储
```

与：

```text
最终归档
```

运行过程中可以采用：

```text
1～2 小时临时 Segment
```

最终必须生成：

```text
一场直播一个 Final Audio
```

例如：

```text
audio/
├── working/
│   ├── segment_000.flac
│   ├── segment_001.flac
│   └── segment_002.flac
│
└── final/
    └── 20260909_190013_UP主.flac
```

默认最终使用者无需感知内部 Segment。

---

# 3.3 Segment 是容错机制，不是业务对象

Segment 的作用主要是：

- 防止长文件写入中断；
- 控制故障影响范围；
- 支持恢复；
- 方便直播过程中阶段性 flush；
- 防止整个数小时直播依赖一个尚未正常关闭的文件。

因此 Segment 不参与：

- 章节定义；
- Highlight 定义；
- ASR 逻辑；
- 内容主题定义。

所有上层业务对象仍然使用：

```text
Session Timeline
```

而不是 Segment Timeline。

---

# 3.4 实时链路与归档链路分离

```text
                     Live Audio
                         │
                         ▼
                    Audio Tee
                         │
            ┌────────────┴────────────┐
            │                         │
            ▼                         ▼
      Archive Pipeline          Realtime Pipeline
      高质量完整归档              低延迟实时分析
```

Archive Pipeline 优先保证：

- 完整；
- 稳定；
- 无音频丢失。

Realtime Pipeline 优先保证：

- 低延迟；
- 可降级；
- 长时间稳定运行。

---

# 3.5 实时 ASR 与最终 ASR 分离

实时 ASR 用于：

```text
实时理解
+
干货判断
+
实时提醒
```

最终 ASR 用于：

```text
完整逐字稿
+
字幕
+
章节
+
后续归档
```

实时识别允许存在一定错误。

最终识别则应优先保证质量。

---

# 3.6 所有数据统一时间轴

定义：

```text
Session Start = T0
```

整个系统全部使用：

```text
session-relative timestamp
```

例如：

```json
{
  "start_ms": 5530520,
  "end_ms": 5564100
}
```

对应：

```text
01:32:10.520
→
01:32:44.100
```

统一时间轴覆盖：

- 原始音频；
- 实时 ASR；
- 最终 ASR；
- Topic；
- Highlight；
- Notification；
- Chapter。

形成：

```text
提醒
 ↓
Highlight
 ↓
章节
 ↓
逐字稿
 ↓
完整直播音频
```

---

# 4. 系统总体架构

```text
┌─────────────────────────────────────────────────────┐
│                  Session Manager                    │
│ 生命周期 / 配置 / 状态 / 故障恢复 / Finalize            │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
                  Audio Source
                       │
                    PCM Stream
                       │
                       ▼
                   Audio Tee
                 ┌─────┴─────┐
                 │           │
                 ▼           ▼
         Archive Pipeline   Realtime Pipeline
                 │           │
        48kHz PCM│           │16kHz mono
                 │           ▼
          Long Segment      VAD
          1～2小时           │
                 │           ▼
                 │       Streaming ASR
                 │           │
                 │           ▼
                 │     Transcript Buffer
                 │           │
                 │           ▼
                 │     Content Analyzer
                 │           │
                 │           ▼
                 │      Interest Engine
                 │           │
                 │           ▼
                 │       State Machine
                 │           │
                 │           ▼
                 │       Notification
                 │
                 ▼
          Segment Storage
                 │
            Session结束
                 │
                 ▼
          Audio Finalizer
                 │
                 ▼
       一场直播一个完整音频
                 │
                 ▼
          Offline Finalization
                 │
       ┌─────────┼─────────┐
       ▼         ▼         ▼
 Offline ASR  Chaptering  Highlights
       │         │         │
       └─────────┴─────────┘
                 │
                 ▼
             Final Output
```

---

# 5. Session Manager

## 5.1 职责

负责一场直播完整生命周期：

- 创建 Session；
- 加载配置；
- 启动 Audio Source；
- 启动 Archive Pipeline；
- 启动 Realtime Pipeline；
- 监控各模块运行状态；
- 维护 checkpoint；
- 处理异常；
- 结束 Session；
- 合并最终音频；
- 触发 Offline Finalization。

---

## 5.2 Session 状态

```text
CREATED
   ↓
INITIALIZING
   ↓
RUNNING
   ↓
STOPPING
   ↓
AUDIO_FINALIZING
   ↓
POST_PROCESSING
   ↓
COMPLETED
```

异常状态：

```text
INTERRUPTED
RECOVERING
FAILED
```

---

# 6. Audio Source

第一阶段假定系统能够稳定取得 PCM 音频。

统一内部输入：

```text
Sample Rate: 48 kHz
Channels: Stereo
Sample Format: PCM S16LE / Float32
```

接口：

```python
class AudioSource:

    def start(self):
        ...

    def read(self) -> AudioFrame:
        ...

    def stop(self):
        ...
```

AudioFrame：

```python
class AudioFrame:
    timestamp_ms: int
    pcm: bytes
    sample_rate: int
    channels: int
```

Audio Source 不感知：

- ASR；
- LLM；
- Highlight；
- 文件最终组织形式。

---

# 7. Audio Tee

Audio Tee 将输入音频同时发送到：

```text
Archive Queue

Realtime Queue
```

架构：

```text
Audio Source
      ↓
 Ring Buffer
      ↓
 ┌────┴────┐
 ↓         ↓
Archive   Realtime
Queue     Queue
```

必须保证：

> Realtime Pipeline 的阻塞不能影响 Archive Pipeline。

优先级：

```text
Audio Archive
>
Realtime ASR
>
Realtime Analysis
```

必要时：

- ASR 可以跳帧；
- LLM 可以暂时停止；
- 实时分析可以降级；

但音频尽量不能丢。

---

# 8. Archive Pipeline

## 8.1 音频格式

推荐归档格式：

```text
FLAC
48 kHz
Stereo
16 bit 或原始有效位深
```

主要考虑：

- 无损；
- 比 WAV 空间小；
- 后续重新 ASR 不损失质量；
- 适合长期归档。

---

# 8.2 Segment 策略

默认推荐：

```yaml
audio:
  segment_minutes: 120
```

即：

```text
2 小时一个临时 Segment
```

可配置范围建议：

```text
60 ～ 180 分钟
```

不建议再使用 20～30 分钟这种过细分片。

例如六小时直播：

```text
working/
├── segment_000_000000-020000.flac
├── segment_001_020000-040000.flac
└── segment_002_040000-060000.flac
```

---

# 8.3 为什么仍保留 Segment

如果直接连续写一个六小时文件：

```text
live.flac
```

一旦：

- 进程异常退出；
- 系统重启；
- 编码器异常；
- 文件尾未正常写入；

可能影响整场归档。

使用长 Segment 后：

```text
前 2 小时    已完成
第 2～4 小时 已完成
第 4～6 小时 当前写入
```

故障主要影响当前 Segment。

因此：

> Segment 是内部工程容错机制，最终用户仍然只看到一场直播一个文件。

---

# 8.4 Final Audio

直播结束后执行：

```text
Segment Validation
        ↓
Timeline Validation
        ↓
Gap Detection
        ↓
Lossless Concatenate
        ↓
Metadata Write
        ↓
Checksum
        ↓
Final Audio
```

输出：

```text
final/
└── 2026-09-09_UP主_190013-002018.flac
```

如果编码参数完全一致，应优先采用：

```text
无损 concat
```

而不是重新解码、重新编码整个六小时音频。

---

# 8.5 Segment 生命周期

最终文件成功生成并校验后：

```text
Final Audio
   ↓
checksum OK
   ↓
duration OK
   ↓
Timeline OK
```

再决定：

```text
删除 working Segment
```

或者：

```text
保留 N 天作为恢复备份
```

建议默认：

```yaml
audio:
  keep_segments_after_finalize_days: 1
```

确认 Final Audio 无问题后自动删除。

---

# 9. Realtime Audio Pipeline

实时链路：

```text
48k Stereo
    ↓
Downmix
    ↓
Resample
    ↓
16k Mono
    ↓
VAD
    ↓
Streaming ASR
```

16k Mono 仅用于分析。

不会作为正式直播归档文件。

---

# 10. VAD 模块

负责：

- 识别语音区间；
- 跳过静音；
- 降低 ASR 计算量；
- 统计主播持续讲话状态。

输出：

```python
SpeechSegment(
    start_ms,
    end_ms,
    audio
)
```

附加 Feature：

```text
speech_ratio
silence_duration
continuous_speech_duration
```

---

# 11. Streaming ASR

Streaming ASR 用于实时内容理解。

输出：

```json
{
  "start_ms": 5341200,
  "end_ms": 5359400,
  "text": "这里我觉得大家对 Agent Runtime 有一个误区",
  "confidence": 0.91,
  "final": true
}
```

目标：

```text
主体内容可理解
+
时间基本准确
+
延迟尽量低
```

实时结果不作为最终正式文稿。

---

# 12. Transcript Buffer

维护多个滚动窗口：

```text
30 秒
3 分钟
5 分钟
15 分钟
```

用途：

| Window | 用途         |
| ------ | ---------- |
| 30 秒   | 热词、白名单快速检测 |
| 3 分钟   | 当前主题       |
| 5 分钟   | 干货判断       |
| 15 分钟  | 主题连续性      |

不需要将整场直播文本长期放入模型上下文。

---

# 13. Content Analyzer

负责从最近直播内容中抽取：

```json
{
  "topic": "Agent Runtime",

  "whitelist_score": 0.92,
  "blacklist_score": 0.03,

  "information_density": 0.86,
  "structured_speech_score": 0.78,
  "topic_continuity": 0.88,
  "novelty": 0.64,

  "speech_ratio": 0.91
}
```

第一阶段不要求生成向量索引。

如果为了 Topic Continuity 使用 Embedding，可作为算法内部临时能力使用，但：

> 不建设持久化 Vector DB。

---

# 14. 白名单检测

支持：

```yaml
whitelist:

  categories:
    - 人工智能
    - 投资
    - 科技产业

  concepts:
    - Agent
    - 大模型
    - GPU
    - AI Infra

  entities:
    - OpenAI
    - NVIDIA
    - AgentScope
    - OpenJiuwen
```

检测方式可以包括：

```text
关键词
+
别名
+
主题判断
+
必要的语义匹配
```

第一阶段不需要为了白名单单独建设知识库。

---

# 15. 黑名单检测

例如：

```yaml
blacklist:
  - 星座
  - 起名
  - 感情聊天
  - 日常闲聊
  - 粉丝寒暄
```

判断以：

```text
当前主题
```

为主，而不是简单关键词过滤。

例如：

> “刚才有人问星座，我们不聊这个，继续谈 AI。”

不应因为出现“星座”二字被判定为黑名单内容。

---

# 16. Information Density

信息密度主要衡量：

> 主播是否正在输出实质信息。

高密度：

- 明确观点；
- 数据；
- 原因；
- 对比；
- 方法；
- 结论；
- 案例；
- 框架；
- 预测。

低密度：

- 寒暄；
- 玩笑；
- 重复；
- 粉丝互动；
- 无主题聊天。

输出：

```text
information_density ∈ [0,1]
```

---

# 17. Structured Speech Detection

高价值内容经常出现明显的论述结构：

```text
第一……
第二……

核心问题是……

原因是……

这里分成两个部分……

举个例子……

我的判断是……
```

生成：

```text
structured_speech_score
```

用来区分：

```text
聊到某个专业主题
```

和：

```text
正在系统输出该主题
```

---

# 18. Topic Continuity

维护连续主题判断：

```text
topic(t-2)
topic(t-1)
topic(t)
```

例如：

```text
AI Agent
→ Agent Runtime
→ Skill Runtime
→ Workflow
```

属于连续展开。

而：

```text
手机
→ 吃饭
→ 星座
→ 股票
→ AI
```

属于低连续性内容。

---

# 19. Interest Engine

第一版采用简单、可解释的评分模型：

```text
InterestScore =

  0.30 × whitelist_score

- 0.30 × blacklist_score

+ 0.20 × information_density

+ 0.10 × topic_continuity

+ 0.05 × structured_speech_score

+ 0.05 × novelty
```

LLM 可以直接返回最终辅助判断：

```json
{
  "topic": "Agent Runtime",
  "summary": "主播正在系统讨论 Agent Runtime 与 Skill Runtime 的边界。",
  "score": 0.88,
  "is_blacklist_topic": false,
  "is_high_information_density": true
}
```

---

# 20. 两级判定

推荐：

```text
Rule-based Fast Filter
           ↓
       Candidate?
        /       \
      No         Yes
      ↓           ↓
继续监听       LLM Judge
```

规则层负责：

- 明显白名单；
- 明显黑名单；
- 长时间无语音；
- 简单关键词；
- 当前状态。

LLM 负责：

- 主题；
- 内容价值；
- 复杂黑白名单判断；
- 信息密度；
- 当前摘要。

---

# 21. 干货状态机

```text
IDLE
 ↓
CANDIDATE
 ↓
HOT
 ↓
COOLING
 ↓
IDLE
```

## IDLE

普通内容。

## CANDIDATE

疑似进入干货。

## HOT

确认进入高价值内容。

触发：

```text
SEND NOTIFICATION
+
OPEN HIGHLIGHT
```

## COOLING

短暂跑题或互动时暂不立即结束 Highlight。

---

# 22. 推荐初始参数

```yaml
interest:

  candidate_threshold: 0.65

  hot_threshold: 0.78

  leave_hot_threshold: 0.45

  enter_hot_consecutive_windows: 2

  leave_hot_consecutive_windows: 3

  analysis_interval_sec: 60

  semantic_window_sec: 180

  llm_window_sec: 300

  notification_cooldown_sec: 600
```

---

# 23. Notification

提醒示例：

```text
🔥 检测到高价值内容

主播：XXX

主题：
Agent Runtime 与 Skill Runtime

当前摘要：
主播正在讨论 Agent Runtime、Workflow Runtime 和
Skill Runtime 的职责边界。

开始时间：
01:42:30

当前评分：
0.87
```

提醒触发：

```text
CANDIDATE → HOT
```

而不是每分钟提醒。

---

# 24. Highlight

进入 HOT：

```json
{
  "id": "hl_003",
  "start_ms": 6150000,
  "end_ms": null,
  "topic": "Agent Runtime",
  "score": 0.87,
  "status": "OPEN"
}
```

结束后：

```json
{
  "start_ms": 6150000,
  "end_ms": 7632000,
  "topic": "Agent Runtime",
  "status": "CLOSED"
}
```

Highlight 独立于音频 Segment。

即使 Highlight：

```text
01:50 → 02:20
```

跨越两个内部音频 Segment，也不影响其表达。

因为所有时间都基于 Session Timeline。

---

# 25. Offline Finalization

Session 结束后：

```text
Audio Segments
      ↓
Final Audio Merge
      ↓
Complete FLAC
      ↓
Offline VAD
      ↓
High Accuracy ASR
      ↓
Timestamp Alignment
      ↓
Punctuation
      ↓
Terminology Correction
      ↓
Verbatim Transcript
      ↓
Readable Transcript
      ↓
Chaptering
      ↓
Highlight Refinement
      ↓
Session Summary
```

---

# 26. 最终逐字稿

生成两种文稿。

## 26.1 Verbatim Transcript

忠实保留口播。

```text
[01:42:31]

我觉得，嗯，其实 Agent Runtime 这个问题吧，
现在大家可能有一个比较大的误区。
```

主要用于：

- 回听；
- 内容核对；
- 原文引用；
- 对应音频。

---

## 26.2 Readable Transcript

对口播进行有限整理：

- 去除明显无意义语气词；
- 合并口语重复；
- 修复标点；
- 修复明显 ASR 错误；
- 修正专业名词；
- 合理分段。

例如：

```text
我认为目前大家对 Agent Runtime 存在一个比较大的误区。
```

禁止：

- 增加主播没说过的观点；
- 引入外部知识；
- 改变观点强度；
- 将系统总结混入原始正文。

---

# 27. 专有名词修正

维护：

```text
global_dictionary
up_dictionary
session_dictionary
```

例如：

```yaml
AgentScope:
  aliases:
    - agent scope
    - agents cope

OpenJiuwen:
  aliases:
    - open九问
    - open久文
```

离线阶段允许使用：

```text
ASR Hotword
+
Dictionary
+
LLM Context Correction
```

---

# 28. Chaptering

整场直播按主题划分章节。

例如：

```text
00:00:00 - 00:18:20    开场闲聊

00:18:20 - 00:45:10    星座

00:45:10 - 01:42:30    粉丝互动

01:42:30 - 02:07:12    Agent Runtime

02:07:12 - 02:43:50    AI Agent 框架

02:43:50 - 04:20:00    日常聊天
```

每个章节至少包含：

```json
{
  "id": "chapter_004",
  "start_ms": 6150000,
  "end_ms": 7632000,
  "title": "Agent Runtime 与 Skill Runtime",
  "summary": "讨论 Runtime 和 Skill 的职责边界。",
  "keywords": [
    "Agent Runtime",
    "Skill Runtime"
  ]
}
```

---

# 29. 音频与文稿联动

这是第一阶段非常重要的体验。

所有 Transcript：

```json
{
  "start_ms": 6420000,
  "end_ms": 6452000,
  "text": "Agent Runtime主要负责……"
}
```

对应 Final Audio：

```text
20260909_UP主.flac
```

因此可以直接形成：

```text
点击逐字稿
     ↓
播放器 seek(6420s)
     ↓
播放对应原音频
```

Highlight 和 Chapter 同理。

---

# 30. 第一阶段输出物

每场直播完成后最终产生：

```text
session/
│
├── audio/
│   └── live.flac
│
├── transcript/
│   ├── verbatim.jsonl
│   ├── verbatim.md
│   ├── readable.md
│   └── subtitles.srt
│
├── analysis/
│   ├── chapters.json
│   ├── highlights.json
│   └── summary.md
│
└── session.json
```

运行阶段还可以存在：

```text
working/
```

目录，但在 Finalize 完成后可清理。

---

# 31. 文件目录设计

推荐：

```text
sessions/
└── 20260909_190013_xxx/

    ├── session.json

    ├── audio/
    │   ├── final/
    │   │   └── live.flac
    │   │
    │   └── working/
    │       ├── segment_000.flac
    │       ├── segment_001.flac
    │       └── segment_002.flac

    ├── realtime/
    │   ├── transcript.jsonl
    │   ├── analysis.jsonl
    │   └── events.jsonl

    ├── final/
    │   ├── verbatim.jsonl
    │   ├── verbatim.md
    │   ├── readable.md
    │   ├── subtitles.srt
    │   ├── chapters.json
    │   ├── highlights.json
    │   └── summary.md

    └── session.sqlite
```

这里暂不加入：

```text
knowledge/
wiki_export/
embeddings/
vector_index/
```

避免第一阶段架构过度膨胀。

---

# 32. SQLite 数据设计

## sessions

```text
id
room_id
up_name
start_time
end_time
duration_ms
status
final_audio_path
```

---

## audio\_segments

只记录内部 Working Segment：

```text
id
session_id
file_path
start_ms
end_ms
status
checksum
```

---

## audio\_final

```text
session_id
file_path
duration_ms
codec
sample_rate
channels
checksum
created_at
```

---

## realtime\_transcripts

```text
id
session_id
start_ms
end_ms
text
confidence
```

---

## final\_transcripts

```text
id
session_id
start_ms
end_ms
speaker
verbatim_text
readable_text
confidence
```

---

## topic\_segments

```text
id
session_id
start_ms
end_ms
topic
confidence
```

---

## highlights

```text
id
session_id
start_ms
end_ms
topic
summary
score
```

---

## chapters

```text
id
session_id
start_ms
end_ms
title
summary
```

---

## events

```text
id
session_id
timestamp_ms
event_type
payload
```

---

# 33. 配置设计

```yaml
audio:

  archive_sample_rate: 48000
  archive_channels: 2
  archive_codec: flac

  # 内部容错分片
  segment_minutes: 120

  # Session结束后生成单文件
  merge_after_session: true

  # 成功合并后临时segment保留时间
  keep_segments_after_finalize_days: 1


asr:

  realtime:
    sample_rate: 16000
    model: streaming-asr

  offline:
    model: high-accuracy-asr


analysis:

  interval_sec: 60

  fast_window_sec: 30

  topic_window_sec: 180

  llm_window_sec: 300

  context_window_sec: 900


interest:

  whitelist:
    - AI
    - Agent
    - 大模型
    - 投资
    - 行业分析

  blacklist:
    - 星座
    - 起名
    - 情感聊天
    - 日常闲聊

  candidate_threshold: 0.65

  notify_threshold: 0.78

  leave_hot_threshold: 0.45


notification:

  enabled: true

  cooldown_minutes: 10


postprocess:

  verbatim_transcript: true

  readable_transcript: true

  subtitles: true

  chaptering: true

  highlights: true

  session_summary: true
```

---

# 34. 异常恢复

## 34.1 Audio Source 中断

```text
AudioSource异常
    ↓
记录 GAP
    ↓
尝试重新连接
    ↓
继续当前 Session
```

必须记录：

```json
{
  "event_type": "AUDIO_GAP",
  "start_ms": 6200000,
  "end_ms": 6218000
}
```

最终文稿不得假装这段内容完整存在。

---

# 34.2 ASR 崩溃

优先策略：

```text
Archive继续
Realtime ASR重启
```

因为最终文稿可以：

```text
完整音频
↓
Offline ASR
```

重新恢复。

---

# 34.3 LLM 不可用

降级：

```text
关键词
+
主题规则
+
基础评分
```

继续进行低精度实时判断。

不影响：

- 音频；
- ASR；
- 最终归档。

---

# 34.4 Archive Writer 异常

这是高优先级故障。

应：

```text
立即尝试重新打开
↓
创建新的 Segment
↓
记录异常
```

而不能因为“原 Segment 写入失败”导致整个 Session 直接退出。

---

# 34.5 整体进程异常

恢复：

```text
读取Session状态
      ↓
读取最后完成Segment
      ↓
检查未完成Segment
      ↓
根据情况修复/保留
      ↓
创建新Segment
      ↓
继续Session
```

---

# 35. Final Audio 校验

生成最终音频后必须校验：

## Duration Check

```text
Final Duration
≈
Session Duration - Recorded Gaps
```

## Segment Coverage

所有成功 Segment 应连续覆盖相应时间范围。

## Decode Test

确认整个 Final Audio 可正常解码。

## Checksum

生成：

```text
SHA-256
```

写入 Session metadata。

只有以上检查通过：

```text
audio_finalize_status = COMPLETED
```

才能清理 Working Segment。

---

# 36. 性能目标

## 实时 ASR

目标：

```text
< 10 秒延迟
```

## 内容判断

目标：

```text
30～60 秒一个分析周期
```

## 高价值提醒

目标：

```text
进入稳定干货内容后约 1～2 分钟提醒
```

对于连续一个小时的高价值内容，这一延迟可以接受。

系统不追求：

```text
主播刚说第一个字
→
立即提醒
```

更看重降低误报。

---

# 37. 资源控制

对于一场可能持续：

```text
4～8 小时
```

甚至更长的直播，系统必须支持长期运行。

主要资源：

```text
Audio Capture
低

FLAC Encode
低

Streaming ASR
中

VAD
低

LLM
间歇调用
```

LLM 不应持续消费完整直播流。

---

# 38. 数据完整性

优先级：

```text
P0 完整音频

P1 Final Transcript

P2 Highlight / Chapter

P3 Realtime Transcript

P4 Realtime Feature / Debug 信息
```

发生资源不足时应按反向顺序降级。

---

# 39. 第一阶段暂不实现

第一阶段明确排除：

- 自动控制 B 站播放器；
- DRM 处理；
- 视频录制；
- 多机器分布式执行；
- Vector DB；
- 全文搜索系统；
- 语义搜索系统；
- 混合搜索；
- RAG；
- 跨直播知识检索；
- 自动知识图谱；
- `llm-wiki` 深度集成；
- 自动发布；
- 自动剪辑视频；
- 完整 Web 管理后台。

---

# 40. llm-wiki 的第一阶段定位

第一阶段只保留非常轻量的扩展接口。

例如：

```text
Final Transcript
+
Chapters
+
Highlights
+
Summary
```

可以未来导出成：

```text
Markdown / JSON
```

供 `llm-wiki` 消费。

但 V1 不要求：

```text
直播结束
↓
自动构建Wiki
↓
自动Embedding
↓
自动索引
```

也不将 `llm-wiki` 是否正常运行作为 Session 完成条件。

系统边界保持为：

```text
Live Sentinel
    ↓
标准化归档文件
```

未来再增加：

```text
Optional Wiki Adapter
```

---

# 41. V1 完整流程

```text
观众端直播音频
       ↓
Audio Source
       ↓
Audio Tee
   ┌───┴────────────┐
   │                │
   ▼                ▼
Archive          Realtime
   │                │
1～2h Segment       VAD
   │                ↓
   │           Streaming ASR
   │                ↓
   │          Transcript Buffer
   │                ↓
   │            Rule Filter
   │                ↓
   │             LLM Judge
   │                ↓
   │          Interest State
   │                ↓
   │           Notification
   │
直播结束
   ↓
Segment Finalize
   ↓
完整单文件 FLAC
   ↓
Offline ASR
   ↓
Verbatim Transcript
   ↓
Readable Transcript
   ↓
Chaptering
   ↓
Highlight Refinement
   ↓
Session Summary
   ↓
完成归档
```

---

# 42. 推荐模块拆分

```text
live_sentinel/

├── session/
│   ├── manager.py
│   ├── lifecycle.py
│   └── recovery.py

├── audio/
│   ├── source.py
│   ├── tee.py
│   ├── archive.py
│   ├── segment.py
│   ├── finalizer.py
│   └── resample.py

├── asr/
│   ├── realtime.py
│   ├── offline.py
│   └── vad.py

├── transcript/
│   ├── buffer.py
│   ├── corrector.py
│   └── formatter.py

├── analysis/
│   ├── whitelist.py
│   ├── blacklist.py
│   ├── topic.py
│   ├── density.py
│   ├── continuity.py
│   └── llm_judge.py

├── interest/
│   ├── scorer.py
│   └── state_machine.py

├── notification/
│   └── notifier.py

├── finalizer/
│   ├── pipeline.py
│   ├── chaptering.py
│   ├── highlights.py
│   └── summary.py

├── storage/
│   ├── sqlite.py
│   └── models.py

└── config/
    └── config.yaml
```

第一阶段暂不建设：

```text
knowledge/
index/
integrations/llm_wiki/
```

---

# 43. 核心伪代码

```python
def run_session():

    session = session_manager.create()

    audio_source.start()
    archive_writer.start(session)

    while session.running:

        frame = audio_source.read()

        # P0：完整音频归档
        archive_writer.write(frame)

        # 实时分析
        realtime_audio = realtime_preprocessor.process(frame)

        for speech in vad.process(realtime_audio):

            result = realtime_asr.transcribe(speech)

            realtime_store.append(result)

            transcript_buffer.append(result)

        if analysis_scheduler.should_run():

            context = transcript_buffer.last(minutes=5)

            features = analyzer.analyze(context)

            if judge_policy.need_llm(features):

                semantic_result = llm_judge.evaluate(
                    context=context,
                    features=features
                )

                score = interest_engine.score(
                    features,
                    semantic_result
                )

            else:

                score = interest_engine.score(features)

            event = state_machine.update(score)

            if event == ENTER_HOT:

                highlight_manager.open(...)

                notifier.send(...)

            elif event == LEAVE_HOT:

                highlight_manager.close(...)

    # 停止采集
    audio_source.stop()

    archive_writer.close()

    # 1～2小时Segment → 一场直播一个音频
    final_audio = audio_finalizer.finalize(session)

    # 基于完整音频重新精加工
    offline_finalizer.run(
        session=session,
        audio=final_audio
    )

    session_manager.complete(session)
```

---

# 44. Audio Finalizer 伪代码

```python
def finalize_audio(session):

    segments = segment_store.list_completed(
        session.id
    )

    validate_timeline(segments)

    final_path = build_final_path(session)

    lossless_concat(
        segments=segments,
        output=final_path
    )

    info = probe_audio(final_path)

    validate_duration(
        session=session,
        audio_info=info
    )

    checksum = sha256(final_path)

    store_final_audio(
        session_id=session.id,
        file_path=final_path,
        checksum=checksum,
        duration_ms=info.duration_ms
    )

    return final_path
```

---

# 45. 最终交付视角

对使用者而言，每场直播只需要看到：

```text
2026-09-09_UP主/
│
├── 直播完整音频.flac
│
├── 忠实逐字稿.md
│
├── 阅读版文稿.md
│
├── 字幕.srt
│
├── 章节.json
│
├── 干货片段.json
│
└── 直播摘要.md
```

而：

```text
Segment
实时临时Transcript
Feature日志
Checkpoint
```

全部属于系统内部实现细节。

---

# 46. 后续演进

第一阶段稳定之后，再考虑：

```text
Phase 2
│
├── Transcript 全文检索
├── 跨直播搜索
├── llm-wiki同步
└── 基础内容浏览界面

Phase 3
│
├── Embedding
├── 语义搜索
├── Hybrid Retrieval
├── Topic Note
└── RAG

Phase 4
│
├── 跨直播知识融合
├── 长期主题追踪
├── 观点演化分析
└── 个人知识库
```

这样避免把一个首先需要解决：

> **“什么时候值得回来听直播？”**

的问题，在第一阶段就变成一个大型知识库工程。

---

# 47. 总结

第一阶段系统可以归纳为四层：

```text
采集层
Audio Capture
      │
      ▼
归档层
Long Segment
→ Final Single Audio
      │
      ▼
实时理解层
VAD
→ Streaming ASR
→ Interest Detection
→ Notification
      │
      ▼
离线加工层
Offline ASR
→ Transcript
→ Chapter
→ Highlight
→ Summary
```

最重要的设计原则调整为：

### 第一，最终必须是一场直播一个完整音频文件

Segment 只作为内部容错手段。

### 第二，Segment 粒度采用小时级

推荐默认：

```text
2 小时
```

最低建议：

```text
1 小时
```

而不是频繁生成几十分钟的小文件。

### 第三，完整直播音频是 Source of Truth

所有文本和分析结果都可以重新生成。

### 第四，实时链路与最终归档解耦

实时链路故障不得破坏完整音频。

### 第五，第一阶段不建设知识检索系统

`llm-wiki`、全文检索、Embedding、Vector Search 和 RAG 全部留到后续阶段。

第一阶段只需要把：

```text
完整音频
+
完整文稿
+
章节
+
Highlight
+
实时提醒
```

这一闭环做到稳定可靠。
