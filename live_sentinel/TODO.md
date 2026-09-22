# 后续待办

## ASR 模型评估

- [x] 为 macOS 26+ 实现 Apple `SpeechTranscriber` Swift 辅助程序和 Python
  `AppleSpeechCLIStreamingASR` 适配器；2026-09-21 使用
  `BV1gx411R7ke` 前五分钟中的 24 个生产同款 VAD 段与
  `paraformer-realtime-v2` 做同源对照，双方 24/24 成功，报告位于
  `reports/asr-shadow/BV1gx411R7ke/run-20260921/`。该结果没有人工参考稿，
  只能比较延迟、稳定性和字符一致度，不能当作准确率结论。
- [x] 2026-09-22 将本机生产实时转写切换为 Apple `SpeechTranscriber`；实时阶段只
  要求语义足以供规则/LLM 判断，完整逐字稿仍由本地 SenseVoice 离线生成。

- [ ] 对 `qwen3-asr-flash-realtime`、`qwen-audio-3.0-asr-flash-streaming`、
  `fun-asr-flash-8k-realtime` 与 `paraformer-realtime-v2` 做同一段中文直播音频
  的延迟、断句、术语/方言和成本对比；记录模型版本、地域、请求协议和计费单位。
- [ ] 核验 Qwen3-ASR 的 WebSocket 事件协议、可用语言/热词、失败重连语义，
  再决定是否新增 `DashScopeQwenRealtimeStreamingASR`，不要只依据模型市场卡片的
  名称或价格替换生产适配器。
- [ ] 对本地 FunASR（SenseVoice / Paraformer）与云端模型做离线 WER/术语召回抽样，
  保留同一份 5 分钟 Final Audio、模型配置和可复现实验输出。
- [ ] 每次模型切换前重新核验官方模型页、API 参考、地域和价格；候选模型未通过
  真实 smoke 与回归样本前，不提升为默认 provider。

当前本机生产基线：实时使用 Apple `SpeechTranscriber`；本地 FunASR/SenseVoice
负责离线完整回填，默认以任务生命周期管理。DashScope Paraformer 保留为可配置的
兼容/回退候选，具体见 `README.md`。
