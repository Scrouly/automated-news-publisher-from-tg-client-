"""
Кастомные tkinter-виджеты: кнопки, поля ввода, статус-бар, поле времени.
"""

import tkinter as tk
from core.config import C


class RoundedButton(tk.Frame):
    def __init__(self, parent, text, command, width=160, height=42,
                 bg=None, fg=C["white"], hover_bg=None,
                 radius=10, font_size=11, canvas_bg=C["bg"], **kwargs):
        self._bg       = bg       or C["accent"]
        self._hover_bg = hover_bg or C["accent_dark"]
        self._fg       = fg
        self._cmd      = command
        self._enabled  = True

        super().__init__(parent, bg=canvas_bg, **kwargs)

        self._inner = tk.Label(
            self, text=text,
            font=("Segoe UI", font_size, "bold"),
            fg=fg, bg=self._bg,
            width=width // (font_size + 2),
            padx=14,
            pady=(height - font_size * 2) // 2,
            cursor="hand2",
        )
        self._inner.pack(fill="both", expand=True)

        for w in (self, self._inner):
            w.bind("<Enter>",    lambda e: self._on_hover(True))
            w.bind("<Leave>",    lambda e: self._on_hover(False))
            w.bind("<Button-1>", lambda e: self._on_click())

    def _on_hover(self, entering):
        if self._enabled:
            self._inner.config(bg=self._hover_bg if entering else self._bg)

    def _on_click(self):
        if self._enabled:
            self._cmd()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self._inner.config(
            bg=self._bg if enabled else C["border"],
            cursor="hand2" if enabled else "arrow",
        )


class StyledEntry(tk.Frame):
    """Поле ввода с placeholder и анимацией фокуса."""

    def __init__(self, parent, placeholder="", **kwargs):
        super().__init__(parent, bg=C["surface2"],
                         highlightbackground=C["border"], highlightthickness=1, **kwargs)
        self._ph    = placeholder
        self._ph_on = True

        self.entry = tk.Entry(self, bg=C["surface2"], fg=C["text_dim"],
                               relief="flat", bd=0, insertbackground=C["accent"],
                               font=("Segoe UI", 11), highlightthickness=0)
        self.entry.pack(fill="both", expand=True, padx=12, pady=8)
        self.entry.insert(0, placeholder)

        self.entry.bind("<FocusIn>",  self._on_focus)
        self.entry.bind("<FocusOut>", self._on_blur)
        self.bind("<Enter>", lambda e: self.config(highlightbackground=C["border_hi"]))
        self.bind("<Leave>", lambda e: self.config(
            highlightbackground=C["accent"] if self.entry == self.focus_get()
            else C["border"]))

    def _on_focus(self, _):
        if self._ph_on:
            self.entry.delete(0, "end")
            self.entry.config(fg=C["text"])
            self._ph_on = False
        self.config(highlightbackground=C["accent"])

    def _on_blur(self, _):
        if not self.entry.get():
            self.entry.insert(0, self._ph)
            self.entry.config(fg=C["text_dim"])
            self._ph_on = True
        self.config(highlightbackground=C["border"])

    def get(self) -> str:
        return "" if self._ph_on else self.entry.get()

    def set(self, value: str):
        self.entry.delete(0, "end")
        self.entry.insert(0, value)
        self.entry.config(fg=C["text"])
        self._ph_on = False

    def clear(self):
        self.entry.delete(0, "end")
        self.entry.insert(0, self._ph)
        self.entry.config(fg=C["text_dim"])
        self._ph_on = True


class StyledText(tk.Frame):
    """Многострочное поле с кастомным скроллбаром."""

    def __init__(self, parent, height=5, **kwargs):
        super().__init__(parent, bg=C["surface2"],
                         highlightbackground=C["border"], highlightthickness=1, **kwargs)
        self.text = tk.Text(
            self, bg=C["surface2"], fg=C["text"], relief="flat", bd=0,
            insertbackground=C["accent"], font=("Segoe UI", 11),
            highlightthickness=0, wrap="word", height=height,
            spacing3=4, padx=2, pady=2,
        )
        scroll = tk.Scrollbar(
            self, orient="vertical", command=self.text.yview,
            bg=C["surface2"], troughcolor=C["surface2"],
            activebackground=C["border_hi"], width=8, relief="flat",
        )
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True, padx=12, pady=8)
        scroll.pack(side="right", fill="y", pady=4)

        self.bind("<Enter>", lambda e: self.config(highlightbackground=C["border_hi"]))
        self.bind("<Leave>", lambda e: self.config(highlightbackground=C["border"]))

    def get(self) -> str:
        return self.text.get("1.0", "end-1c")

    def set(self, value: str):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", value)

    def clear(self):
        self.text.delete("1.0", "end")


class TimeEntry(tk.Frame):
    """
    Редактируемое поле даты/времени формата ДД.ММ.ГГГГ ЧЧ:ММ:СС.
    При потере фокуса проверяет формат и подсвечивает красным если неверный.
    """
    FMT = "%d.%m.%Y %H:%M:%S"

    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=C["surface2"],
                         highlightbackground=C["border"], highlightthickness=1, **kwargs)
        self._var = tk.StringVar()
        self._entry = tk.Entry(
            self, textvariable=self._var,
            bg=C["surface2"], fg=C["text"],
            relief="flat", bd=0,
            font=("Segoe UI", 10),
            insertbackground=C["accent"],
            highlightthickness=0,
            width=20,
        )
        self._entry.pack(fill="both", expand=True, padx=10, pady=6)

        self._entry.bind("<FocusIn>",  self._on_focus)
        self._entry.bind("<FocusOut>", self._on_blur)
        self.bind("<Enter>", lambda e: self.config(highlightbackground=C["border_hi"]))
        self.bind("<Leave>", lambda e: self.config(highlightbackground=C["border"]))

    def _on_focus(self, _):
        self.config(highlightbackground=C["accent"])

    def _on_blur(self, _):
        val = self._var.get().strip()
        if val:
            try:
                from datetime import datetime as _dt
                _dt.strptime(val, self.FMT)
                self._entry.config(fg=C["text"])
                self.config(highlightbackground=C["border"])
            except ValueError:
                self._entry.config(fg=C["danger"])
                self.config(highlightbackground=C["danger"])
        else:
            self.config(highlightbackground=C["border"])

    def get(self) -> str:
        return self._var.get().strip()

    def set(self, value: str):
        self._var.set(value)
        self._entry.config(fg=C["text"])

    def clear(self):
        self._var.set("")
        self._entry.config(fg=C["text"])
        self.config(highlightbackground=C["border"])


class StatusBar(tk.Frame):
    """Нижняя статусная строка с цветным индикатором."""

    COLORS = {
        "idle":    C["text_faint"],
        "loading": C["accent"],
        "success": C["success"],
        "error":   C["danger"],
        "warn":    C["warning"],
    }

    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=C["surface"], height=36, **kwargs)
        self._dot = tk.Label(self, text="●", font=("Segoe UI", 10),
                              bg=C["surface"], fg=C["text_faint"])
        self._dot.pack(side="left", padx=(16, 6), pady=6)
        self._label = tk.Label(self, text="Готов к работе", font=("Segoe UI", 10),
                                bg=C["surface"], fg=C["text_dim"])
        self._label.pack(side="left", pady=6)

    def set(self, text: str, kind: str = "idle"):
        color = self.COLORS.get(kind, C["text_dim"])
        self._dot.config(fg=color)
        self._label.config(text=text, fg=C["text_dim"] if kind == "idle" else color)