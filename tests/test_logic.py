"""
Unit-тесты для logic.py.
Запуск: python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock

# ── Фейковые env + заглушки пакетов до импорта logic ─────────────
os.environ.setdefault("SECRET_TOKEN",   "test_token")
os.environ.setdefault("BITRIX_API_URL", "https://example.com/api")
os.environ.setdefault("GEMINI_API_KEY", "test_key")

def _mock_pkg(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod

if 'google' not in sys.modules:
    g   = _mock_pkg('google')
    gai = _mock_pkg('google.genai')
    gai.Client = MagicMock()
    g.genai    = gai

if 'deep_translator' not in sys.modules:
    dt = _mock_pkg('deep_translator')
    dt.GoogleTranslator = MagicMock()

from core import logic
from core.logic import (
    clean_tg_url,
    inject_font_size,
    validate_publication,
    with_retry,
    save_draft,
    load_draft,
    clear_draft,
    check_duplicate,
    log_publication,
)


# ─────────────────────────────────────────────
#  clean_tg_url
# ─────────────────────────────────────────────

class TestCleanTgUrl(unittest.TestCase):

    def test_strips_query_params(self):
        self.assertEqual(
            clean_tg_url("https://t.me/channel/123?embed=1"),
            "https://t.me/channel/123",
        )

    def test_strips_single_param(self):
        self.assertEqual(
            clean_tg_url("https://t.me/channel/123?single"),
            "https://t.me/channel/123",
        )

    def test_no_params_unchanged(self):
        url = "https://t.me/channel/123"
        self.assertEqual(clean_tg_url(url), url)

    def test_preserves_path(self):
        self.assertEqual(
            clean_tg_url("https://t.me/DOLOMIT_RUBA/42?foo=bar"),
            "https://t.me/DOLOMIT_RUBA/42",
        )


# ─────────────────────────────────────────────
#  inject_font_size
# ─────────────────────────────────────────────

class TestInjectFontSize(unittest.TestCase):

    def test_wraps_p_in_span(self):
        result = inject_font_size("<p>Текст абзаца</p>")
        self.assertIn("font-size: 15pt;", result)
        self.assertIn("Текст абзаца", result)

    def test_skips_empty_p(self):
        self.assertEqual(inject_font_size("<p>  </p>").strip(), "")

    def test_removes_old_font_size_spans(self):
        html   = '<p><span style="font-size: 12pt;">Текст</span></p>'
        result = inject_font_size(html)
        self.assertIn("15pt", result)
        self.assertNotIn("12pt", result)

    def test_multiple_paragraphs(self):
        html   = "<p>Первый</p><p>Второй</p>"
        result = inject_font_size(html)
        self.assertEqual(result.count("15pt"), 2)

    def test_output_structure(self):
        result = inject_font_size("<p>Привет</p>")
        self.assertRegex(
            result,
            r'<p>\s*<span style="font-size: 15pt;">Привет</span>\s*</p>',
        )


# ─────────────────────────────────────────────
#  validate_publication
# ─────────────────────────────────────────────

class TestValidatePublication(unittest.TestCase):

    def _call(self, **overrides):
        params = dict(
            ru_title="Заголовок новости.",
            ru_preview="Краткий анонс.",
            ru_text="<p>Полный текст новости.</p>",
            preview_image_url="https://example.com/photo.jpg",
        )
        params.update(overrides)
        return validate_publication(**params)

    def test_valid_data_returns_empty(self):
        self.assertEqual(self._call(), [])

    def test_empty_title_caught(self):
        issues = self._call(ru_title="")
        self.assertTrue(any("заголовок" in i.lower() for i in issues))

    def test_empty_text_caught(self):
        issues = self._call(ru_text="")
        self.assertTrue(any("текст" in i.lower() for i in issues))

    def test_html_in_title_caught(self):
        issues = self._call(ru_title="<b>Заголовок</b>")
        self.assertTrue(any("HTML" in i for i in issues))

    def test_preview_too_long(self):
        issues = self._call(ru_preview="А" * 200)
        self.assertTrue(any("симв" in i or "длинный" in i.lower() for i in issues))

    def test_no_photo_caught(self):
        issues = self._call(preview_image_url=None)
        self.assertTrue(any("фото" in i.lower() for i in issues))

    def test_multiple_issues_counted(self):
        issues = self._call(ru_title="", ru_text="", preview_image_url=None)
        self.assertGreaterEqual(len(issues), 3)


# ─────────────────────────────────────────────
#  with_retry
# ─────────────────────────────────────────────

class TestWithRetry(unittest.TestCase):

    def test_success_first_try(self):
        fn = MagicMock(return_value=42)
        self.assertEqual(with_retry(fn, retries=2, delay=0), 42)
        self.assertEqual(fn.call_count, 1)

    def test_retries_connection_error(self):
        import requests
        fn = MagicMock(side_effect=[
            requests.ConnectionError(), requests.ConnectionError(), 99
        ])
        self.assertEqual(with_retry(fn, retries=2, delay=0), 99)
        self.assertEqual(fn.call_count, 3)

    def test_raises_after_all_retries(self):
        import requests
        fn = MagicMock(side_effect=requests.ConnectionError())
        with self.assertRaises(requests.ConnectionError):
            with_retry(fn, retries=2, delay=0)
        self.assertEqual(fn.call_count, 3)

    def test_non_retryable_propagates_immediately(self):
        fn = MagicMock(side_effect=ValueError("bad"))
        with self.assertRaises(ValueError):
            with_retry(fn, retries=2, delay=0)
        self.assertEqual(fn.call_count, 1)


# ─────────────────────────────────────────────
#  Черновик
# ─────────────────────────────────────────────

class TestDraft(unittest.TestCase):

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(path)
        self._path = path
        self._orig = logic.DRAFT_FILE
        logic.DRAFT_FILE = path

    def tearDown(self):
        logic.DRAFT_FILE = self._orig
        if os.path.exists(self._path):
            os.remove(self._path)

    def test_save_and_load(self):
        data = {"tg_url": "https://t.me/ch/1", "data": {"ru": {"title": "Test"}}}
        save_draft(data)
        loaded = load_draft()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["tg_url"], "https://t.me/ch/1")
        self.assertEqual(loaded["data"]["ru"]["title"], "Test")

    def test_load_none_when_missing(self):
        self.assertIsNone(load_draft())

    def test_clear_removes_file(self):
        save_draft({"x": 1})
        clear_draft()
        self.assertFalse(os.path.exists(self._path))

    def test_load_none_on_corrupt_file(self):
        with open(self._path, "w") as f:
            f.write("not json {{{{")
        self.assertIsNone(load_draft())


# ─────────────────────────────────────────────
#  ЛОГ ПУБЛИКАЦИЙ
# ─────────────────────────────────────────────

class TestPublicationLog(unittest.TestCase):

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        os.remove(path)
        self._path = path
        self._orig = logic.LOG_FILE
        logic.LOG_FILE = path

    def tearDown(self):
        logic.LOG_FILE = self._orig
        if os.path.exists(self._path):
            os.remove(self._path)

    def test_new_url_not_duplicate(self):
        self.assertFalse(check_duplicate("https://t.me/ch/999"))

    def test_logged_url_is_duplicate(self):
        log_publication("https://t.me/ch/1", "Title", 42)
        self.assertTrue(check_duplicate("https://t.me/ch/1"))

    def test_different_url_not_duplicate(self):
        log_publication("https://t.me/ch/1", "Title", 42)
        self.assertFalse(check_duplicate("https://t.me/ch/2"))

    def test_multiple_entries(self):
        log_publication("https://t.me/ch/1", "A", 1)
        log_publication("https://t.me/ch/2", "B", 2)
        self.assertTrue(check_duplicate("https://t.me/ch/1"))
        self.assertTrue(check_duplicate("https://t.me/ch/2"))
        self.assertFalse(check_duplicate("https://t.me/ch/3"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
