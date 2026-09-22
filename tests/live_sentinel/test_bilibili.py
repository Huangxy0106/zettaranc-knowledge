from __future__ import annotations

import unittest
from unittest.mock import patch

from live_sentinel.bilibili import (
    _request_headers,
    _select_stream_info,
    _select_stream_url,
    _stream_url,
)


class BilibiliStreamResolverTests(unittest.TestCase):
    @staticmethod
    def _modern_payload(playurl):
        return {
            "code": 0,
            "data": {
                "playurl_info": {
                    "playurl": playurl,
                }
            },
        }

    def test_modern_http_flv_prefers_avc_without_fallback(self) -> None:
        playurl = {
            "stream": [
                {
                    "protocol_name": "http_stream",
                    "format": [
                        {
                            "format_name": "flv",
                            "codec": [
                                {
                                    "codec_name": "hevc",
                                    "base_url": "/hevc.flv",
                                    "url_info": [{"host": "https://cdn.test", "extra": "?a=1"}],
                                },
                                {
                                    "codec_name": "avc",
                                    "base_url": "/avc.flv",
                                    "url_info": [{"host": "https://cdn.test", "extra": "?a=2"}],
                                },
                            ],
                        }
                    ],
                }
            ]
        }
        with patch(
            "live_sentinel.bilibili._fetch_json",
            return_value=self._modern_payload(playurl),
        ) as fetch:
            self.assertEqual(_stream_url(123, 20), "https://cdn.test/avc.flv?a=2")
        self.assertEqual(fetch.call_count, 1)

    def test_empty_modern_playurl_falls_back_to_legacy_durl(self) -> None:
        responses = [
            self._modern_payload(None),
            {
                "code": 0,
                "data": {
                    "durl": [
                        {"order": 2, "url": "https://backup.test/live.flv"},
                        {"order": 1, "url": "https://primary.test/live.flv"},
                    ]
                },
            },
        ]
        with patch.dict("os.environ", {}, clear=True), patch(
            "live_sentinel.bilibili._fetch_json", side_effect=responses
        ) as fetch:
            self.assertEqual(_stream_url(123, 20), "https://primary.test/live.flv")
        self.assertIn("getRoomPlayInfo", fetch.call_args_list[0].args[0])
        self.assertIn("Room/playUrl", fetch.call_args_list[1].args[0])

    def test_authenticated_page_supports_hls_fmp4(self) -> None:
        hls = {
            "stream": [
                {
                    "protocol_name": "http_hls",
                    "format": [
                        {
                            "format_name": "fmp4",
                            "codec": [
                                {
                                    "codec_name": "avc",
                                    "base_url": "/index.m3u8",
                                    "url_info": [
                                        {"host": "https://live.test", "extra": "?token=ok"}
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        with patch.dict("os.environ", {"BILIBILI_COOKIE": "SESSDATA=secret"}), patch(
            "live_sentinel.bilibili._fetch_json",
            side_effect=[self._modern_payload(None)],
        ), patch(
            "live_sentinel.bilibili._fetch_text",
            return_value=(
                "<script>window.__NEPTUNE_IS_MY_WAIFU__="
                + __import__("json").dumps(
                    {"roomInitRes": {"data": {"playurl_info": {"playurl": hls}}}}
                )
                + ";</script>"
            ),
        ):
            self.assertEqual(_stream_url(123, 20), "https://live.test/index.m3u8?token=ok")

    def test_cookie_header_rejects_newlines(self) -> None:
        with patch.dict("os.environ", {"BILIBILI_COOKIE": "SESSDATA=a\nInjected: yes"}):
            with self.assertRaisesRegex(RuntimeError, "不能包含换行符"):
                _request_headers()

    def test_http_flv_is_preferred_over_hls(self) -> None:
        playurl = {
            "stream": [
                {
                    "protocol_name": "http_hls",
                    "format": [
                        {
                            "format_name": "fmp4",
                            "codec": [{"codec_name": "avc", "base_url": "/hls", "url_info": [{"host": "https://cdn.test", "extra": ""}]}],
                        }
                    ],
                },
                {
                    "protocol_name": "http_stream",
                    "format": [
                        {
                            "format_name": "flv",
                            "codec": [{"codec_name": "avc", "base_url": "/flv", "url_info": [{"host": "https://cdn.test", "extra": ""}]}],
                        }
                    ],
                },
            ]
        }
        self.assertEqual(_select_stream_url(playurl), "https://cdn.test/flv")

    def test_drm_metadata_is_preserved_for_system_audio_fallback(self) -> None:
        playurl = {
            "stream": [
                {
                    "protocol_name": "http_hls",
                    "format": [
                        {
                            "format_name": "fmp4",
                            "codec": [
                                {
                                    "codec_name": "avc",
                                    "drm": True,
                                    "base_url": "/drm.m3u8",
                                    "url_info": [{"host": "https://cdn.test", "extra": ""}],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        selected = _select_stream_info(playurl)
        self.assertTrue(selected.drm)
        self.assertEqual(selected.url, "https://cdn.test/drm.m3u8")

    def test_both_endpoints_fail_with_combined_diagnostic(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch(
            "live_sentinel.bilibili._fetch_json",
            side_effect=[
                self._modern_payload(None),
                {"code": 19001012, "message": "upstream error", "data": {}},
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "新版.*备用"):
                _stream_url(123, 20)


if __name__ == "__main__":
    unittest.main()
