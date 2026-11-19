"""
Диалоговые окна приложения.
"""

import os
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from core.config import C
from core.logic import load_image_thumbnail, read_publications
from desktop.widgets import RoundedButton
from PIL import Image, ImageTk

IMAGE_FILETYPES = [
    ("Изображения", "*.jpg *.jpeg *.png *.webp *.gif *.bmp"),
    ("Все файлы", "*.*"),
]


class ImageManagerDialog(tk.Toplevel):
    """
    Модальное окно управления изображениями новости: удалить, заменить, добавить
    (по ссылке или с компьютера), поменять порядок и выбрать анонсовое фото.
    Работает с копией списка — исходный меняется только по кнопке «Применить».
    Элемент списка: {"src": ссылка или путь к файлу, "kind": "url" | "file"}.
    """

    THUMB_SIZE      = (200, 134)
    COLS            = 3
    VISIBLE_ROWS_PX = 540

    def __init__(self, parent, images: list, preview_src: str | None,
                 allow_local: bool = False):
        super().__init__(parent)
        self.title("Изображения новости")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.grab_set()

        self._items       = [dict(i) for i in images]
        self._preview     = preview_src
        self._allow_local = allow_local
        self._result      = None
        self._alive       = True

        self._pil_cache: dict = {}      # src → PIL.Image (None — не удалось открыть)
        self._photos: dict    = {}      # src → PhotoImage (защита от GC)
        self._loading: set    = set()
        self._labels: dict    = {}      # src → [Label, ...] на текущей отрисовке
        self._failed: set     = set()

        self._build_ui()
        self._render()
        self._center_on(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", lambda e: self._canvas.yview_scroll(-1, "units"))
        self.bind("<Button-5>", lambda e: self._canvas.yview_scroll(1, "units"))

    # ── Построение UI ──────────────────────────────────────────

    def _build_ui(self):
        tk.Label(self, text="Изображения новости",
                 font=("Segoe UI", 14, "bold"),
                 bg=C["bg"], fg=C["text"]).pack(pady=(20, 2), padx=24, anchor="w")
        tk.Label(self, text="Кликните по фото, чтобы сделать его анонсовым. "
                            "Стрелки ◀ ▶ меняют порядок, ✕ удаляет.",
                 font=("Segoe UI", 10), bg=C["bg"], fg=C["text_dim"]
                 ).pack(padx=24, pady=(0, 12), anchor="w")

        holder = tk.Frame(self, bg=C["bg"])
        holder.pack(padx=24, fill="both", expand=True)

        card_w = self.THUMB_SIZE[0] + 28
        width  = card_w * self.COLS + 20
        self._canvas = tk.Canvas(holder, bg=C["bg"], highlightthickness=0,
                                  width=width, height=self.VISIBLE_ROWS_PX)
        vsb = tk.Scrollbar(holder, orient="vertical", command=self._canvas.yview,
                            width=10, relief="flat")
        self._canvas.configure(yscrollcommand=vsb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self._grid = tk.Frame(self._canvas, bg=C["bg"])
        self._canvas.create_window((0, 0), window=self._grid, anchor="nw")
        self._grid.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))

        self._status = tk.Label(self, text="", font=("Segoe UI", 10),
                                 bg=C["bg"], fg=C["accent"])
        self._status.pack(pady=(10, 0), padx=24, anchor="w")

        btn_row = tk.Frame(self, bg=C["bg"])
        btn_row.pack(fill="x", padx=24, pady=(8, 20))

        if self._allow_local:
            RoundedButton(btn_row, "＋ С компьютера…", self._add_files,
                          width=200, height=38, font_size=10,
                          bg=C["surface2"], hover_bg=C["border"],
                          canvas_bg=C["bg"]).pack(side="left", padx=(0, 8))
        RoundedButton(btn_row, "＋ По ссылке…", self._add_url,
                      width=170, height=38, font_size=10,
                      bg=C["surface2"], hover_bg=C["border"],
                      canvas_bg=C["bg"]).pack(side="left")

        RoundedButton(btn_row, "✕  Отмена", self._on_close,
                      width=130, height=38, font_size=10,
                      bg=C["surface2"], hover_bg=C["border"],
                      canvas_bg=C["bg"]).pack(side="right")
        RoundedButton(btn_row, "✓  Применить", self._apply,
                      width=170, height=38, font_size=10, bg=C["success"],
                      hover_bg="#27B87A", canvas_bg=C["bg"]).pack(side="right", padx=(0, 8))

    # ── Отрисовка ──────────────────────────────────────────────

    def _render(self):
        y = self._canvas.yview()[0]
        for w in self._grid.winfo_children():
            w.destroy()
        self._labels = {}

        if not self._items:
            tk.Label(self._grid, text="Изображений нет. Добавьте их кнопками ниже.",
                     font=("Segoe UI", 11), bg=C["bg"], fg=C["text_dim"],
                     width=60, height=8).grid(row=0, column=0)
        for i, item in enumerate(self._items):
            self._build_card(i, item)
        self._update_status()
        self.update_idletasks()
        # ширина окна под три карточки (по размеру первой), чтобы колонки не обрезались
        first = next((w for w in self._grid.winfo_children()
                      if isinstance(w, tk.Frame)), None)
        if first is not None:
            self._canvas.config(width=(first.winfo_reqwidth() + 16) * self.COLS)
        self._canvas.yview_moveto(y)

    def _build_card(self, i: int, item: dict):
        src      = item["src"]
        selected = src == self._preview
        failed   = src in self._failed
        bg       = C["selected_bg"] if selected else C["surface2"]
        cell = tk.Frame(self._grid, bg=bg, highlightthickness=2,
                         highlightbackground=(C["selected_bd"] if selected
                                              else C["danger"] if failed else C["border"]))
        cell.grid(row=i // self.COLS, column=i % self.COLS, padx=8, pady=8)

        box = tk.Frame(cell, width=self.THUMB_SIZE[0], height=self.THUMB_SIZE[1],
                        bg=C["surface2"], cursor="hand2")
        box.pack(padx=6, pady=(6, 4))
        box.pack_propagate(False)
        lbl = tk.Label(box, bg=C["surface2"], fg=C["text_dim"], font=("Segoe UI", 10),
                        text=("Не удалось\nзагрузить" if failed else "⏳"),
                        cursor="hand2")
        if failed:
            lbl.config(fg=C["danger"])
        lbl.place(relx=0.5, rely=0.5, anchor="center")
        self._labels.setdefault(src, []).append((lbl, cell))

        title_row = tk.Frame(cell, bg=bg)
        title_row.pack(fill="x", padx=8)
        kind = "файл" if item.get("kind") == "file" else "ссылка"
        tk.Label(title_row, text=f"№ {i + 1} · {kind}", font=("Segoe UI", 9),
                 bg=bg, fg=C["text_dim"]).pack(side="left")
        if selected:
            tk.Label(title_row, text="АНОНС", font=("Segoe UI", 9, "bold"),
                     bg=bg, fg=C["accent"]).pack(side="right")

        btns = tk.Frame(cell, bg=bg)
        btns.pack(fill="x", padx=6, pady=(4, 8))
        self._small_btn(btns, "◀", lambda i=i: self._move(i, -1),
                        disabled=i == 0).pack(side="left")
        self._small_btn(btns, "▶", lambda i=i: self._move(i, 1),
                        disabled=i == len(self._items) - 1).pack(side="left", padx=(2, 0))
        rep = self._small_btn(btns, "Заменить", None, width=8)
        rep.config(command=lambda i=i, w=rep: self._replace_menu(i, w))
        rep.pack(side="left", padx=(6, 0))
        self._small_btn(btns, "✕", lambda i=i: self._delete(i),
                        fg=C["danger"]).pack(side="right")

        for w in (cell, box, lbl, title_row):
            w.bind("<Button-1>", lambda e, i=i: self._select(i))

        if src in self._pil_cache:
            self._show_thumb(src)
        elif src not in self._loading:
            self._loading.add(src)
            threading.Thread(target=self._load_thumb, args=(src,), daemon=True).start()

    def _small_btn(self, parent, text, command, disabled=False, fg=None, width=3):
        return tk.Button(
            parent, text=text, command=command, width=width,
            font=("Segoe UI", 9), relief="flat", bd=0, cursor="hand2",
            highlightthickness=0, takefocus=0,
            bg=C["surface"], fg=fg or C["text"],
            activebackground=C["border_hi"], activeforeground=C["text"],
            disabledforeground=C["text_faint"],
            state="disabled" if disabled else "normal",
        )

    def _update_status(self):
        present = {it["src"] for it in self._items}
        parts = []
        if self._loading & present:
            parts.append("⏳ Загрузка превью…")
        bad = self._failed & present
        if bad:
            parts.append(f"Не удалось загрузить: {len(bad)} (отмечены красным)")
        self._status.config(text="   ".join(parts),
                            fg=C["danger"] if bad else C["accent"])

    # ── Превью ─────────────────────────────────────────────────

    def _load_thumb(self, src: str):
        pil = load_image_thumbnail(src, size=self.THUMB_SIZE)
        try:
            self.after(0, self._thumb_loaded, src, pil)
        except Exception:      # окно уже закрыто
            pass

    def _thumb_loaded(self, src: str, pil):
        self._loading.discard(src)
        if not self._alive:
            return
        self._pil_cache[src] = pil
        if pil is None:
            self._failed.add(src)
        self._show_thumb(src)
        self._update_status()

    def _show_thumb(self, src: str):
        pil = self._pil_cache.get(src)
        for lbl, cell in self._labels.get(src, []):
            try:
                if pil is None:
                    lbl.config(text="Не удалось\nзагрузить", fg=C["danger"], image="")
                    cell.config(highlightbackground=C["danger"])
                else:
                    if src not in self._photos:
                        self._photos[src] = ImageTk.PhotoImage(pil)
                    lbl.config(image=self._photos[src], text="")
                    lbl.image = self._photos[src]
            except tk.TclError:
                pass

    # ── Действия ───────────────────────────────────────────────

    def _select(self, i: int):
        self._preview = self._items[i]["src"]
        self._render()

    def _move(self, i: int, step: int):
        j = i + step
        if 0 <= j < len(self._items):
            self._items[i], self._items[j] = self._items[j], self._items[i]
            self._render()

    def _delete(self, i: int):
        removed = self._items.pop(i)
        if removed["src"] == self._preview:
            self._preview = (self._items[min(i, len(self._items) - 1)]["src"]
                             if self._items else None)
        self._render()

    def _exists(self, src: str, ignore: int | None = None) -> bool:
        return any(it["src"] == src for j, it in enumerate(self._items) if j != ignore)

    def _ask_url(self) -> dict | None:
        url = simpledialog.askstring(
            "Добавить по ссылке", "Ссылка на изображение (https://…):", parent=self)
        if not url:
            return None
        url = url.strip()
        if not re.match(r'^https?://\S+$', url):
            messagebox.showwarning("Ссылка", "Нужна прямая ссылка вида https://…",
                                   parent=self)
            return None
        return {"src": url, "kind": "url"}

    def _ask_files(self, multiple: bool) -> list[dict]:
        if multiple:
            paths = filedialog.askopenfilenames(
                parent=self, title="Выберите изображения", filetypes=IMAGE_FILETYPES)
        else:
            one = filedialog.askopenfilename(
                parent=self, title="Выберите изображение", filetypes=IMAGE_FILETYPES)
            paths = (one,) if one else ()
        result = []
        for path in paths:
            path = os.path.abspath(path)
            try:
                with Image.open(path) as img:
                    img.verify()
            except Exception:
                messagebox.showwarning(
                    "Файл", f"Не удалось открыть как изображение:\n{os.path.basename(path)}",
                    parent=self)
                continue
            result.append({"src": path, "kind": "file"})
        return result

    def _add_url(self):
        item = self._ask_url()
        if item is None:
            return
        if self._exists(item["src"]):
            messagebox.showinfo("Ссылка", "Это изображение уже есть в списке.", parent=self)
            return
        self._items.append(item)
        self._render()

    def _add_files(self):
        added = 0
        for item in self._ask_files(multiple=True):
            if not self._exists(item["src"]):
                self._items.append(item)
                added += 1
        if added:
            self._render()

    def _replace_menu(self, i: int, widget):
        menu = tk.Menu(self, tearoff=0, bg=C["surface2"], fg=C["text"],
                       activebackground=C["accent"], activeforeground=C["white"])
        if self._allow_local:
            menu.add_command(label="Файлом с компьютера…",
                             command=lambda: self._replace(i, local=True))
        menu.add_command(label="Ссылкой…", command=lambda: self._replace(i, local=False))
        menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())

    def _replace(self, i: int, local: bool):
        if local:
            files = self._ask_files(multiple=False)
            item = files[0] if files else None
        else:
            item = self._ask_url()
        if item is None:
            return
        if self._exists(item["src"], ignore=i):
            messagebox.showinfo("Замена", "Это изображение уже есть в списке.", parent=self)
            return
        old = self._items[i]["src"]
        self._items[i] = item
        if old == self._preview:
            self._preview = item["src"]
        self._render()

    # ── Прокрутка ──────────────────────────────────────────────

    def _on_wheel(self, event):
        self._canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    # ── Подтверждение / закрытие ───────────────────────────────

    def _apply(self):
        present = {it["src"] for it in self._items}
        bad = self._failed & present
        if bad and not messagebox.askyesno(
            "Не все изображения загрузились",
            f"Не удалось загрузить превью: {len(bad)} шт. (красные рамки).\n"
            "Если это ссылки, сайт может не суметь их скачать.\n\nПрименить всё равно?",
            parent=self,
        ):
            return
        preview = self._preview if self._preview in present else (
            self._items[0]["src"] if self._items else None)
        self._result = ([dict(i) for i in self._items], preview)
        self._on_close()

    def _on_close(self):
        self._alive = False
        self.destroy()

    def get_result(self) -> tuple | None:
        """(список изображений, анонсовое фото) или None, если окно закрыли без применения."""
        return self._result

    # ── Центрирование ──────────────────────────────────────────

    def _center_on(self, parent):
        self.update_idletasks()
        px = parent.winfo_rootx() + parent.winfo_width()  // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2
        w  = self.winfo_width()
        h  = self.winfo_height()
        self.geometry(f"+{max(px - w // 2, 0)}+{max(py - h // 2, 0)}")


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
