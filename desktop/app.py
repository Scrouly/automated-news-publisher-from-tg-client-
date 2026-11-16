"""
Доломит — Публикатор новостей
Главное окно. Точка входа: python app.py
"""

import io
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

import requests
from PIL import Image, ImageTk

from core.config import C, MODELS, PREVIEW_MAX_LEN
from desktop.dialogs import PhotoPickerDialog, HistoryDialog
from core.logic import (
    clean_tg_url, check_duplicate, log_publication,
    fetch_post_data, enhance_with_ai, inject_font_size,
    send_to_bitrix, regenerate_title, regenerate_preview,
    check_limit, translate_to_belarusian,
    save_draft, load_draft, clear_draft, validate_publication,
)
from desktop.widgets import RoundedButton, StyledEntry, StyledText, StatusBar, TimeEntry


# ─────────────────────────────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНЫЕ ВИДЖЕТЫ
# ─────────────────────────────────────────────────────────────────

class Spinner(tk.Label):
    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=C["bg"], fg=C["accent"], font=("Segoe UI", 15), text="", **kw)
        self._on = False

    def start(self):
        self._on = True
        self._i  = 0
        self.pack(side="left", padx=(0, 5))
        self._tick()

    def stop(self):
        self._on = False
        self.pack_forget()
        self.config(text="")

    def _tick(self):
        if self._on:
            self.config(text=self.FRAMES[self._i % len(self.FRAMES)])
            self._i += 1
            self.after(80, self._tick)


class PhotoPreview(tk.Frame):
    W, H = 240, 180

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=C["surface2"],
                         highlightbackground=C["border"], highlightthickness=1,
                         width=self.W, height=self.H, **kw)
        self.pack_propagate(False)
        self._ref = None
        self._ph  = tk.Label(self, text="Фото для анонса\nне выбрано",
                              font=("Segoe UI", 10), bg=C["surface2"],
                              fg=C["text_faint"], justify="center")
        self._ph.place(relx=0.5, rely=0.5, anchor="center")
        self._img = tk.Label(self, bg=C["surface2"], bd=0)

    def set_url(self, url):
        self._ph.config(text="⏳ Загрузка...", fg=C["accent"])
        threading.Thread(target=self._load, args=(url,), daemon=True).start()

    def _load(self, url):
        try:
            r   = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            w, h = img.size
            tr   = self.W / self.H
            if abs(w / h - tr) > 0.02:
                if w / h > tr:
                    nw = int(h * tr)
                    img = img.crop(((w - nw) // 2, 0, (w + nw) // 2, h))
                else:
                    nh = int(w / tr)
                    img = img.crop((0, (h - nh) // 2, w, (h + nh) // 2))
            img.thumbnail((self.W, self.H), Image.LANCZOS)
            ph = ImageTk.PhotoImage(img)
            self.after(0, self._show, ph)
        except Exception:
            self.after(0, lambda: self._ph.config(text="Не удалось\nзагрузить", fg=C["danger"]))

    def _show(self, ph):
        self._ref = ph
        self._img.config(image=ph)
        self._ph.place_forget()
        self._img.place(relx=0.5, rely=0.5, anchor="center")

    def clear(self):
        self._img.place_forget()
        self._ref = None
        self._ph.config(text="Фото для анонса\nне выбрано", fg=C["text_faint"])
        self._ph.place(relx=0.5, rely=0.5, anchor="center")


class AnnouncementCard(tk.Frame):
    """Предпросмотр анонса — показывает как карточка выглядит в ленте новостей."""

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=C["surface"],
                         highlightbackground=C["border"], highlightthickness=1, **kw)
        tk.Label(self, text="ПРЕДПРОСМОТР АНОНСА",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["surface"], fg=C["text_faint"]).pack(anchor="w", padx=10, pady=(8, 2))
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x", padx=10)

        self._title_lbl = tk.Label(
            self, text="", font=("Segoe UI", 10, "bold"),
            bg=C["surface"], fg=C["text"],
            wraplength=218, justify="left", anchor="w",
        )
        self._title_lbl.pack(anchor="w", padx=10, pady=(8, 2), fill="x")

        self._preview_lbl = tk.Label(
            self, text="", font=("Segoe UI", 9),
            bg=C["surface"], fg=C["text_dim"],
            wraplength=218, justify="left", anchor="w",
        )
        self._preview_lbl.pack(anchor="w", padx=10, pady=(0, 6), fill="x")

        self._date_lbl = tk.Label(
            self, text="", font=("Segoe UI", 8),
            bg=C["surface"], fg=C["text_faint"], anchor="w",
        )
        self._date_lbl.pack(anchor="w", padx=10, pady=(0, 8))

    def update(self, title: str, preview: str, date_str: str = ""):
        self._title_lbl.config(text=title or "—")
        self._preview_lbl.config(text=preview or "—")
        # Показываем только дату (без времени) если формат стандартный
        short = date_str[:10] if date_str else ""
        self._date_lbl.config(text=short)


# ─────────────────────────────────────────────────────────────────
#  ГЛАВНОЕ ОКНО
# ─────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Доломит — Публикатор новостей")
        self.configure(bg=C["bg"])
        self.geometry("1020x920")
        self.minsize(960, 820)

        # Состояние
        self._image_urls        = []
        self._preview_image_url = None
        self._tg_url            = ""
        self._post_text         = ""
        self._active_from       = None   # время из поста Telegram
        self._cancel_event      = threading.Event()   # отмена текущего запроса
        self._translation_fresh = False  # флаг актуальности BY-перевода

        # Данные обеих версий (ru/by)
        self._data = {
            "ru": {"title": "", "preview": "", "text": ""},
            "by": {"title": "", "preview": "", "text": ""},
        }
        self._lang = "ru"

        self._model_var          = tk.StringVar(value=list(MODELS.keys())[0])
        self._publish_by_var     = tk.BooleanVar(value=True)
        self._target_section_var = tk.StringVar(value="news")

        self._build_ui()
        self._refresh_usage()
        self.after(100, self._fade_in)
        self.after(200, self._try_load_draft)

    # ──────────────────────────────────────────────────────────────
    #  СТРОИТЕЛЬСТВО UI
    # ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_header()
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")
        self._build_body()
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")
        self._status_bar = StatusBar(self)
        self._status_bar.pack(fill="x", side="bottom")

    # ── Шапка ─────────────────────────────────────────────────────

    def _build_header(self):
        hdr = tk.Frame(self, bg=C["surface"], height=64)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        # Логотип
        left = tk.Frame(hdr, bg=C["surface"])
        left.pack(side="left", padx=24, fill="y")
        tk.Label(left, text="◈", font=("Segoe UI", 20),
                 bg=C["surface"], fg=C["accent"]).pack(side="left", pady=16, padx=(0, 10))
        col = tk.Frame(left, bg=C["surface"])
        col.pack(side="left", pady=12)
        tk.Label(col, text="ДОЛОМИТ", font=("Segoe UI", 13, "bold"),
                 bg=C["surface"], fg=C["text"]).pack(anchor="w")
        tk.Label(col, text="Публикатор новостей", font=("Segoe UI", 9),
                 bg=C["surface"], fg=C["text_dim"]).pack(anchor="w")

        # Правая панель
        right = tk.Frame(hdr, bg=C["surface"])
        right.pack(side="right", padx=20, fill="y")

        row = tk.Frame(right, bg=C["surface"])
        row.pack(expand=True, anchor="center", pady=18)

        # Кнопка истории
        RoundedButton(row, "📋  История", self._on_history,
                      width=120, height=32, font_size=9,
                      bg=C["surface2"], hover_bg=C["border"],
                      canvas_bg=C["surface"]).pack(side="left", padx=(0, 14))

        tk.Frame(row, bg=C["border"], width=1, height=22).pack(side="left", padx=(0, 12))

        tk.Label(row, text="Модель ИИ:", font=("Segoe UI", 9),
                 bg=C["surface"], fg=C["text_dim"]).pack(side="left", padx=(0, 8))

        sty = ttk.Style()
        sty.theme_use("default")
        sty.configure("D.TCombobox",
                       fieldbackground=C["surface2"], background=C["surface2"],
                       foreground=C["text"], selectbackground=C["accent"],
                       selectforeground=C["white"], arrowcolor=C["accent"], relief="flat")
        sty.map("D.TCombobox",
                fieldbackground=[("readonly", C["surface2"])],
                foreground=[("readonly", C["text"])])

        self._model_cb = ttk.Combobox(row, textvariable=self._model_var,
                                       values=list(MODELS.keys()), state="readonly",
                                       style="D.TCombobox", font=("Segoe UI", 10), width=20)
        self._model_cb.pack(side="left")
        self._model_cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_usage())

        tk.Frame(row, bg=C["border"], width=1, height=22).pack(side="left", padx=12)

        self._usage_lbl = tk.Label(row, text="Использовано сегодня: — / —",
                                    font=("Segoe UI", 9), bg=C["surface"], fg=C["text_dim"])
        self._usage_lbl.pack(side="left")

    # ── Тело ──────────────────────────────────────────────────────

    def _build_body(self):
        main = tk.Frame(self, bg=C["bg"])
        main.pack(fill="both", expand=True, padx=28, pady=18)

        # ── 01 URL
        self._lbl(main, "01  ССЫЛКА НА ПОСТ")
        url_row = tk.Frame(main, bg=C["bg"])
        url_row.pack(fill="x", pady=(6, 0))

        self._url_entry = StyledEntry(url_row, placeholder="https://t.me/DOLOMIT_RUBA/0000")
        self._url_entry.pack(side="left", fill="both", expand=True)

        self._fetch_spinner = Spinner(url_row)

        self._fetch_btn = RoundedButton(url_row, "⚡  Загрузить", self._on_fetch,
                                         width=148, height=44, font_size=11, canvas_bg=C["bg"])
        self._fetch_btn.pack(side="left", padx=(12, 0))

        # Кнопка отмены (скрыта по умолчанию)
        self._cancel_btn = RoundedButton(url_row, "✕  Отмена", self._on_cancel,
                                          width=110, height=44, font_size=11,
                                          bg=C["danger"], hover_bg="#D03D4E", canvas_bg=C["bg"])

        self._warn_video = tk.Label(main, text="", bg=C["bg"],
                                     font=("Segoe UI", 10), fg=C["warning"])
        self._warn_video.pack(anchor="w", pady=(4, 0))
        self._warn_dup = tk.Label(main, text="", bg=C["bg"],
                                   font=("Segoe UI", 10), fg=C["danger"])
        self._warn_dup.pack(anchor="w")

        tk.Frame(main, bg=C["border"], height=1).pack(fill="x", pady=12)

        # ── 02 Редактирование
        self._lbl(main, "02  РЕДАКТИРОВАНИЕ")

        cols = tk.Frame(main, bg=C["bg"])
        cols.pack(fill="both", expand=True, pady=(6, 0))

        # ── Левая: поля
        lft = tk.Frame(cols, bg=C["bg"])
        lft.pack(side="left", fill="both", expand=True)

        # Переключатель RU / BY
        tab_row = tk.Frame(lft, bg=C["bg"])
        tab_row.pack(fill="x", pady=(0, 8))

        self._tab_ru = self._tab_btn(tab_row, "🇷🇺  Русский", lambda: self._switch_lang("ru"))
        self._tab_by = self._tab_btn(tab_row, "🇧🇾  Беларускі", lambda: self._switch_lang("by"))
        self._tab_ru.pack(side="left", padx=(0, 4))
        self._tab_by.pack(side="left")

        self._by_status = tk.Label(tab_row, text="", font=("Segoe UI", 9),
                                    bg=C["bg"], fg=C["text_dim"])
        self._by_status.pack(side="left", padx=(14, 0))

        # Заголовок
        title_hdr = tk.Frame(lft, bg=C["bg"])
        title_hdr.pack(fill="x", pady=(0, 3))
        tk.Label(title_hdr, text="Заголовок", font=("Segoe UI", 10),
                 bg=C["bg"], fg=C["text_dim"]).pack(side="left")
        self._regen_title_sp  = Spinner(title_hdr)
        self._regen_title_btn = RoundedButton(
            title_hdr, "↻ Новый заголовок", self._on_regen_title,
            width=160, height=28, font_size=9, bg=C["surface2"],
            hover_bg=C["border"], canvas_bg=C["bg"])
        self._regen_title_btn.pack(side="right")

        self._title_entry = StyledEntry(lft, placeholder="Заголовок новости...")
        self._title_entry.pack(fill="x")
        self._title_entry.entry.bind("<FocusOut>", lambda e: self._on_ru_edit())
        self._title_entry.entry.bind("<KeyRelease>", lambda e: self._update_card())

        # Анонс
        prev_hdr = tk.Frame(lft, bg=C["bg"])
        prev_hdr.pack(fill="x", pady=(10, 3))
        tk.Label(prev_hdr, text="Анонс", font=("Segoe UI", 10),
                 bg=C["bg"], fg=C["text_dim"]).pack(side="left")
        self._regen_prev_sp  = Spinner(prev_hdr)
        self._regen_prev_btn = RoundedButton(
            prev_hdr, "↻ Новый анонс", self._on_regen_preview,
            width=145, height=28, font_size=9, bg=C["surface2"],
            hover_bg=C["border"], canvas_bg=C["bg"])
        self._regen_prev_btn.pack(side="right")

        self._preview_entry = StyledEntry(lft, placeholder="Краткое описание для ленты...")
        self._preview_entry.pack(fill="x")

        self._char_lbl = tk.Label(lft, text=f"0 / {PREVIEW_MAX_LEN}",
                                   font=("Segoe UI", 9), bg=C["bg"], fg=C["text_dim"])
        self._char_lbl.pack(anchor="e")
        self._preview_entry.entry.bind("<KeyRelease>", self._on_preview_key)
        self._preview_entry.entry.bind("<FocusOut>",   lambda e: self._on_ru_edit())

        # Основной текст
        tk.Label(lft, text="Основной текст (HTML)", font=("Segoe UI", 10),
                 bg=C["bg"], fg=C["text_dim"]).pack(anchor="w", pady=(6, 3))
        self._text_box = StyledText(lft, height=9)
        self._text_box.pack(fill="both", expand=True)
        self._text_box.text.bind("<FocusOut>", lambda e: self._on_ru_edit())

        # ── Правая: фото + предпросмотр
        rgt = tk.Frame(cols, bg=C["bg"])
        rgt.pack(side="right", fill="y", padx=(20, 0))

        self._lbl(rgt, "03  ФОТО ДЛЯ АНОНСА")
        self._photo_preview = PhotoPreview(rgt)
        self._photo_preview.pack(pady=(8, 0))
        self._photo_lbl = tk.Label(rgt, text="Фото не выбрано",
                                    font=("Segoe UI", 9), bg=C["bg"], fg=C["text_dim"],
                                    wraplength=240, justify="center")
        self._photo_lbl.pack(pady=(6, 8))
        self._photo_btn = RoundedButton(rgt, "🖼  Выбрать фото", self._on_pick_photo,
                                         width=200, height=36, bg=C["surface2"],
                                         hover_bg=C["border"], font_size=10, canvas_bg=C["bg"])
        self._photo_btn.pack()

        tk.Frame(rgt, bg=C["border"], height=1).pack(fill="x", pady=(14, 0))

        self._announce_card = AnnouncementCard(rgt)
        self._announce_card.pack(fill="x", pady=(10, 0))

        tk.Frame(main, bg=C["border"], height=1).pack(fill="x", pady=12)

        # ── 04 Публикация
        bottom = tk.Frame(main, bg=C["bg"])
        bottom.pack(fill="x")

        by_frame = tk.Frame(bottom, bg=C["bg"])
        by_frame.pack(side="left", fill="y")
        self._lbl(by_frame, "04  ПУБЛИКАЦИЯ")

        # Выбор раздела
        sec_frame = tk.Frame(by_frame, bg=C["bg"])
        sec_frame.pack(anchor="w", pady=(6, 4))
        tk.Label(sec_frame, text="Раздел:", font=("Segoe UI", 10),
                 bg=C["bg"], fg=C["text_dim"]).pack(side="left", padx=(0, 8))
        tk.Radiobutton(sec_frame, text="Новости", variable=self._target_section_var,
                       value="news", bg=C["bg"], fg=C["text"], selectcolor=C["surface2"],
                       activebackground=C["bg"], activeforeground=C["text"],
                       font=("Segoe UI", 10), cursor="hand2").pack(side="left", padx=(0, 15))
        tk.Radiobutton(sec_frame, text="Профсоюзная жизнь", variable=self._target_section_var,
                       value="union", bg=C["bg"], fg=C["text"], selectcolor=C["surface2"],
                       activebackground=C["bg"], activeforeground=C["text"],
                       font=("Segoe UI", 10), cursor="hand2").pack(side="left")

        # BY чекбокс
        self._publish_by_cb = tk.Checkbutton(
            by_frame, text="Публиковать белорусскую версию",
            variable=self._publish_by_var,
            bg=C["bg"], fg=C["text_dim"], selectcolor=C["surface2"],
            activebackground=C["bg"], activeforeground=C["text"],
            font=("Segoe UI", 10), cursor="hand2",
        )
        self._publish_by_cb.pack(anchor="w", pady=(4, 0))

        # Время активности — редактируемое поле
        time_row = tk.Frame(by_frame, bg=C["bg"])
        time_row.pack(anchor="w", pady=(6, 0))
        tk.Label(time_row, text="Начало активности:", font=("Segoe UI", 9),
                 bg=C["bg"], fg=C["text_dim"]).pack(side="left", padx=(0, 8))
        self._active_entry = TimeEntry(time_row)
        self._active_entry.pack(side="left")
        tk.Label(time_row, text="ДД.ММ.ГГГГ ЧЧ:ММ:СС",
                 font=("Segoe UI", 8), bg=C["bg"], fg=C["text_faint"]).pack(side="left", padx=(6, 0))

        self._active_src_lbl = tk.Label(by_frame, text="", font=("Segoe UI", 8),
                                         bg=C["bg"], fg=C["text_dim"])
        self._active_src_lbl.pack(anchor="w")

        # Кнопка публикации
        pub_right = tk.Frame(bottom, bg=C["bg"])
        pub_right.pack(side="right", fill="y")

        self._publish_sp  = Spinner(pub_right)
        self._publish_btn = RoundedButton(pub_right, "🚀  Опубликовать", self._on_publish,
                                           width=220, height=52, bg=C["success"],
                                           hover_bg="#27B87A", font_size=13, canvas_bg=C["bg"])
        self._publish_btn.pack(side="right", pady=8)

    def _tab_btn(self, parent, text, cmd):
        return RoundedButton(parent, text, cmd, width=150, height=32, font_size=10,
                              bg=C["surface2"], hover_bg=C["border"], canvas_bg=C["bg"])

    def _lbl(self, parent, text):
        tk.Label(parent, text=text, font=("Segoe UI", 9, "bold"),
                 bg=C["bg"], fg=C["accent"]).pack(anchor="w")

    # ──────────────────────────────────────────────────────────────
    #  АНИМАЦИЯ
    # ──────────────────────────────────────────────────────────────

    def _fade_in(self):
        self.attributes("-alpha", 0)
        self._fade(0.0)

    def _fade(self, a):
        if a <= 1.0:
            self.attributes("-alpha", a)
            self.after(18, self._fade, round(a + 0.08, 2))

    # ──────────────────────────────────────────────────────────────
    #  ЧЕРНОВИК
    # ──────────────────────────────────────────────────────────────

    def _try_load_draft(self):
        draft = load_draft()
        if not draft or not draft.get("data", {}).get("ru", {}).get("title"):
            return
        if messagebox.askyesno(
            "Черновик",
            "Найден незавершённый черновик. Восстановить?",
            parent=self,
        ):
            self._restore_draft(draft)

    def _restore_draft(self, draft: dict):
        self._tg_url        = draft.get("tg_url", "")
        self._image_urls    = draft.get("image_urls", [])
        self._preview_image_url = draft.get("preview_image_url")
        self._data          = draft.get("data", self._data)

        if self._tg_url:
            self._url_entry.set(self._tg_url)

        active = draft.get("active_from", "")
        if active:
            self._active_entry.set(active)
            self._active_from = active
            self._active_src_lbl.config(text="(восстановлено из черновика)", fg=C["text_faint"])

        self._lang = "ru"
        self._load_lang("ru")
        self._update_tab_style()

        if self._preview_image_url:
            idx = (self._image_urls.index(self._preview_image_url) + 1
                   if self._preview_image_url in self._image_urls else 1)
            self._photo_lbl.config(
                text=f"Фото №{idx}  (доступно: {len(self._image_urls)})",
                fg=C["success"],
            )
            self._photo_preview.set_url(self._preview_image_url)

        if draft.get("data", {}).get("by", {}).get("title"):
            self._by_status.config(text="✓ Перевод восстановлен", fg=C["success"])

        self._update_card()
        self._status("Черновик восстановлен", "success")

    def _save_draft(self):
        """Сохраняет текущее состояние в draft.json."""
        if self._lang == "ru":
            self._data["ru"]["title"]   = self._title_entry.get()
            self._data["ru"]["preview"] = self._preview_entry.get()
            self._data["ru"]["text"]    = self._text_box.get()
        if not self._data["ru"]["title"]:
            return
        save_draft({
            "tg_url":            self._tg_url,
            "image_urls":        self._image_urls,
            "preview_image_url": self._preview_image_url,
            "active_from":       self._active_entry.get(),
            "data":              self._data,
        })

    # ──────────────────────────────────────────────────────────────
    #  ВКЛАДКИ ЯЗЫКА
    # ──────────────────────────────────────────────────────────────

    def _switch_lang(self, lang: str):
        if self._lang == "ru":
            self._data["ru"]["title"]   = self._title_entry.get()
            self._data["ru"]["preview"] = self._preview_entry.get()
            self._data["ru"]["text"]    = self._text_box.get()
        self._lang = lang
        self._load_lang(lang)
        self._update_tab_style()

    def _load_lang(self, lang: str):
        self._title_entry.entry.config(state="normal")
        self._preview_entry.entry.config(state="normal")
        self._text_box.text.config(state="normal")

        d = self._data[lang]
        if d["title"]:
            self._title_entry.set(d["title"])
        else:
            self._title_entry.clear()

        if d["preview"]:
            self._preview_entry.set(d["preview"])
        else:
            self._preview_entry.clear()

        if d["text"]:
            self._text_box.set(d["text"])
        else:
            self._text_box.clear()

        self._on_preview_key()

        ro = "disabled" if lang == "by" else "normal"
        self._title_entry.entry.config(state=ro)
        self._preview_entry.entry.config(state=ro)
        self._text_box.text.config(state=ro)

        self._regen_title_btn.set_enabled(lang == "ru")
        self._regen_prev_btn.set_enabled(lang == "ru")

    def _update_tab_style(self):
        for btn, lang in ((self._tab_ru, "ru"), (self._tab_by, "by")):
            active = self._lang == lang
            btn._bg       = C["accent"]      if active else C["surface2"]
            btn._hover_bg = C["accent_dark"] if active else C["border"]
            btn._inner.config(bg=btn._bg, fg=C["white"] if active else C["text_dim"])

    # ──────────────────────────────────────────────────────────────
    #  ПРЕДПРОСМОТР КАРТОЧКИ
    # ──────────────────────────────────────────────────────────────

    def _update_card(self, _=None):
        title   = self._title_entry.get()
        preview = self._preview_entry.get()
        date    = self._active_entry.get()
        self._announce_card.update(title, preview, date)

    # ──────────────────────────────────────────────────────────────
    #  СЧЁТЧИК ИСПОЛЬЗОВАНИЯ
    # ──────────────────────────────────────────────────────────────

    def _refresh_usage(self):
        name = self._model_var.get()
        can, used, limit = check_limit(name)
        color = C["danger"] if not can else C["warning"] if used >= limit * 0.8 else C["text_dim"]
        self._usage_lbl.config(text=f"Использовано сегодня: {used} / {limit}", fg=color)

    def _get_model(self):
        name = self._model_var.get()
        return name, MODELS[name]

    def _guard_limit(self) -> bool:
        name, _ = self._get_model()
        can, used, limit = check_limit(name)
        if not can:
            messagebox.showwarning(
                "Лимит исчерпан",
                f"Для модели «{name}» исчерпан дневной лимит ({limit} запр.).\n"
                "Попробуйте завтра или выберите другую модель.",
                parent=self,
            )
        return can

    # ──────────────────────────────────────────────────────────────
    #  ИСТОРИЯ ПУБЛИКАЦИЙ
    # ──────────────────────────────────────────────────────────────

    def _on_history(self):
        HistoryDialog(self)

    # ──────────────────────────────────────────────────────────────
    #  ЗАГРУЗКА ПОСТА
    # ──────────────────────────────────────────────────────────────

    def _on_fetch(self):
        url = self._url_entry.get().strip()
        if not url:
            self._status("Введите ссылку на пост", "warn")
            return
        if not self._guard_limit():
            return

        # Отмена предыдущего запроса если ещё выполняется
        self._cancel_event.set()
        self._cancel_event = threading.Event()

        url = clean_tg_url(url)
        self._tg_url = url
        self._url_entry.set(url)
        self._warn_video.config(text="")
        self._warn_dup.config(text="")

        if check_duplicate(url):
            self._warn_dup.config(text="⚠  Этот пост уже публиковался")

        self._loading(True, "Загружаю пост из Telegram...")
        threading.Thread(
            target=self._fetch_thread,
            args=(url, self._cancel_event),
            daemon=True,
        ).start()

    def _on_cancel(self):
        self._cancel_event.set()
        self._loading(False)
        self._status("Загрузка отменена", "warn")

    def _fetch_thread(self, url, cancel: threading.Event):
        try:
            post_text, img_urls, has_video, post_dt = fetch_post_data(url)
            if cancel.is_set():
                return
            if not post_text.strip():
                self.after(0, self._fetch_error, "Текст поста не найден")
                return

            self._post_text  = post_text
            self._image_urls = img_urls
            self.after(0, lambda: self._status("Генерирую текст через ИИ...", "loading"))

            mn, ma = self._get_model()
            title, preview, html = enhance_with_ai(post_text, ma, mn)
            if cancel.is_set():
                return
            html = inject_font_size(html)
            self.after(0, self._fetch_done, title, preview, html, has_video, post_dt)
        except Exception as ex:
            if not cancel.is_set():
                self.after(0, self._fetch_error, str(ex))

    def _fetch_done(self, title, preview, html, has_video, post_dt):
        self._loading(False)
        self._data["ru"] = {"title": title, "preview": preview, "text": html}
        self._data["by"] = {"title": "", "preview": "", "text": ""}
        self._translation_fresh = False

        self._lang = "ru"
        self._load_lang("ru")
        self._update_tab_style()
        self._refresh_usage()

        # Время активности
        if post_dt:
            self._active_entry.set(post_dt)
            self._active_from = post_dt
            self._active_src_lbl.config(text="(время из поста)", fg=C["text_faint"])
        else:
            now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
            self._active_entry.set(now)
            self._active_from = now
            self._active_src_lbl.config(
                text="⚠ время поста не найдено", fg=C["warning"]
            )

        if has_video:
            self._warn_video.config(text="⚠  В посте есть видео — оно не будет загружено")

        if self._image_urls:
            self._preview_image_url = self._image_urls[0]
            self._photo_lbl.config(
                text=f"Фото №1  (доступно: {len(self._image_urls)})",
                fg=C["success"],
            )
            self._photo_preview.set_url(self._preview_image_url)
        else:
            self._preview_image_url = None
            self._photo_lbl.config(text="Фотографии не найдены", fg=C["text_dim"])
            self._photo_preview.clear()

        self._update_card()
        self._save_draft()
        self._status(f"Готово. Фото: {len(self._image_urls)}", "success")

        if self._publish_by_var.get():
            self._start_translate(title, preview, html)

    def _fetch_error(self, msg):
        self._loading(False)
        self._status(f"Ошибка: {msg}", "error")

    # ──────────────────────────────────────────────────────────────
    #  ПЕРЕГЕНЕРАЦИЯ (RU)
    # ──────────────────────────────────────────────────────────────

    def _on_regen_title(self):
        if not self._post_text:
            self._status("Сначала загрузите пост", "warn")
            return
        if not self._guard_limit():
            return
        self._regen_title_btn.set_enabled(False)
        self._regen_title_sp.start()
        self._status("Генерирую новый заголовок...", "loading")
        mn, ma = self._get_model()
        src    = self._data["ru"]["text"] or self._post_text
        threading.Thread(target=self._do_regen, args=("title", src, ma, mn), daemon=True).start()

    def _on_regen_preview(self):
        if not self._post_text:
            self._status("Сначала загрузите пост", "warn")
            return
        if not self._guard_limit():
            return
        self._regen_prev_btn.set_enabled(False)
        self._regen_prev_sp.start()
        self._status("Генерирую новый анонс...", "loading")
        mn, ma = self._get_model()
        src    = self._data["ru"]["text"] or self._post_text
        threading.Thread(target=self._do_regen, args=("preview", src, ma, mn), daemon=True).start()

    def _do_regen(self, kind, src, ma, mn):
        try:
            result = regenerate_title(src, ma, mn) if kind == "title" else regenerate_preview(src, ma, mn)
            self.after(0, self._regen_done, kind, result)
        except Exception as ex:
            self.after(0, self._regen_error, kind, str(ex))

    def _regen_done(self, kind, result):
        if kind == "title":
            self._regen_title_sp.stop()
            self._regen_title_btn.set_enabled(True)
            self._data["ru"]["title"] = result
            if self._lang == "ru":
                self._title_entry.set(result)
            self._status("Заголовок обновлён", "success")
        else:
            self._regen_prev_sp.stop()
            self._regen_prev_btn.set_enabled(True)
            self._data["ru"]["preview"] = result
            if self._lang == "ru":
                self._preview_entry.set(result)
                self._on_preview_key()
            self._status("Анонс обновлён", "success")
        self._refresh_usage()
        self._update_card()
        self._on_ru_edit()

    def _regen_error(self, kind, msg):
        if kind == "title":
            self._regen_title_sp.stop()
            self._regen_title_btn.set_enabled(True)
        else:
            self._regen_prev_sp.stop()
            self._regen_prev_btn.set_enabled(True)
        self._status(f"Ошибка перегенерации: {msg}", "error")

    # ──────────────────────────────────────────────────────────────
    #  ПЕРЕВОД НА БЕЛОРУССКИЙ
    # ──────────────────────────────────────────────────────────────

    _retranslate_timer = None

    def _on_ru_edit(self):
        """Вызывается при любом ручном изменении RU-полей."""
        if not self._publish_by_var.get():
            return
        # Если перевод уже был — отметить как устаревший
        if self._data["by"].get("title"):
            self._translation_fresh = False
            self._by_status.config(text="⚠ Перевод устарел", fg=C["warning"])
        self._schedule_retranslate()
        self._save_draft()

    def _schedule_retranslate(self):
        if not self._publish_by_var.get():
            return
        if self._retranslate_timer:
            self.after_cancel(self._retranslate_timer)
        self._retranslate_timer = self.after(4000, self._do_scheduled_translate)

    def _do_scheduled_translate(self):
        if self._lang == "ru":
            self._data["ru"]["title"]   = self._title_entry.get()
            self._data["ru"]["preview"] = self._preview_entry.get()
            self._data["ru"]["text"]    = self._text_box.get()
        d = self._data["ru"]
        if d["title"]:
            self._start_translate(d["title"], d["preview"], d["text"])

    def _start_translate(self, title, preview, html):
        self._by_status.config(text="⏳ Перевожу на белорусский...", fg=C["accent"])
        threading.Thread(
            target=self._do_translate, args=(title, preview, html), daemon=True
        ).start()

    def _do_translate(self, title, preview, html):
        try:
            tb, pb, xb = translate_to_belarusian(title, preview, html)
            self.after(0, self._translate_done, tb, pb, xb)
        except Exception as ex:
            self.after(0, lambda e=str(ex): self._by_status.config(
                text=f"✗ Ошибка перевода: {e[:60]}", fg=C["danger"]
            ))

    def _translate_done(self, tb, pb, xb):
        self._data["by"]        = {"title": tb, "preview": pb, "text": xb}
        self._translation_fresh = True
        self._by_status.config(text="✓ Перевод готов", fg=C["success"])
        if self._lang == "by":
            self._load_lang("by")
        self._save_draft()

    # ──────────────────────────────────────────────────────────────
    #  ВЫБОР ФОТО
    # ──────────────────────────────────────────────────────────────

    def _on_pick_photo(self):
        if not self._image_urls:
            messagebox.showinfo("Фото", "В посте нет фотографий.", parent=self)
            return
        dlg = PhotoPickerDialog(self, self._image_urls)
        self.wait_window(dlg)
        r = dlg.get_result()
        if r:
            self._preview_image_url = r
            idx = self._image_urls.index(r) + 1
            self._photo_lbl.config(
                text=f"Фото №{idx}  (доступно: {len(self._image_urls)})",
                fg=C["success"],
            )
            self._photo_preview.set_url(r)
            self._status(f"Анонсовое фото: №{idx}", "success")
            self._save_draft()

    # ──────────────────────────────────────────────────────────────
    #  ПУБЛИКАЦИЯ
    # ──────────────────────────────────────────────────────────────

    def _on_publish(self):
        # Сохраняем актуальные поля
        current_lang = self._lang
        self._data[current_lang]["title"]   = self._title_entry.get().strip()
        self._data[current_lang]["preview"] = self._preview_entry.get().strip()
        self._data[current_lang]["text"]    = self._text_box.get().strip()

        ru = self._data["ru"]
        by = self._data["by"]

        # ── Валидация ──────────────────────────────────────────
        issues = validate_publication(
            ru["title"], ru["preview"], ru["text"], self._preview_image_url
        )
        if issues:
            messagebox.showwarning(
                "Проверьте данные",
                "Перед публикацией устраните следующее:\n\n• " + "\n• ".join(issues),
                parent=self,
            )
            return

        publish_by = self._publish_by_var.get()
        if publish_by and not by.get("title"):
            messagebox.showwarning(
                "Ошибка",
                "Белорусская версия ещё не переведена.\n"
                "Дождитесь перевода или снимите галочку.",
                parent=self,
            )
            return

        # Берём время из редактируемого поля
        active_from = self._active_entry.get() or datetime.now().strftime("%d.%m.%Y %H:%M:%S")

        self._publish_loading(True)
        self._status("Отправляю на сайт...", "loading")

        target_section = self._target_section_var.get()
        threading.Thread(
            target=self._publish_thread,
            args=(
                ru["title"], ru["preview"], ru["text"],
                list(self._image_urls), self._preview_image_url,
                active_from,
                by.get("title", ""), by.get("preview", ""), by.get("text", ""),
                publish_by, target_section,
            ),
            daemon=True,
        ).start()

    def _publish_thread(self, *args):
        try:
            result = send_to_bitrix(*args)
            self.after(0, self._publish_done, result, args[0])
        except Exception as ex:
            self.after(0, self._publish_done, {"status": "error", "msg": str(ex)}, args[0])

    def _publish_done(self, result, title):
        self._publish_loading(False)
        if result.get("status") == "success":
            id_ru  = result.get("id_ru") or result.get("id")
            id_by  = result.get("id_by")
            err_by = result.get("err_by")
            log_publication(self._tg_url, title, id_ru)
            clear_draft()

            parts = [f"Русская версия: ID {id_ru}"]
            if id_by:
                parts.append(f"Белорусская версия: ID {id_by}")
            elif err_by:
                parts.append(f"Белорусская версия — ошибка: {err_by}")

            self._status(
                f"✓  Опубликовано. RU:{id_ru}" + (f"  BY:{id_by}" if id_by else ""),
                "success",
            )
            messagebox.showinfo("Успех", "Новость создана!\n\n" + "\n".join(parts), parent=self)
            self._reset()
        else:
            msg = result.get("msg", "Неизвестная ошибка")
            self._status(f"Ошибка: {msg}", "error")
            messagebox.showerror("Ошибка Битрикса", msg, parent=self)

    # ──────────────────────────────────────────────────────────────
    #  ВСПОМОГАТЕЛЬНЫЕ
    # ──────────────────────────────────────────────────────────────

    def _on_preview_key(self, _=None):
        raw = self._preview_entry.entry.get()
        n   = 0 if self._preview_entry._ph_on else len(raw)
        lim = PREVIEW_MAX_LEN
        color = C["danger"] if n > lim else C["warning"] if n > int(lim * 0.85) else C["text_dim"]
        self._char_lbl.config(text=f"{n} / {lim}", fg=color)
        self._update_card()

    def _status(self, text, kind="idle"):
        self._status_bar.set(text, kind)

    def _loading(self, state, msg=""):
        self.config(cursor="watch" if state else "")
        self._fetch_btn.set_enabled(not state)
        if state:
            self._fetch_spinner.start()
            self._cancel_btn.pack(side="left", padx=(8, 0))
            self._status(msg, "loading")
        else:
            self._fetch_spinner.stop()
            self._cancel_btn.pack_forget()

    def _publish_loading(self, state):
        self.config(cursor="watch" if state else "")
        self._publish_btn.set_enabled(not state)
        if state:
            self._publish_sp.pack(side="right", padx=(0, 8))
            self._publish_sp.start()
        else:
            self._publish_sp.stop()

    def _reset(self):
        self._url_entry.clear()
        self._warn_video.config(text="")
        self._warn_dup.config(text="")
        self._photo_lbl.config(text="Фото не выбрано", fg=C["text_dim"])
        self._photo_preview.clear()
        self._active_entry.clear()
        self._active_src_lbl.config(text="")
        self._by_status.config(text="")
        self._image_urls        = []
        self._preview_image_url = None
        self._tg_url            = ""
        self._post_text         = ""
        self._active_from       = None
        self._translation_fresh = False
        self._data = {
            "ru": {"title": "", "preview": "", "text": ""},
            "by": {"title": "", "preview": "", "text": ""},
        }
        self._lang = "ru"
        self._load_lang("ru")
        self._update_tab_style()
        self._char_lbl.config(text=f"0 / {PREVIEW_MAX_LEN}", fg=C["text_dim"])
        self._announce_card.update("", "", "")
        self._status("Готов к работе")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()