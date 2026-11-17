"""
Unit-тесты для core/logic.py.
Запуск: python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

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

from core import logic  # noqa: E402
from core.logic import (  # noqa: E402
    clean_tg_url,
    parse_tg_urls,
    inject_font_size,
    validate_publication,
    with_retry,
    save_draft,
    load_draft,
    clear_draft,
    check_duplicate,
    find_duplicates,
    log_publication,
    similarity,
    preview_too_similar,
    fit_preview,
    fetch_posts,
    enhance_with_ai,
    translate_to_belarusian,
)


class FakeApiError(Exception):
    """Похож на google.genai.errors.APIError: у него есть .code."""
    def __init__(self, code):
        super().__init__(f"api error {code}")
        self.code = code


def _resp(text):
    r = MagicMock()
    r.text = text
    return r


# ─────────────────────────────────────────────
#  clean_tg_url / parse_tg_urls
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


class TestParseTgUrls(unittest.TestCase):

    def test_one_url(self):
        self.assertEqual(parse_tg_urls("https://t.me/ch/1"), ["https://t.me/ch/1"])

    def test_lines_spaces_and_commas(self):
        raw = "https://t.me/ch/1\nhttps://t.me/ch/2, https://t.me/ch/3  https://t.me/ch/4"
        self.assertEqual(
            parse_tg_urls(raw),
            [f"https://t.me/ch/{i}" for i in (1, 2, 3, 4)],
        )

    def test_dedup_keeps_order(self):
        raw = "https://t.me/ch/2\nhttps://t.me/ch/1?single\nhttps://t.me/ch/2/"
        self.assertEqual(parse_tg_urls(raw), ["https://t.me/ch/2", "https://t.me/ch/1"])

    def test_without_scheme(self):
        self.assertEqual(parse_tg_urls("t.me/ch/5"), ["https://t.me/ch/5"])

    def test_ignores_channel_without_post_id(self):
        self.assertEqual(parse_tg_urls("https://t.me/ch"), [])

    def test_empty_and_garbage(self):
        self.assertEqual(parse_tg_urls(""), [])
        self.assertEqual(parse_tg_urls("просто текст"), [])


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

    def test_valid_active_from(self):
        self.assertEqual(self._call(active_from="15.03.2026 09:30:00"), [])

    def test_invalid_active_from_caught(self):
        issues = self._call(active_from="2026-03-15")
        self.assertTrue(any("активности" in i.lower() for i in issues))

    def test_empty_active_from_ignored(self):
        self.assertEqual(self._call(active_from=""), [])


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
        data = {"tg_urls": ["https://t.me/ch/1"], "data": {"ru": {"title": "Test"}}}
        save_draft(data)
        loaded = load_draft()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["tg_urls"], ["https://t.me/ch/1"])
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

    def test_merged_news_logs_every_url(self):
        log_publication(["https://t.me/ch/1", "https://t.me/ch/2"], "Merged", 7)
        self.assertTrue(check_duplicate("https://t.me/ch/1"))
        self.assertTrue(check_duplicate("https://t.me/ch/2"))
        self.assertFalse(check_duplicate("https://t.me/ch/3"))
        # одна новость — одна строка в истории
        self.assertEqual(len(logic.read_publications()), 1)

    def test_url_prefix_is_not_duplicate(self):
        log_publication("https://t.me/ch/12", "A", 1)
        self.assertFalse(check_duplicate("https://t.me/ch/1"))

    def test_find_duplicates(self):
        log_publication(["https://t.me/ch/1", "https://t.me/ch/2"], "M", 1)
        self.assertEqual(
            find_duplicates(["https://t.me/ch/2", "https://t.me/ch/5"]),
            ["https://t.me/ch/2"],
        )

    def test_find_duplicates_without_file(self):
        self.assertEqual(find_duplicates(["https://t.me/ch/1"]), [])


# ─────────────────────────────────────────────
#  Загрузка постов и объединение
# ─────────────────────────────────────────────

class TestFetchPostData(unittest.TestCase):

    def test_inline_tags_do_not_break_sentences(self):
        html = (
            '<div class="tgme_widget_message_text">'
            'Работники <b>ОАО «Доломит»</b> приняли участие в <a href="x">акции</a>.'
            '<br/>Вторая строка</div>'
        )
        resp = MagicMock(text=html)
        with patch.object(logic.requests, "get", return_value=resp):
            text, _, _, _ = logic.fetch_post_data("https://t.me/ch/1")
        self.assertEqual(
            text,
            "Работники ОАО «Доломит» приняли участие в акции.\nВторая строка",
        )


class TestFetchPosts(unittest.TestCase):

    def _fake(self, mapping):
        return lambda url: mapping[url]

    def test_single_post_has_no_markers(self):
        mapping = {"u1": ("Текст", ["a.jpg"], False, "01.02.2026 10:00:00")}
        with patch.object(logic, "fetch_post_data", self._fake(mapping)):
            m = fetch_posts(["u1"])
        self.assertEqual(m.text, "Текст")
        self.assertEqual(m.posts_count, 1)
        self.assertEqual(m.image_urls, ["a.jpg"])

    def test_merge_texts_images_and_earliest_time(self):
        mapping = {
            "u1": ("Первый", ["a.jpg", "b.jpg"], False, "01.02.2026 12:00:00"),
            "u2": ("Второй", ["b.jpg", "c.jpg"], True,  "01.02.2026 09:30:00"),
        }
        with patch.object(logic, "fetch_post_data", self._fake(mapping)):
            m = fetch_posts(["u1", "u2"])
        self.assertIn("[ПОСТ 1 из 2]\nПервый", m.text)
        self.assertIn("[ПОСТ 2 из 2]\nВторой", m.text)
        self.assertLess(m.text.index("Первый"), m.text.index("Второй"))
        self.assertEqual(m.image_urls, ["a.jpg", "b.jpg", "c.jpg"])
        self.assertTrue(m.has_video)
        self.assertEqual(m.post_dt, "01.02.2026 09:30:00")
        self.assertEqual(m.posts_count, 2)

    def test_photo_only_post_adds_images_not_text(self):
        mapping = {
            "u1": ("Текст", [], False, None),
            "u2": ("",      ["p.jpg"], False, None),
        }
        with patch.object(logic, "fetch_post_data", self._fake(mapping)):
            m = fetch_posts(["u1", "u2"])
        self.assertEqual(m.text, "Текст")          # без меток: текст один
        self.assertEqual(m.text_posts, 1)
        self.assertEqual(m.posts_count, 2)
        self.assertEqual(m.image_urls, ["p.jpg"])
        self.assertIsNone(m.post_dt)

    def test_no_text_anywhere_gives_empty_text(self):
        mapping = {"u1": ("", ["p.jpg"], False, None)}
        with patch.object(logic, "fetch_post_data", self._fake(mapping)):
            self.assertEqual(fetch_posts(["u1"]).text, "")

    def test_network_error_names_the_url(self):
        import requests

        def boom(url):
            raise requests.ConnectionError("down")

        with patch.object(logic, "fetch_post_data", boom):
            with self.assertRaises(RuntimeError) as cm:
                fetch_posts(["https://t.me/ch/9"])
        self.assertIn("https://t.me/ch/9", str(cm.exception))

    def test_empty_list_rejected(self):
        with self.assertRaises(ValueError):
            fetch_posts([])


# ─────────────────────────────────────────────
#  Анонс: похожесть на заголовок и обрезка
# ─────────────────────────────────────────────

class TestPreviewSimilarity(unittest.TestCase):

    def test_exact_copy_is_too_similar(self):
        t = "Работники ОАО «Доломит» почтили память павших героев."
        self.assertTrue(preview_too_similar(t, t))

    def test_copy_with_small_addition_is_too_similar(self):
        self.assertTrue(preview_too_similar(
            "Работники ОАО «Доломит» почтили память павших героев.",
            "Работники ОАО «Доломит» почтили память павших героев в День Победы.",
        ))

    def test_reworded_headline_is_too_similar(self):
        self.assertTrue(preview_too_similar(
            "Масленица в микрорайоне Руба прошла весело и вкусно!",
            "Масленица в микрорайоне Руба прошла очень весело.",
        ))

    def test_good_previews_pass(self):
        pairs = [
            ("Масленица в микрорайоне Руба прошла весело и вкусно!",
             "В минувшие выходные работники ОАО «Доломит», жители и гости "
             "микрорайона собрались, чтобы весело проводить зиму и встретить весну."),
            ("Работники ОАО «Доломит» почтили память павших героев.",
             "В День защитников Отечества представители предприятия приняли участие "
             "в церемонии возложения цветов к памятнику Неизвестному солдату."),
        ]
        for title, preview in pairs:
            self.assertFalse(preview_too_similar(title, preview), preview)

    def test_empty_inputs_not_similar(self):
        self.assertEqual(similarity("", "что-то"), 0.0)
        self.assertEqual(similarity("что-то", ""), 0.0)


class TestFitPreview(unittest.TestCase):

    def test_short_unchanged(self):
        self.assertEqual(fit_preview("Короткий анонс."), "Короткий анонс.")

    def test_never_exceeds_limit(self):
        for text in ("слово " * 100, "А" * 400, "Предложение номер один. " * 30):
            self.assertLessEqual(len(fit_preview(text, 150)), 150)

    def test_prefers_sentence_end(self):
        text = "Первое предложение довольно длинное и содержательное. " \
               "Второе предложение тоже длинное и тоже очень содержательное, но не влезет."
        result = fit_preview(text, 100)
        self.assertTrue(result.endswith("."))
        self.assertNotIn("Второе", result)

    def test_ellipsis_when_no_sentence_end(self):
        result = fit_preview("слово " * 100, 50)
        self.assertTrue(result.endswith("…"))
        self.assertLessEqual(len(result), 50)


# ─────────────────────────────────────────────
#  Вызов Gemini: лог, лимиты, переключение моделей
# ─────────────────────────────────────────────

class _GenerateBase(unittest.TestCase):
    """Подменяет client и файл лога запросов."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        os.remove(path)
        self._req_path = path
        self._orig_req = logic.REQUESTS_LOG
        logic.REQUESTS_LOG = path
        self.client = MagicMock()
        self._patch_client = patch.object(logic, "client", self.client)
        self._patch_client.start()
        self._patch_sleep = patch.object(logic.time, "sleep", lambda s: None)
        self._patch_sleep.start()
        self.names = list(logic.MODELS.keys())
        self.apis  = list(logic.MODELS.values())
        logic._disabled_options.clear()
        logic._cooldown.clear()

    def tearDown(self):
        logic._disabled_options.clear()
        logic._cooldown.clear()
        self._patch_client.stop()
        self._patch_sleep.stop()
        logic.REQUESTS_LOG = self._orig_req
        if os.path.exists(self._req_path):
            os.remove(self._req_path)


class TestGenerate(_GenerateBase):

    def test_success_logs_request_under_model_name(self):
        self.client.models.generate_content.return_value = _resp("  ответ  ")
        out = logic._generate("p", self.apis[0], self.names[0], "full")
        self.assertEqual(out, "ответ")
        self.assertEqual(logic.get_today_usage(), {self.names[0]: 1})

    def test_json_mode_passes_config(self):
        self.client.models.generate_content.return_value = _resp("{}")
        logic._generate("p", self.apis[0], self.names[0], "full", json_mode=True)
        kwargs = self.client.models.generate_content.call_args.kwargs
        self.assertEqual(kwargs["config"]["response_mime_type"], "application/json")

    def test_thinking_level_is_passed(self):
        self.client.models.generate_content.return_value = _resp("ok")
        logic._generate("p", self.apis[0], self.names[0], "title")
        cfg = self.client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(cfg["thinking_config"]["thinking_level"], logic.THINKING_LEVEL)
        self.assertNotIn("response_mime_type", cfg)

    def test_thinking_not_passed_when_disabled_in_config(self):
        self.client.models.generate_content.return_value = _resp("ok")
        with patch.object(logic, "THINKING_LEVEL", None):
            logic._generate("p", self.apis[0], self.names[0], "title")
        cfg = self.client.models.generate_content.call_args.kwargs["config"]
        self.assertNotIn("thinking_config", cfg)

    def test_no_config_at_all_when_everything_disabled(self):
        self.client.models.generate_content.return_value = _resp("ok")
        with patch.object(logic, "THINKING_LEVEL", None), \
                patch.object(logic, "REQUEST_TIMEOUT", None):
            logic._generate("p", self.apis[0], self.names[0], "title")
        self.assertNotIn("config", self.client.models.generate_content.call_args.kwargs)

    def test_timeout_passed_in_ms_and_one_and_half_for_full(self):
        self.client.models.generate_content.return_value = _resp("ok")
        with patch.object(logic, "REQUEST_TIMEOUT", 40):
            logic._generate("p", self.apis[0], self.names[0], "translate")
            logic._generate("p", self.apis[0], self.names[0], "full")
        calls = self.client.models.generate_content.call_args_list
        self.assertEqual(calls[0].kwargs["config"]["http_options"]["timeout"], 40000)
        self.assertEqual(calls[1].kwargs["config"]["http_options"]["timeout"], 60000)

    def test_hung_request_is_retried_on_same_model(self):
        self.client.models.generate_content.side_effect = [TimeoutError("hung"), _resp("ok")]
        self.assertEqual(logic._generate("p", self.apis[0], self.names[0], "full"), "ok")
        used = [c.kwargs["model"] for c in self.client.models.generate_content.call_args_list]
        self.assertEqual(used, [self.apis[0], self.apis[0]])

    def test_httpx_style_timeout_is_recognised_by_name(self):
        class ReadTimeout(Exception):
            pass
        self.client.models.generate_content.side_effect = [ReadTimeout("x"), _resp("ok")]
        self.assertEqual(logic._generate("p", self.apis[0], self.names[0], "full"), "ok")

    def test_two_network_failures_give_up_with_clear_error(self):
        self.client.models.generate_content.side_effect = ConnectionError("no route")
        with self.assertRaises(RuntimeError) as cm:
            logic._generate("p", self.apis[0], self.names[0], "full")
        self.assertIn("Нет связи", str(cm.exception))
        self.assertEqual(self.client.models.generate_content.call_count, 2)

    def test_rejected_http_options_is_disabled(self):
        self.client.models.generate_content.side_effect = [
            ValueError("validation error: http_options not permitted"), _resp("ok"),
        ]
        self.assertEqual(logic._generate("p", self.apis[0], self.names[0], "title"), "ok")
        second = self.client.models.generate_content.call_args_list[1].kwargs["config"]
        self.assertNotIn("http_options", second)
        self.assertIn("thinking_config", second)       # остальное не отключается
        self.assertIn("http_options", logic._disabled_options)

    def test_rejected_thinking_param_retries_without_it(self):
        # старый SDK: pydantic ValidationError (ValueError) с упоминанием thinking_level
        self.client.models.generate_content.side_effect = [
            ValueError("1 validation error: thinking_level extra inputs are not permitted"),
            _resp("ok"),
        ]
        out = logic._generate("p", self.apis[0], self.names[0], "full", json_mode=True)
        self.assertEqual(out, "ok")
        calls = self.client.models.generate_content.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn("thinking_config", calls[0].kwargs["config"])
        self.assertNotIn("thinking_config", calls[1].kwargs["config"])
        self.assertIn("response_mime_type", calls[1].kwargs["config"])   # JSON-режим сохранён
        self.assertIn("thinking_config", logic._disabled_options)

    def test_unrelated_error_does_not_disable_thinking(self):
        self.client.models.generate_content.side_effect = FakeApiError(400)
        with self.assertRaises(FakeApiError):
            logic._generate("p", self.apis[0], self.names[0], "full")
        self.assertEqual(logic._disabled_options, set())

    def test_429_switches_to_next_model(self):
        self.client.models.generate_content.side_effect = [
            FakeApiError(429), _resp("из резерва"),
        ]
        out = logic._generate("p", self.apis[0], self.names[0], "full")
        self.assertEqual(out, "из резерва")
        used = [c.kwargs["model"] for c in self.client.models.generate_content.call_args_list]
        self.assertEqual(used, [self.apis[0], self.apis[1]])
        # в лог попала модель, которая реально ответила
        self.assertEqual(logic.get_today_usage(), {self.names[1]: 1})

    def test_503_overload_goes_straight_to_next_model(self):
        # «high demand»: повтор на той же модели только тратит время (в логе — 88 с и 504)
        self.client.models.generate_content.side_effect = [FakeApiError(503), _resp("ok")]
        self.assertEqual(logic._generate("p", self.apis[0], self.names[0], "full"), "ok")
        used = [c.kwargs["model"] for c in self.client.models.generate_content.call_args_list]
        self.assertEqual(used, [self.apis[0], self.apis[1]])

    def test_504_deadline_goes_straight_to_next_model(self):
        self.client.models.generate_content.side_effect = [FakeApiError(504), _resp("ok")]
        logic._generate("p", self.apis[0], self.names[0], "full")
        used = [c.kwargs["model"] for c in self.client.models.generate_content.call_args_list]
        self.assertEqual(used, [self.apis[0], self.apis[1]])

    def test_500_retries_same_model_once(self):
        self.client.models.generate_content.side_effect = [FakeApiError(500), _resp("ok")]
        self.assertEqual(logic._generate("p", self.apis[0], self.names[0], "full"), "ok")
        used = [c.kwargs["model"] for c in self.client.models.generate_content.call_args_list]
        self.assertEqual(used, [self.apis[0], self.apis[0]])

    def test_failed_model_rests_for_next_requests(self):
        self.client.models.generate_content.side_effect = [FakeApiError(503), _resp("a")]
        logic._generate("p", self.apis[0], self.names[0], "full")
        self.client.models.generate_content.reset_mock()
        self.client.models.generate_content.side_effect = None
        self.client.models.generate_content.return_value = _resp("b")
        logic._generate("p", self.apis[0], self.names[0], "translate")
        # второй запрос сразу идёт на модель, которая ответила, а не на сбойную
        self.assertEqual(self.client.models.generate_content.call_args.kwargs["model"],
                         self.apis[1])
        self.assertEqual(self.client.models.generate_content.call_count, 1)

    def test_rest_expires(self):
        logic._cooldown[self.names[0]] = 0          # срок отдыха давно прошёл
        self.client.models.generate_content.return_value = _resp("ok")
        logic._generate("p", self.apis[0], self.names[0], "full")
        self.assertEqual(self.client.models.generate_content.call_args.kwargs["model"],
                         self.apis[0])

    def test_all_models_resting_are_still_tried(self):
        for name in self.names:
            logic._cooldown[name] = logic.time.monotonic() + 1000
        self.client.models.generate_content.return_value = _resp("ok")
        self.assertEqual(logic._generate("p", self.apis[0], self.names[0], "full"), "ok")
        self.assertEqual(self.client.models.generate_content.call_args.kwargs["model"],
                         self.apis[0])

    def test_success_clears_rest(self):
        logic._cooldown[self.names[0]] = logic.time.monotonic() + 1000
        self.client.models.generate_content.return_value = _resp("ok")
        # единственная «неотдыхающая» модель сбоит -> пробуем отдыхающую выбранную и она отвечает
        self.client.models.generate_content.side_effect = (
            [FakeApiError(503)] * (len(self.apis) - 1) + [_resp("ok")])
        logic._generate("p", self.apis[0], self.names[0], "full")
        self.assertNotIn(self.names[0], logic._cooldown)

    def test_rate_limit_rest_is_shorter_than_overload_rest(self):
        self.client.models.generate_content.side_effect = [FakeApiError(429), _resp("ok")]
        t0 = logic.time.monotonic()
        logic._generate("p", self.apis[0], self.names[0], "full")
        rest429 = logic._cooldown[self.names[0]] - t0
        self.client.models.generate_content.side_effect = [FakeApiError(503), _resp("ok")]
        logic._cooldown.clear()
        t0 = logic.time.monotonic()
        logic._generate("p", self.apis[0], self.names[0], "full")
        rest503 = logic._cooldown[self.names[0]] - t0
        self.assertLess(rest429, rest503)

    def test_all_models_429_raises_runtime_error(self):
        self.client.models.generate_content.side_effect = FakeApiError(429)
        with self.assertRaises(RuntimeError):
            logic._generate("p", self.apis[0], self.names[0], "full")
        self.assertEqual(self.client.models.generate_content.call_count, len(self.apis))

    def test_other_errors_propagate(self):
        self.client.models.generate_content.side_effect = FakeApiError(400)
        with self.assertRaises(FakeApiError):
            logic._generate("p", self.apis[0], self.names[0], "full")
        self.assertEqual(self.client.models.generate_content.call_count, 1)

    def test_empty_answer_raises(self):
        self.client.models.generate_content.return_value = _resp("")
        with self.assertRaises(ValueError):
            logic._generate("p", self.apis[0], self.names[0], "full")

    def test_model_over_local_limit_is_skipped(self):
        for _ in range(logic.get_limit(self.names[0])):
            logic.log_request(self.names[0], "full")
        self.client.models.generate_content.return_value = _resp("ok")
        logic._generate("p", self.apis[0], self.names[0], "full")
        used = [c.kwargs["model"] for c in self.client.models.generate_content.call_args_list]
        self.assertEqual(used, [self.apis[1]])

    def test_all_models_over_limit_raises(self):
        for name in self.names:
            for _ in range(logic.get_limit(name)):
                logic.log_request(name, "full")
        with self.assertRaises(RuntimeError):
            logic._generate("p", self.apis[0], self.names[0], "full")
        self.client.models.generate_content.assert_not_called()


# ─────────────────────────────────────────────
#  Генерация заголовка / анонса / текста
# ─────────────────────────────────────────────

GOOD_AI = {
    "title":   "Работники ОАО «Доломит» почтили память павших героев.",
    "preview": "В День защитников Отечества представители предприятия возложили цветы.",
    "text":    "<p>Первый абзац.</p><p>Второй абзац.</p>",
}


class TestEnhanceWithAi(_GenerateBase):

    def _prompt(self):
        return self.client.models.generate_content.call_args_list[0].kwargs["contents"]

    def test_returns_parts(self):
        self.client.models.generate_content.return_value = _resp(json.dumps(GOOD_AI))
        t, p, x = enhance_with_ai("сырой текст", self.apis[0], self.names[0])
        self.assertEqual((t, p, x), (GOOD_AI["title"], GOOD_AI["preview"], GOOD_AI["text"]))
        self.assertEqual(self.client.models.generate_content.call_count, 1)

    def test_single_post_prompt_has_no_merge_instruction(self):
        self.client.models.generate_content.return_value = _resp(json.dumps(GOOD_AI))
        enhance_with_ai("сырой текст", self.apis[0], self.names[0])
        self.assertNotIn("Объедини", self._prompt())

    def test_merge_prompt_for_several_posts(self):
        self.client.models.generate_content.return_value = _resp(json.dumps(GOOD_AI))
        enhance_with_ai("[ПОСТ 1 из 2]\nа\n\n[ПОСТ 2 из 2]\nб", self.apis[0],
                        self.names[0], posts_count=2)
        prompt = self._prompt()
        self.assertIn("Объедини", prompt)
        self.assertIn("2 постов", prompt)

    def test_prompt_has_json_example_with_single_braces(self):
        self.client.models.generate_content.return_value = _resp(json.dumps(GOOD_AI))
        enhance_with_ai("x", self.apis[0], self.names[0])
        self.assertIn('{"title":', self._prompt())
        self.assertNotIn('{{', self._prompt())

    def test_similar_preview_is_regenerated_with_feedback(self):
        bad = dict(GOOD_AI, preview=GOOD_AI["title"])
        self.client.models.generate_content.side_effect = [
            _resp(json.dumps(bad)),
            _resp("Стороны возложили цветы к памятнику Неизвестному солдату."),
        ]
        t, p, x = enhance_with_ai("x", self.apis[0], self.names[0])
        self.assertEqual(p, "Стороны возложили цветы к памятнику Неизвестному солдату.")
        self.assertEqual(self.client.models.generate_content.call_count, 2)
        second_prompt = self.client.models.generate_content.call_args_list[1].kwargs["contents"]
        self.assertIn(GOOD_AI["title"], second_prompt)   # заголовок передан модели

    def test_failed_regeneration_keeps_generated_preview(self):
        bad = dict(GOOD_AI, preview=GOOD_AI["title"])
        self.client.models.generate_content.side_effect = [
            _resp(json.dumps(bad)), FakeApiError(400),
        ]
        _, p, _ = enhance_with_ai("x", self.apis[0], self.names[0])
        self.assertEqual(p, bad["preview"])

    def test_long_preview_is_fitted(self):
        long = dict(GOOD_AI, preview="Совсем другие слова " * 30)
        self.client.models.generate_content.return_value = _resp(json.dumps(long))
        _, p, _ = enhance_with_ai("x", self.apis[0], self.names[0])
        self.assertLessEqual(len(p), logic.PREVIEW_MAX_LEN)

    def test_empty_title_raises(self):
        self.client.models.generate_content.return_value = _resp(
            json.dumps(dict(GOOD_AI, title="")))
        with self.assertRaises(ValueError):
            enhance_with_ai("x", self.apis[0], self.names[0])

    def test_invalid_json_raises(self):
        self.client.models.generate_content.return_value = _resp("не json")
        with self.assertRaises(ValueError):
            enhance_with_ai("x", self.apis[0], self.names[0])

    def test_json_wrapped_in_text_is_parsed(self):
        self.client.models.generate_content.return_value = _resp(
            "Вот результат:\n" + json.dumps(GOOD_AI))
        t, _, _ = enhance_with_ai("x", self.apis[0], self.names[0])
        self.assertEqual(t, GOOD_AI["title"])


class TestRegenerate(_GenerateBase):

    def test_title_prompt_contains_previous_title(self):
        self.client.models.generate_content.return_value = _resp('"Новый заголовок."')
        out = logic.regenerate_title("текст", self.apis[0], self.names[0],
                                     previous="Старый заголовок.")
        self.assertEqual(out, "Новый заголовок.")
        prompt = self.client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn("Старый заголовок.", prompt)

    def test_preview_prompt_contains_title_and_previous(self):
        self.client.models.generate_content.return_value = _resp("Анонс.")
        logic.regenerate_preview("текст", self.apis[0], self.names[0],
                                 title="Заголовок X.", previous="Старый анонс Y.")
        prompt = self.client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn("Заголовок X.", prompt)
        self.assertIn("Старый анонс Y.", prompt)

    def test_preview_is_fitted(self):
        self.client.models.generate_content.return_value = _resp("слово " * 100)
        out = logic.regenerate_preview("текст", self.apis[0], self.names[0])
        self.assertLessEqual(len(out), logic.PREVIEW_MAX_LEN)


# ─────────────────────────────────────────────
#  Перевод на белорусский через ИИ
# ─────────────────────────────────────────────

SRC_HTML = ('<p>\n\t<span style="font-size: 15pt;">Работники приняли участие в акции.'
            '</span>\n</p>'
            '<p>\n\t<span style="font-size: 15pt;">Второй <b>важный</b> абзац.</span>\n</p>')
SRC_FRAGS = ["Работники приняли участие в акции.", "Второй", "важный", "абзац."]
BY_FRAGS  = ["Работнікі прынялі ўдзел у акцыі.", "Другі", "важны", "абзац."]
BY_OK = {"title": "Работнікі ААТ «Даламіт» ўшанавалі памяць.",
         "preview": "Падрабязнасці ўнутры.", "fragments": BY_FRAGS}
BY_HTML = ('<p>\n\t<span style="font-size: 15pt;">Работнікі прынялі ўдзел у акцыі.'
           '</span>\n</p>'
           '<p>\n\t<span style="font-size: 15pt;">Другі <b>важны</b> абзац.</span>\n</p>')


class TestTranslate(_GenerateBase):

    def _translate(self, html=SRC_HTML):
        return translate_to_belarusian(
            "Работники ОАО «Доломит» почтили память.", "Подробности внутри.",
            html, self.apis[0], self.names[0])

    def _prompt(self, n=0):
        return self.client.models.generate_content.call_args_list[n].kwargs["contents"]

    def test_one_request_for_whole_news(self):
        self.client.models.generate_content.return_value = _resp(json.dumps(BY_OK))
        t, p, x = self._translate()
        self.assertEqual(t, BY_OK["title"])
        self.assertEqual(p, BY_OK["preview"])
        self.assertEqual(x, BY_HTML)
        self.assertEqual(self.client.models.generate_content.call_count, 1)
        self.assertEqual(logic.get_today_usage(), {self.names[0]: 1})

    def test_ai_sees_only_text_fragments_not_markup(self):
        self.client.models.generate_content.return_value = _resp(json.dumps(BY_OK))
        self._translate()
        prompt = self._prompt()
        self.assertIn("Работники приняли участие в акции.", prompt)
        self.assertNotIn("font-size", prompt)
        self.assertNotIn("<span", prompt)

    def test_html_is_untouched_except_text(self):
        self.client.models.generate_content.return_value = _resp(json.dumps(BY_OK))
        _, _, x = self._translate()
        import re
        self.assertEqual(re.findall(r"<[^>]+>", x), re.findall(r"<[^>]+>", SRC_HTML))
        self.assertIn("\n\t", x)                 # пробелы между тегами сохранены побайтово

    def test_entities_roundtrip(self):
        html = "<p>Пётр&nbsp;и &amp; Ко</p>"
        self.client.models.generate_content.return_value = _resp(json.dumps(
            {"title": "З.", "preview": "", "fragments": ["Пётр і & Ко"]}))
        _, _, x = translate_to_belarusian("З.", "", html, self.apis[0], self.names[0])
        self.assertEqual(x, "<p>Пётр і &amp; Ко</p>")
        self.assertIn("Пётр\xa0и & Ко", self._prompt())      # ИИ получает обычный текст, не сущности

    def test_leading_and_trailing_spaces_kept(self):
        html = "<p>Начало <b>жирный</b> конец</p>"
        self.client.models.generate_content.return_value = _resp(json.dumps(
            {"title": "З.", "preview": "", "fragments": ["Пачатак", "тоўсты", "канец"]}))
        _, _, x = translate_to_belarusian("З.", "", html, self.apis[0], self.names[0])
        self.assertEqual(x, "<p>Пачатак <b>тоўсты</b> канец</p>")

    def test_prompt_has_glossary(self):
        self.client.models.generate_content.return_value = _resp(json.dumps(BY_OK))
        self._translate()
        for ru, by in logic.TRANSLATION_GLOSSARY.items():
            self.assertIn(ru, self._prompt())
            self.assertIn(by, self._prompt())

    def test_wrong_fragment_count_triggers_one_retry(self):
        short = dict(BY_OK, fragments=BY_FRAGS[:2])
        self.client.models.generate_content.side_effect = [
            _resp(json.dumps(short)), _resp(json.dumps(BY_OK)),
        ]
        _, _, x = self._translate()
        self.assertEqual(x, BY_HTML)
        self.assertEqual(self.client.models.generate_content.call_count, 2)
        self.assertIn("ТОЧНО той же длины", self._prompt(1))

    def test_still_wrong_after_retry_raises(self):
        short = dict(BY_OK, fragments=BY_FRAGS[:2])
        self.client.models.generate_content.return_value = _resp(json.dumps(short))
        with self.assertRaises(ValueError):
            self._translate()
        self.assertEqual(self.client.models.generate_content.call_count, 2)

    def test_empty_fragment_triggers_retry(self):
        bad = dict(BY_OK, fragments=["", *BY_FRAGS[1:]])
        self.client.models.generate_content.side_effect = [
            _resp(json.dumps(bad)), _resp(json.dumps(BY_OK)),
        ]
        self._translate()
        self.assertEqual(self.client.models.generate_content.call_count, 2)

    def test_empty_title_triggers_retry(self):
        self.client.models.generate_content.side_effect = [
            _resp(json.dumps(dict(BY_OK, title=""))), _resp(json.dumps(BY_OK)),
        ]
        t, _, _ = self._translate()
        self.assertEqual(t, BY_OK["title"])

    def test_empty_preview_stays_empty(self):
        self.client.models.generate_content.return_value = _resp(
            json.dumps(dict(BY_OK, preview="")))
        _, p, _ = translate_to_belarusian(
            "Заголовок.", "", SRC_HTML, self.apis[0], self.names[0])
        self.assertEqual(p, "")

    def test_html_without_text(self):
        self.client.models.generate_content.return_value = _resp(
            json.dumps({"title": "Загаловак.", "preview": "", "fragments": []}))
        t, _, x = translate_to_belarusian("Заголовок.", "", "<p> </p>",
                                          self.apis[0], self.names[0])
        self.assertEqual(t, "Загаловак.")
        self.assertEqual(x, "<p> </p>")

    def test_comments_and_scripts_are_not_sent(self):
        html = "<!-- заметка --><p>Текст.</p><script>var a = 1;</script>"
        self.client.models.generate_content.return_value = _resp(
            json.dumps({"title": "З.", "preview": "", "fragments": ["Тэкст."]}))
        _, _, x = translate_to_belarusian("З.", "", html, self.apis[0], self.names[0])
        self.assertNotIn("заметка", self._prompt())
        self.assertNotIn("var a", self._prompt())
        self.assertIn("<!-- заметка -->", x)
        self.assertIn("var a = 1;", x)
        self.assertIn("Тэкст.", x)

    def test_translation_model_override(self):
        self.client.models.generate_content.return_value = _resp(json.dumps(BY_OK))
        with patch.object(logic, "TRANSLATION_MODEL", self.names[-1]):
            self._translate()
        self.assertEqual(self.client.models.generate_content.call_args.kwargs["model"],
                         self.apis[-1])
        self.assertEqual(logic.get_today_usage(), {self.names[-1]: 1})

    def test_defaults_to_first_model(self):
        self.client.models.generate_content.return_value = _resp(json.dumps(BY_OK))
        translate_to_belarusian("З.", "А.", SRC_HTML)
        self.assertEqual(self.client.models.generate_content.call_args.kwargs["model"],
                         self.apis[0])


class TestBelarusianCheck(unittest.TestCase):

    RU_LONG = ("Работники предприятия приняли участие в праздничных мероприятиях и "
               "подвели итоги работы за год, отметив лучших сотрудников. " * 3)
    BY_LONG = ("Работнікі прадпрыемства прынялі ўдзел у святочных мерапрыемствах і "
               "падвялі вынікі працы за год, адзначыўшы лепшых супрацоўнікаў. " * 3)

    def test_russian_detected(self):
        self.assertFalse(logic._looks_belarusian(self.RU_LONG))

    def test_belarusian_passes(self):
        self.assertTrue(logic._looks_belarusian(self.BY_LONG))

    def test_one_untranslated_sentence_is_caught(self):
        # раньше такой текст проскакивал: русская доля букв была ниже порога
        mixed = self.BY_LONG + " Работники предприятия приняли участие в празднике."
        self.assertFalse(logic._looks_belarusian(mixed))

    def test_quoted_russian_name_does_not_fail_long_text(self):
        text = self.BY_LONG * 2 + " Мерапрыемства «Единый день информирования»."
        self.assertTrue(logic._looks_belarusian(text))

    def test_two_russian_words_pass(self):
        self.assertTrue(logic._looks_belarusian("Работники приняли."))

    def test_tags_are_ignored(self):
        self.assertTrue(logic._looks_belarusian(
            '<p><span style="font-size: 15pt;">' + self.BY_LONG + '</span></p>'))

    def test_russian_fragments_rejected_as_problem(self):
        src = ["а"] * 3
        out = [self.RU_LONG, self.RU_LONG, self.RU_LONG]
        self.assertIsNotNone(logic._fragments_problem(src, out))

    def test_untranslated_title_rejected(self):
        problem = logic._fragments_problem(
            ["а"], ["Работнікі прынялі ўдзел."], title="Работники почтили память героев.")
        self.assertIsNotNone(problem)
        self.assertIn("заголовок", problem)

    def test_belarusian_title_and_preview_pass(self):
        self.assertIsNone(logic._fragments_problem(
            ["а"], ["Работнікі прынялі ўдзел."],
            title="Работнікі ААТ «Даламіт» ўшанавалі памяць.", preview="Падрабязнасці ўнутры."))

    def test_fragments_must_be_list_of_strings(self):
        self.assertIsNotNone(logic._fragments_problem(["а"], "строка"))
        self.assertIsNotNone(logic._fragments_problem(["а"], [1]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
