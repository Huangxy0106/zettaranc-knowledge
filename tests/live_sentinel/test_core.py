from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from live_sentinel.audio.resample import (
    RealtimeAudioPreprocessor,
    downmix_pcm_s16le,
    resample_pcm_s16le,
)
from live_sentinel.audio.segment import CompletedSegment, validate_timeline
from live_sentinel.audio.tee import AudioTee
from live_sentinel.audio.source import DurationLimitedAudioSource, IterableAudioSource
from live_sentinel.asr.vad import EnergyVAD
from live_sentinel.config import InterestConfig, load_config
from live_sentinel.interest.scorer import InterestScorer
from live_sentinel.interest.state_machine import InterestStateMachine
from live_sentinel.analysis.analyzer import RuleBasedContentAnalyzer
from live_sentinel.models import AnalysisFeatures, AudioFrame, InterestState, TranscriptSegment
from live_sentinel.transcript.buffer import TranscriptBuffer
from live_sentinel.session.checkpoint import Checkpoint, CheckpointStore


class CoreTests(unittest.TestCase):
    def test_interest_formula_markers_and_tuning_are_configurable(self) -> None:
        analyzer = RuleBasedContentAnalyzer(
            ["目标"],
            [],
            structure_markers=["拆解"],
            density_markers=["证据"],
            tuning={
                "density_marker_weight": 1.0,
                "density_number_weight": 0.0,
                "long_text_bonus": 0.0,
                "structure_marker_weight": 1.0,
            },
        )
        features = analyzer.analyze([TranscriptSegment(0, 1000, "目标 拆解 证据")])
        self.assertEqual(features.information_density, 1.0)
        self.assertEqual(features.structured_speech_score, 1.0)

    def test_load_json_config(self) -> None:
        config_path = Path(__file__).parents[2] / "live_sentinel" / "config.example.json"
        config = load_config(config_path)
        self.assertEqual(config.audio.segment_minutes, 120)
        self.assertIn("AI", config.interest.whitelist)
        self.assertEqual(config.bilibili_capture.offline_confirmations, 3)

    def test_checkpoint_store_is_readable_after_atomic_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "checkpoint.json")
            checkpoint = Checkpoint("s1", 1234).with_timestamp(1234)
            store.save(checkpoint)
            loaded = store.load()
            self.assertEqual(loaded.session_id, "s1")
            self.assertEqual(loaded.last_timestamp_ms, 1234)

    def test_audio_frame_and_preprocessor(self) -> None:
        # stereo: [1000, -1000], [2000, 0]
        pcm = (1000).to_bytes(2, "little", signed=True)
        pcm += (-1000).to_bytes(2, "little", signed=True)
        pcm += (2000).to_bytes(2, "little", signed=True)
        pcm += (0).to_bytes(2, "little", signed=True)
        self.assertEqual(len(AudioFrame(0, pcm, 2, 2).pcm), 8)
        mono = downmix_pcm_s16le(pcm, 2)
        self.assertEqual(
            [int.from_bytes(mono[i : i + 2], "little", signed=True) for i in (0, 2)],
            [0, 1000],
        )
        self.assertEqual(len(resample_pcm_s16le(mono, 2, 4)), 8)
        prepared = RealtimeAudioPreprocessor(4).process(AudioFrame(0, pcm, 2, 2))
        self.assertEqual((prepared.sample_rate, prepared.channels), (4, 1))

    def test_duration_limited_source_trims_last_frame(self) -> None:
        source = DurationLimitedAudioSource(
            IterableAudioSource(
                [
                    AudioFrame(0, b"\x01\x00" * 100, 1000, 1, 2),
                    AudioFrame(100, b"\x01\x00" * 100, 1000, 1, 2),
                ]
            ),
            150,
        )
        source.start()
        first = source.read()
        second = source.read()
        self.assertEqual(first.end_ms, 100)
        self.assertEqual(second.end_ms, 150)
        self.assertIsNone(source.read())

    def test_energy_vad_caps_continuous_speech_for_realtime_latency(self) -> None:
        vad = EnergyVAD(rms_threshold=100, silence_ms=1_000, max_segment_ms=200)
        emitted = []
        for index in range(4):
            emitted.extend(
                vad.process(AudioFrame(index * 100, b"\xe8\x03" * 100, 1000, 1, 2))
            )
        self.assertEqual(
            [(item.start_ms, item.end_ms) for item in emitted],
            [(0, 200), (200, 400)],
        )

    def test_buffer_replaces_partial_and_rolls(self) -> None:
        buffer = TranscriptBuffer(max_seconds=3)
        buffer.append(TranscriptSegment(0, 1000, "partial", final=False))
        buffer.append(TranscriptSegment(0, 1000, "final"))
        buffer.append(TranscriptSegment(2500, 3500, "new"))
        self.assertEqual([x.text for x in buffer.all()], ["final", "new"])
        self.assertEqual([x.text for x in buffer.last(1, now_ms=3500)], ["new"])

    def test_tee_preserves_archive_when_realtime_is_full(self) -> None:
        tee = AudioTee(archive_maxsize=2, realtime_maxsize=1)
        frame = AudioFrame(0, b"\x00\x00", 1, 1)
        tee.publish(frame)
        tee.get_archive()
        tee.publish(frame)
        tee.get_archive()
        tee.publish(frame)
        tee.get_archive()
        self.assertEqual(tee.stats.published, 3)
        self.assertEqual(tee.stats.realtime_dropped, 2)

    def test_state_machine_and_score(self) -> None:
        config = InterestConfig(
            candidate_threshold=0.5,
            hot_threshold=0.8,
            leave_hot_threshold=0.3,
            enter_hot_consecutive_windows=2,
            leave_hot_consecutive_windows=2,
            notification_cooldown_sec=0,
        )
        features = AnalysisFeatures(
            whitelist_score=1,
            blacklist_score=0,
            information_density=1,
            topic_continuity=1,
            structured_speech_score=1,
            novelty=1,
        )
        self.assertAlmostEqual(InterestScorer(config).score(features), 0.70)
        machine = InterestStateMachine("s1", config)
        self.assertEqual(machine.update(0, 0.6).state, InterestState.CANDIDATE)
        self.assertIsNone(machine.update(1000, 0.9))
        hot = machine.update(2000, 0.9, "AI", "summary")
        self.assertEqual(hot.event_type, "ENTER_HOT")
        self.assertTrue(hot.payload["notify"])
        self.assertEqual(machine.active_highlight.topic, "AI")
        self.assertIsNone(machine.update(3000, 0.2))
        cooling = machine.update(4000, 0.2)
        self.assertEqual(cooling.event_type, "ENTER_COOLING")
        leave = machine.update(5000, 0.2)
        self.assertEqual(leave.event_type, "LEAVE_HOT")
        self.assertIsNone(machine.active_highlight)

    def test_timeline_reports_gaps_and_rejects_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "a"
            second = Path(directory) / "b"
            first.write_bytes(b"a")
            second.write_bytes(b"b")
            ordered, gaps = validate_timeline(
                [
                    CompletedSegment("b", second, 200, 300),
                    CompletedSegment("a", first, 0, 100),
                ]
            )
            self.assertEqual([item.id for item in ordered], ["a", "b"])
            self.assertEqual([(gap.start_ms, gap.end_ms) for gap in gaps], [(100, 200)])
            with self.assertRaises(ValueError):
                validate_timeline(
                    [CompletedSegment("a", first, 0, 150), CompletedSegment("b", second, 100, 300)]
                )
