"""
Диалоговые окна приложения.
"""

import tkinter as tk
import threading
from tkinter import ttk

from core.config import C
from core.logic import download_image_thumbnail, read_publications
from desktop.widgets import RoundedButton
from PIL import ImageTk


class PhotoPickerDialog(tk.Toplevel):
    """
    Модальное окно для визуального выбора анонсового фото.
    Превью загружаются в фоне, выбранное фото подсвечивается.
    """

    THUMB_SIZE = (240, 160)
    COLS       = 3

    def __init__(self, parent, image_urls: list):
        super().__init__(parent)
        self.title("Выбор фото для анонса")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.grab_set()

        self._urls     = image_urls
        self._thumbs   = {}     # index → PhotoImage (защита от GC)
        self._frames   = []
        self._selected = 0
        self._result   = None
        self._alive    = True

        self._build_ui()
        self._select(0)
        threading.Thread(target=self._load_thumbs, daemon=True).start()
        self._center_on(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Построение UI ──────────────────────────────────────────

    def _build_ui(self):
        tk.Label(self, text="Выберите фото для анонса",
                 font=("Segoe UI", 14, "bold"),
                 bg=C["bg"], fg=C["text"]).pack(pady=(24, 4), padx=24)
        tk.Label(self, text="Кликните по фотографии, затем нажмите «Подтвердить»",
                 font=("Segoe UI", 10), bg=C["bg"], fg=C["text_dim"]).pack(pady=(0, 16))

        grid = tk.Frame(self, bg=C["bg"])
        grid.pack(padx=24)

        cols = min(self.COLS, len(self._urls))
        for i, url in enumerate(self._urls):
            wrap = tk.Frame(grid, bg=C["bg"])
            wrap.grid(row=i // cols, column=i % cols, padx=8, pady=8)

            cell = tk.Frame(wrap, bg=C["surface2"],
                             highlightbackground=C["border"], highlightthickness=2,
                             cursor="hand2")
            cell.pack()

            ph = tk.Label(cell, text=f"Фото {i+1}",
                           width=22, height=9,
                           bg=C["surface2"], fg=C["text_dim"],
                           font=("Segoe UI", 10))
            ph.pack(padx=2, pady=2)

            num = tk.Label(cell, text=f"№ {i+1}",
                            font=("Segoe UI", 9, "bold"),
                            bg=C["surface2"], fg=C["text_dim"])
            num.pack(pady=(0, 6))

            self._frames.append({"cell": cell, "ph": ph, "num": num, "index": i})

            for w in (cell, ph, num):
                w.bind("<Button-1>", lambda e, idx=i: self._select(idx))

        self._status = tk.Label(self, text="⏳ Загрузка превью...",
                                 font=("Segoe UI", 10), bg=C["bg"], fg=C["accent"])
        self._status.pack(pady=8)

        btn_row = tk.Frame(self, bg=C["bg"])
        btn_row.pack(pady=(4, 20))

        RoundedButton(btn_row, "✓  Подтвердить", self._confirm,
                      width=180, height=42, bg=C["success"],
                      hover_bg="#27B87A", canvas_bg=C["bg"]).pack(side="left", padx=8)
        RoundedButton(btn_row, "✕  Отмена", self._on_close,
                      width=130, height=42,
                      bg=C["surface2"], hover_bg=C["border"],
                      canvas_bg=C["bg"]).pack(side="left", padx=8)

    # ── Загрузка превью ────────────────────────────────────────

    def _load_thumbs(self):
        for i, url in enumerate(self._urls):
            if not self._alive:
                return
            pil_img = download_image_thumbnail(url, size=self.THUMB_SIZE)
            if pil_img and self._alive:
                # Конвертируем PIL.Image → ImageTk здесь, в GUI-слое
                thumb = ImageTk.PhotoImage(pil_img)
                self._thumbs[i] = thumb
                self.after(0, self._safe_apply_thumb, i, thumb)
        if self._alive:
            self.after(0, self._safe_clear_status)

    def _safe_apply_thumb(self, i: int, thumb):
        if not self._alive:
            return
        try:
            f = self._frames[i]
            f["ph"].config(image=thumb, text="", width=0, height=0)
            f["ph"].image = thumb
        except tk.TclError:
            pass

    def _safe_clear_status(self):
        if not self._alive:
            return
        try:
            self._status.config(text="")
        except tk.TclError:
            pass

    # ── Выбор ──────────────────────────────────────────────────

    def _select(self, idx: int):
        for f in self._frames:
            selected = f["index"] == idx
            f["cell"].config(
                highlightbackground=C["selected_bd"] if selected else C["border"],
                bg=C["selected_bg"] if selected else C["surface2"],
            )
            f["ph"].config(bg=C["selected_bg"] if selected else C["surface2"])
            f["num"].config(
                bg=C["selected_bg"] if selected else C["surface2"],
                fg=C["accent"] if selected else C["text_dim"],
            )
        self._selected = idx

    # ── Подтверждение / закрытие ───────────────────────────────

    def _confirm(self):
        self._result = self._urls[self._selected]
        self._on_close()

    def _on_close(self):
        self._alive = False
        self.destroy()

    def get_result(self) -> str | None:
        return self._result

    # ── Центрирование ──────────────────────────────────────────

    def _center_on(self, parent):
        self.update_idletasks()
        px = parent.winfo_rootx() + parent.winfo_width()  // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2
        w  = self.winfo_width()
        h  = self.winfo_height()
        self.geometry(f"+{px - w//2}+{py - h//2}")


# ─────────────────────────────────────────────────────────────────
#  ИСТОРИЯ ПУБЛИКАЦИЙ
# ─────────────────────────────────────────────────────────────────

class HistoryDialog(tk.Toplevel):
    """Окно просмотра истории публикаций из publications_log.csv."""

    COL_WIDTHS = {"date": 140, "title": 380, "bitrix_id": 80, "tg_url": 300}
    COL_NAMES  = {"date": "Дата", "title": "Заголовок", "bitrix_id": "ID", "tg_url": "Ссылка TG"}

    def __init__(self, parent):
        super().__init__(parent)
        self.title("История публикаций")
        self.configure(bg=C["bg"])
        self.geometry("960x540")
        self.minsize(700, 400)
        self.grab_set()
        self._build_ui()
        self._load_data()
        self._center_on(parent)

    def _build_ui(self):
        # Заголовок
        hdr = tk.Frame(self, bg=C["surface"], height=52)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="История публикаций", font=("Segoe UI", 13, "bold"),
                 bg=C["surface"], fg=C["text"]).pack(side="left", padx=20, pady=14)

        self._count_lbl = tk.Label(hdr, text="", font=("Segoe UI", 10),
                                    bg=C["surface"], fg=C["text_dim"])
        self._count_lbl.pack(side="left", padx=4)

        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        # Поиск
        search_row = tk.Frame(self, bg=C["bg"])
        search_row.pack(fill="x", padx=16, pady=10)
        tk.Label(search_row, text="🔍", font=("Segoe UI", 11),
                 bg=C["bg"], fg=C["text_dim"]).pack(side="left", padx=(0, 6))
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._filter())
        search_entry = tk.Entry(search_row, textvariable=self._search_var,
                                 bg=C["surface2"], fg=C["text"],
                                 relief="flat", bd=0, insertbackground=C["accent"],
                                 font=("Segoe UI", 10), highlightthickness=1,
                                 highlightbackground=C["border"],
                                 highlightcolor=C["accent"])
        search_entry.pack(side="left", fill="x", expand=True, ipady=6, ipadx=8)

        # Таблица
        style = ttk.Style()
        style.theme_use("default")
        style.configure("History.Treeview",
                         background=C["surface2"], fieldbackground=C["surface2"],
                         foreground=C["text"], rowheight=28,
                         font=("Segoe UI", 10))
        style.configure("History.Treeview.Heading",
                         background=C["surface"], foreground=C["text_dim"],
                         font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("History.Treeview",
                  background=[("selected", C["selected_bg"])],
                  foreground=[("selected", C["accent"])])

        cols = list(self.COL_NAMES.keys())
        frame = tk.Frame(self, bg=C["bg"])
        frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        self._tree = ttk.Treeview(frame, columns=cols, show="headings",
                                   style="History.Treeview", selectmode="browse")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)

        for col in cols:
            self._tree.heading(col, text=self.COL_NAMES[col],
                                command=lambda c=col: self._sort(c))
            self._tree.column(col, width=self.COL_WIDTHS[col], minwidth=60, anchor="w")

        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Кнопка закрыть
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")
        btn_row = tk.Frame(self, bg=C["bg"])
        btn_row.pack(pady=12)
        RoundedButton(btn_row, "Закрыть", self.destroy,
                      width=120, height=36, bg=C["surface2"],
                      hover_bg=C["border"], canvas_bg=C["bg"]).pack()

    def _load_data(self):
        self._all_rows = read_publications()
        self._sort_col = "date"
        self._sort_rev = False
        self._render(self._all_rows)

    def _render(self, rows: list[dict]):
        self._tree.delete(*self._tree.get_children())
        for row in rows:
            self._tree.insert("", "end", values=(
                row.get("date", ""),
                row.get("title", ""),
                row.get("bitrix_id", ""),
                row.get("tg_url", ""),
            ))
        total = len(self._all_rows)
        shown = len(rows)
        self._count_lbl.config(
            text=f"{shown} из {total}" if shown != total else f"Всего: {total}"
        )

    def _filter(self):
        q = self._search_var.get().lower()
        if not q:
            self._render(self._all_rows)
            return
        filtered = [r for r in self._all_rows
                    if q in r.get("title", "").lower()
                    or q in r.get("tg_url", "").lower()
                    or q in r.get("date", "").lower()]
        self._render(filtered)

    def _sort(self, col: str):
        if self._sort_col == col:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col = col
            self._sort_rev = False
        self._all_rows.sort(key=lambda r: r.get(col, ""), reverse=self._sort_rev)
        self._filter()

    def _center_on(self, parent):
        self.update_idletasks()
        px = parent.winfo_rootx() + parent.winfo_width()  // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"+{px - self.winfo_width()//2}+{py - self.winfo_height()//2}")
