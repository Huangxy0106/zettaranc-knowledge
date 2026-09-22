from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from scripts.import_bilibili_cookie import import_cookie


class BilibiliCookieImportTests(unittest.TestCase):
    def test_import_preserves_other_credentials_and_uses_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text(
                "OTHER_SECRET=keep\nBILIBILI_COOKIE=old\n",
                encoding="utf-8",
            )
            names = import_cookie(
                env_file,
                "SESSDATA=session; DedeUserID=123; bili_jct=csrf",
            )
            content = env_file.read_text(encoding="utf-8")
            self.assertIn("OTHER_SECRET=keep", content)
            self.assertIn("BILIBILI_COOKIE=SESSDATA=session", content)
            self.assertNotIn("BILIBILI_COOKIE=old", content)
            self.assertEqual(names, {"SESSDATA", "DedeUserID", "bili_jct"})
            self.assertEqual(os.stat(env_file).st_mode & 0o777, 0o600)

    def test_import_rejects_incomplete_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            with self.assertRaisesRegex(ValueError, "缺少必要字段"):
                import_cookie(env_file, "SESSDATA=session")


if __name__ == "__main__":
    unittest.main()
