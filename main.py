"""Formula Clip: a lightweight Windows formula screenshot-to-LaTeX app."""
import base64
import configparser
import ctypes
import json
import os
import sys
import threading
from datetime import datetime
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageEnhance, ImageGrab, ImageTk
import pystray
from pynput import keyboard

try:
    import winreg
except ImportError:
    winreg = None

# Render at the monitor's native DPI before Tk creates any window.  Without
# this declaration Windows may bitmap-scale the whole UI and blur text.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "FormulaClip"
CONFIG_PATH = APP_DIR / "config.ini"
HISTORY_DIR = APP_DIR / "history"
HISTORY_PATH = APP_DIR / "history.json"
PROMPT = """Read the mathematical formula in this image and return standard LaTeX.
Ignore surrounding prose, page numbers, and captions. Return only JSON with this exact schema:
{\"formula\": \"yes\", \"content\": \"LaTeX without dollar delimiters\"}
If there is no formula return {\"formula\": \"no\", \"content\": \"\"}. Do not use Markdown fences."""


def resource_path(name):
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / name


class Settings:
    def __init__(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.data = configparser.ConfigParser()
        self.data["api"] = {
            "base_url": "http://127.0.0.1:8080/v1",
            "api_key": "",
            "model": "gpt-5.5",
            "hotkey": "Shift+Space",
            "provider": "OpenAI 兼容",
            "autostart": "0",
            "keep_history": "1",
            "history_limit": "100",
            "show_toast": "1",
        }
        if CONFIG_PATH.exists():
            self.data.read(CONFIG_PATH, encoding="utf-8")
            if not self.data.has_section("api"):
                self.data["api"] = {}
        self.save()

    def get(self, name):
        return self.data.get("api", name, fallback="")

    def save_values(self, values):
        for key, value in values.items():
            self.data.set("api", key, value.strip())
        self.save()

    def save(self):
        with CONFIG_PATH.open("w", encoding="utf-8") as file:
            self.data.write(file)


class HistoryStore:
    def __init__(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    def read(self):
        if not HISTORY_PATH.exists():
            return []
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []

    def add(self, image, latex, limit):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        image_path = HISTORY_DIR / f"{stamp}.png"
        image.save(image_path, format="PNG")
        items = self.read()
        items.insert(0, {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "latex": latex, "image": str(image_path)})
        if limit != "0":
            items = items[: int(limit)]
        HISTORY_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        return items[0]


class ToggleSwitch(tk.Frame):
    def __init__(self, parent, variable, **kwargs):
        super().__init__(parent, bg="#ffffff", width=52, height=30, **kwargs)
        self.variable = variable
        self.canvas = tk.Canvas(self, width=52, height=30, bg="#ffffff", highlightthickness=0)
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self.toggle)
        self.variable.trace_add("write", lambda *_: self.draw())
        self.draw()

    def toggle(self, _event=None):
        self.variable.set(not self.variable.get())

    def draw(self):
        self.canvas.delete("all")
        active = self.variable.get()
        color = "#007aff" if active else "#d1d1d6"
        self.canvas.create_rectangle(12, 4, 40, 26, fill=color, outline=color)
        self.canvas.create_oval(1, 4, 23, 26, fill=color, outline=color)
        self.canvas.create_oval(29, 4, 51, 26, fill=color, outline=color)
        x = 39 if active else 13
        self.canvas.create_oval(x - 9, 6, x + 9, 24, fill="#ffffff", outline="#ffffff")

    def get(self):
        return bool(self.variable.get())


class CaptureOverlay:
    def __init__(self, app, image):
        self.app = app
        self.image = image
        self.start = None
        self.rectangle = None
        self.selected_box = None
        self.action_bar = None
        self.window = tk.Toplevel(app.root)
        self.window.attributes("-fullscreen", True)
        self.window.attributes("-topmost", True)
        self.window.configure(cursor="crosshair", bg="#101828")
        self.canvas = tk.Canvas(self.window, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        dimmed = ImageEnhance.Brightness(image).enhance(0.38)
        self.preview = ImageTk.PhotoImage(dimmed)
        self.canvas.create_image(0, 0, image=self.preview, anchor="nw")
        self.canvas.bind("<ButtonPress-1>", self.begin)
        self.canvas.bind("<B1-Motion>", self.move)
        self.canvas.bind("<ButtonRelease-1>", self.finish)
        self.window.bind("<Escape>", lambda _event: self.cancel())
        self.window.focus_force()

    def begin(self, event):
        self.start = (event.x, event.y)
        self.rectangle = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#007aff", width=3)

    def move(self, event):
        if self.start and self.rectangle:
            self.canvas.coords(self.rectangle, self.start[0], self.start[1], event.x, event.y)

    def finish(self, event):
        if not self.start:
            return
        left, top = self.start
        right, bottom = event.x, event.y
        left, right = sorted((left, right))
        top, bottom = sorted((top, bottom))
        if right - left < 8 or bottom - top < 8:
            self.window.destroy()
            self.app.show_status("截图区域太小，请重新选择。", "warn")
            self.app.root.deiconify()
            return
        self.selected_box = (left, top, right, bottom)
        if self.action_bar:
            self.action_bar.destroy()
        self.action_bar = tk.Frame(self.window, bg="#ffffff", padx=8, pady=6)
        self.action_bar.place(x=max(12, min(left, self.window.winfo_screenwidth() - 170)), y=min(bottom + 12, self.window.winfo_screenheight() - 58))
        tk.Button(self.action_bar, text="识别", command=self.confirm_selection, relief="flat", bd=0, bg="#007aff", fg="#ffffff", activebackground="#0066d6", activeforeground="#ffffff", font=("Microsoft YaHei UI", 10, "bold"), padx=14, pady=5).pack(side="left")
        tk.Button(self.action_bar, text="取消", command=self.cancel, relief="flat", bd=0, bg="#f2f2f7", fg="#1d1d1f", activebackground="#e5e5ea", font=("Microsoft YaHei UI", 10), padx=12, pady=5).pack(side="left", padx=(6, 0))

    def confirm_selection(self):
        if not self.selected_box:
            return
        box = self.selected_box
        self.window.destroy()
        self.app.recognize(self.image.crop(box))

    def cancel(self):
        self.window.destroy()
        self.app.root.deiconify()
        self.app.show_status("已取消截图。", "normal")


class FormulaClip:
    def __init__(self):
        self.settings = Settings()
        self.root = tk.Tk()
        self.root.title("Formula Clip")
        self.root.geometry("900x700")
        self.root.minsize(820, 620)
        self.root.configure(bg="#f5f5f7")
        try:
            self.root.iconbitmap(str(resource_path("assets/formulaclip-icon.ico")))
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.hotkey_listener = None
        self.tray_icon = None
        self.tray_thread = None
        self.history = HistoryStore()
        self.last_image = None
        self.history_rows = {}
        self.build_ui()
        self.apply_autostart()
        self.start_hotkey()
        self.start_tray()

    def start_tray(self):
        icon_image = Image.open(resource_path("assets/formulaclip-icon.png")).convert("RGBA")
        menu = pystray.Menu(
            pystray.MenuItem("打开 Formula Clip", lambda: self.root.after(0, self.show_from_tray)),
            pystray.MenuItem("退出程序", lambda: self.root.after(0, self.quit)),
        )
        self.tray_icon = pystray.Icon("FormulaClip", icon_image, "Formula Clip", menu)
        self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        self.tray_thread.start()

    def hide_to_tray(self):
        self.root.withdraw()

    def show_from_tray(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def build_ui(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        self.root.configure(bg="#f5f5f7")
        style.configure("Page.TFrame", background="#f5f5f7")
        style.configure("Card.TFrame", background="#ffffff")
        ui_font = "Microsoft YaHei UI"
        style.configure("Title.TLabel", background="#ffffff", foreground="#1d1d1f", font=(ui_font, 25, "bold"))
        style.configure("Sub.TLabel", background="#ffffff", foreground="#6e6e73", font=(ui_font, 11))
        style.configure("Card.TLabel", background="#ffffff", foreground="#1d1d1f", font=(ui_font, 10))
        style.configure("Hint.TLabel", background="#ffffff", foreground="#86868b", font=(ui_font, 9))
        style.configure("Accent.TButton", font=(ui_font, 10, "bold"), padding=(18, 10), background="#007aff", foreground="#ffffff", borderwidth=0, relief="flat")
        style.map("Accent.TButton", background=[("active", "#0066d6")])
        style.configure("TEntry", fieldbackground="#ffffff", foreground="#1d1d1f", font=(ui_font, 10), padding=8, bordercolor="#d2d2d7", lightcolor="#d2d2d7", darkcolor="#d2d2d7")
        style.configure("TCombobox", fieldbackground="#ffffff", foreground="#1d1d1f", font=(ui_font, 10), padding=7)

        page = ttk.Frame(self.root, padding=0, style="Page.TFrame")
        page.pack(fill="both", expand=True)
        sidebar = tk.Frame(page, bg="#f0f0f2", width=220)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="Formula Clip", bg="#f0f0f2", fg="#1d1d1f", font=(ui_font, 15, "bold"), anchor="w").pack(fill="x", padx=24, pady=(30, 34))
        content = ttk.Frame(page, style="Page.TFrame")
        content.pack(side="right", fill="both", expand=True)
        api_tab = ttk.Frame(content, padding=(46, 38), style="Card.TFrame")
        settings_tab = ttk.Frame(content, padding=(46, 38), style="Card.TFrame")
        history_tab = ttk.Frame(content, padding=(46, 38), style="Card.TFrame")
        self.pages = {"识别": api_tab, "历史记录": history_tab, "设置": settings_tab}
        for text in ("识别", "历史记录", "设置"):
            button = tk.Button(sidebar, text=text, anchor="w", relief="flat", bd=0, bg="#f0f0f2", activebackground="#dfeaff", activeforeground="#1264d8", fg="#4b4b50", font=(ui_font, 11), padx=24, pady=11, command=lambda name=text: self.show_page(name))
            button.pack(fill="x")
        tk.Label(sidebar, text="\n快捷键\n" + (self.settings.get("hotkey") or "Shift+Space"), justify="left", anchor="w", bg="#f0f0f2", fg="#86868b", font=(ui_font, 9), padx=24).pack(side="bottom", fill="x", pady=(0, 28))
        container = api_tab
        ttk.Label(container, text="Formula Clip", style="Title.TLabel").pack(anchor="w")
        ttk.Label(container, text="截取公式，识别并复制 LaTeX。", style="Sub.TLabel").pack(anchor="w", pady=(4, 18))

        grid = ttk.Frame(container, style="Card.TFrame")
        grid.pack(fill="x")
        self.fields = {}
        labels = [("接口类型", "provider"), ("API 地址", "base_url"), ("API 密钥", "api_key"), ("视觉模型", "model"), ("截图快捷键", "hotkey")]
        for row, (label, key) in enumerate(labels):
            ttk.Label(grid, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 18), pady=8)
            variable = tk.StringVar(value=self.settings.get(key))
            if key == "provider":
                entry = ttk.Combobox(grid, textvariable=variable, width=51, state="readonly", values=("OpenAI 兼容", "OpenAI", "DeepSeek", "Sub2API"))
                entry.bind("<<ComboboxSelected>>", self.apply_provider)
            elif key == "model":
                entry = ttk.Combobox(grid, textvariable=variable, width=51)
                self.model_select = entry
            else:
                entry = ttk.Entry(grid, textvariable=variable, width=54, show="•" if key == "api_key" else "")
            entry.grid(row=row, column=1, sticky="ew", pady=8)
            self.fields[key] = variable
            if key == "hotkey":
                tk.Button(grid, text="录入快捷键", command=self.record_hotkey, relief="flat", bd=0, bg="#f2f2f7", fg="#1d1d1f", activebackground="#e5e5ea", font=(ui_font, 9), padx=12, pady=7).grid(row=row, column=2, padx=(10, 0), pady=8)
        grid.columnconfigure(1, weight=1)

        actions = ttk.Frame(container, style="Card.TFrame")
        actions.pack(fill="x", pady=(20, 12))
        tk.Button(actions, text="保存配置", command=self.save_settings, relief="flat", bd=0, bg="#f2f2f7", fg="#1d1d1f", activebackground="#e5e5ea", font=(ui_font, 10), padx=16, pady=9).pack(side="left")
        tk.Button(actions, text="获取模型", command=self.fetch_models, relief="flat", bd=0, bg="#f2f2f7", fg="#1d1d1f", activebackground="#e5e5ea", font=(ui_font, 10), padx=16, pady=9).pack(side="left", padx=(10, 0))
        tk.Button(actions, text="测试连接", command=self.test_api, relief="flat", bd=0, bg="#f2f2f7", fg="#1d1d1f", activebackground="#e5e5ea", font=(ui_font, 10), padx=16, pady=9).pack(side="left", padx=(10, 0))
        self.capture_button = ttk.Button(actions, text=f"开始截图  {self.fields['hotkey'].get() or 'F1'}", style="Accent.TButton", command=self.capture)
        self.capture_button.pack(side="right")

        ttk.Separator(container, orient="horizontal").pack(fill="x", pady=(4, 14))
        ttk.Label(container, text="识别结果", style="Card.TLabel").pack(anchor="w", pady=(0, 7))
        self.output = tk.Text(container, height=6, bg="#ffffff", fg="#1d1d1f", insertbackground="#1d1d1f", relief="solid", borderwidth=1, font=("Cascadia Mono", 11), wrap="word", padx=12, pady=10)
        self.output.pack(fill="both", expand=True)
        self.status = ttk.Label(container, text="准备就绪。填写支持图片输入的模型后，按 Shift+Space 开始截图。", style="Hint.TLabel")
        self.status.pack(anchor="w", pady=(10, 0))

        self.build_settings_tab(settings_tab)
        self.build_history_tab(history_tab)
        self.show_page("识别")

    def show_page(self, name):
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="both", expand=True)

    def build_settings_tab(self, parent):
        ttk.Label(parent, text="应用设置", style="Title.TLabel").pack(anchor="w")
        ttk.Label(parent, text="控制启动方式、历史记录和识别完成提示。", style="Sub.TLabel").pack(anchor="w", pady=(4, 24))
        self.autostart_var = tk.BooleanVar(value=self.settings.get("autostart") == "1")
        self.keep_history_var = tk.BooleanVar(value=self.settings.get("keep_history") != "0")
        self.show_toast_var = tk.BooleanVar(value=self.settings.get("show_toast") != "0")
        for text, variable in (
            ("开机时自动启动 Formula Clip", self.autostart_var),
            ("保留识别历史记录（包含截图和 LaTeX）", self.keep_history_var),
            ("识别成功后在右下角显示 1.5 秒提示", self.show_toast_var),
        ):
            row = tk.Frame(parent, bg="#ffffff", height=48)
            row.pack(fill="x", pady=4)
            row.pack_propagate(False)
            tk.Label(row, text=text, bg="#ffffff", fg="#1d1d1f", font=("Microsoft YaHei UI", 10), anchor="w").pack(side="left", fill="x", expand=True)
            ToggleSwitch(row, variable).pack(side="right", padx=4)
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", pady=10)
        ttk.Label(row, text="最多保留记录", style="Card.TLabel").pack(side="left")
        self.history_limit_var = tk.StringVar(value=self.settings.get("history_limit") or "100")
        ttk.Combobox(row, textvariable=self.history_limit_var, state="readonly", width=16, values=("20", "50", "100", "200", "不限制")).pack(side="left", padx=20)
        tk.Button(parent, text="保存设置", command=self.save_app_settings, relief="flat", bd=0, bg="#007aff", fg="#ffffff", activebackground="#0066d6", activeforeground="#ffffff", font=("Microsoft YaHei UI", 10, "bold"), padx=20, pady=10).pack(anchor="w", pady=(22, 0))

    def build_history_tab(self, parent):
        ttk.Label(parent, text="历史记录", style="Title.TLabel").pack(anchor="w")
        ttk.Label(parent, text="查看过去识别的截图和 LaTeX 结果。", style="Sub.TLabel").pack(anchor="w", pady=(4, 14))
        self.history_list = tk.Frame(parent, bg="#ffffff")
        self.history_list.pack(fill="both", expand=True)
        self.history_preview = tk.Text(parent, height=5, bg="#ffffff", fg="#1d1d1f", relief="solid", borderwidth=1, font=("Cascadia Mono", 10), wrap="word", padx=10, pady=8)
        self.history_preview.pack(fill="x", pady=(14, 0))
        self.refresh_history()

    def refresh_history(self):
        if not hasattr(self, "history_list"):
            return
        for child in self.history_list.winfo_children():
            child.destroy()
        self.history_rows = {}
        self.history_thumbs = []
        items = self.history.read()
        if not items:
            tk.Label(self.history_list, text="还没有识别记录", bg="#ffffff", fg="#86868b", font=("Microsoft YaHei UI", 11)).pack(pady=36)
            return
        for item in items:
            card = tk.Frame(self.history_list, bg="#f8f8fa", padx=12, pady=10, cursor="hand2")
            card.pack(fill="x", pady=(0, 8))
            thumb = tk.Label(card, bg="#ffffff", width=110, height=58)
            thumb.pack(side="left", padx=(0, 12))
            try:
                preview = Image.open(item.get("image", ""))
                preview.thumbnail((110, 58))
                photo = ImageTk.PhotoImage(preview)
                thumb.configure(image=photo)
                self.history_thumbs.append(photo)
            except Exception:
                thumb.configure(text="公式", fg="#86868b", font=("Microsoft YaHei UI", 10))
            body = tk.Frame(card, bg="#f8f8fa")
            body.pack(side="left", fill="both", expand=True)
            tk.Label(body, text=item.get("time", ""), bg="#f8f8fa", fg="#86868b", font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x")
            latex = item.get("latex", "")
            tk.Label(body, text=latex if len(latex) < 100 else latex[:97] + "…", bg="#f8f8fa", fg="#1d1d1f", font=("Cascadia Mono", 10), anchor="w", justify="left").pack(fill="x", pady=(5, 0))
            def bind_row(widget):
                widget.bind("<Button-1>", lambda _event, selected=item: self.show_history_card(selected))
                widget.configure(cursor="hand2")
                for child in widget.winfo_children():
                    bind_row(child)
            bind_row(card)

    def show_history_item(self, _event=None):
        return

    def show_history_card(self, item):
        self.history_preview.delete("1.0", "end")
        self.history_preview.insert("1.0", item.get("latex", ""))
        self.root.clipboard_clear()
        self.root.clipboard_append(item.get("latex", ""))
        self.root.update()
        self.show_status("历史公式已复制到剪贴板。", "ok")
        if self.settings.get("show_toast") != "0":
            self.show_toast("历史公式已复制", "可以直接粘贴 LaTeX。")

    def save_app_settings(self):
        limit = self.history_limit_var.get()
        limit = "0" if limit == "不限制" else limit
        self.settings.save_values({"autostart": "1" if self.autostart_var.get() else "0", "keep_history": "1" if self.keep_history_var.get() else "0", "history_limit": limit, "show_toast": "1" if self.show_toast_var.get() else "0"})
        self.apply_autostart()
        self.show_status("应用设置已保存。", "ok")

    def apply_autostart(self):
        if not winreg:
            return
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
            if self.settings.get("autostart") == "1":
                command = f'"{Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()}"'
                winreg.SetValueEx(key, "FormulaClip", 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, "FormulaClip")
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception:
            self.show_status("开机自启动设置失败，请检查系统权限。", "warn")

    def save_settings(self):
        values = {key: value.get() for key, value in self.fields.items()}
        if not values["base_url"] or not values["api_key"] or not values["model"]:
            messagebox.showwarning("配置不完整", "请填写 API 地址、API 密钥和视觉模型。")
            return
        self.settings.save_values(values)
        self.start_hotkey()
        self.capture_button.configure(text=f"开始截图  {values['hotkey'] or 'F1'}")
        self.show_status("配置已保存。", "ok")

    def record_hotkey(self):
        """Capture a shortcut in-app, so users do not need to learn key syntax."""
        dialog = tk.Toplevel(self.root)
        dialog.title("录入截图快捷键")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        dialog.configure(bg="#ffffff")
        ttk.Label(dialog, text="请按下新的截图快捷键", style="Title.TLabel").pack(padx=30, pady=(24, 8))
        ttk.Label(dialog, text="例如 Shift + Space 或 Ctrl + Shift + S。按 Esc 取消。", style="Sub.TLabel").pack(padx=30, pady=(0, 22))

        pressed_modifiers = set()
        modifier_names = {
            "Shift_L": "Shift", "Shift_R": "Shift",
            "Control_L": "Ctrl", "Control_R": "Ctrl",
            "Alt_L": "Alt", "Alt_R": "Alt",
            "Meta_L": "Win", "Meta_R": "Win",
        }

        def on_modifier_down(event):
            name = modifier_names.get(event.keysym)
            if name:
                pressed_modifiers.add(name)
            return "break"

        def on_modifier_up(event):
            name = modifier_names.get(event.keysym)
            if name:
                pressed_modifiers.discard(name)
            return "break"

        def on_key(event):
            if event.keysym == "Escape":
                dialog.destroy()
                return "break"
            if event.keysym in modifier_names:
                pressed_modifiers.add(modifier_names[event.keysym])
                return "break"
            modifiers = [name for name in ("Ctrl", "Shift", "Alt", "Win") if name in pressed_modifiers]
            key = "Space" if event.keysym == "space" else event.keysym.upper() if len(event.keysym) == 1 else event.keysym
            shortcut = "+".join(modifiers + [key])
            self.fields["hotkey"].set(shortcut)
            dialog.destroy()
            self.show_status(f"快捷键已设为 {shortcut}；点击保存配置后生效。", "ok")
            return "break"

        dialog.bind("<KeyPress>", on_key)
        dialog.bind("<KeyPress-Shift_L>", on_modifier_down)
        dialog.bind("<KeyPress-Shift_R>", on_modifier_down)
        dialog.bind("<KeyPress-Control_L>", on_modifier_down)
        dialog.bind("<KeyPress-Control_R>", on_modifier_down)
        dialog.bind("<KeyPress-Alt_L>", on_modifier_down)
        dialog.bind("<KeyPress-Alt_R>", on_modifier_down)
        dialog.bind("<KeyPress-Meta_L>", on_modifier_down)
        dialog.bind("<KeyPress-Meta_R>", on_modifier_down)
        dialog.bind("<KeyRelease>", on_modifier_up)
        dialog.after(100, dialog.focus_force)

    def apply_provider(self, _event=None):
        presets = {
            "OpenAI": "https://api.openai.com/v1",
            "DeepSeek": "https://api.deepseek.com/v1",
            "Sub2API": "http://127.0.0.1:8080/v1",
        }
        provider = self.fields["provider"].get()
        if provider in presets:
            self.fields["base_url"].set(presets[provider])

    def fetch_models(self):
        base_url = self.fields["base_url"].get().strip().rstrip("/")
        api_key = self.fields["api_key"].get().strip()
        if not base_url or not api_key:
            messagebox.showwarning("缺少配置", "请先填写 API 地址和 API 密钥。")
            return
        self.show_status("正在读取模型列表…", "normal")
        threading.Thread(target=self.request_models, args=(base_url, api_key), daemon=True).start()

    def test_api(self):
        base_url = self.fields["base_url"].get().strip().rstrip("/")
        api_key = self.fields["api_key"].get().strip()
        model = self.fields["model"].get().strip()
        if not base_url or not api_key:
            messagebox.showwarning("配置不完整", "请先填写 API 地址和 API 密钥。")
            return
        self.show_status("正在测试 API 连接…", "normal")
        threading.Thread(target=self.request_api_test, args=(base_url, api_key, model), daemon=True).start()

    def request_api_test(self, base_url, api_key, model):
        try:
            request = urllib.request.Request(base_url + "/models", headers={"Authorization": "Bearer " + api_key})
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            models = [item.get("id") for item in payload.get("data", []) if item.get("id")]
            selected = model if model and model in models else (model or "当前模型")
            detail = f"API 连接成功。\n\n当前模型：{selected}\n可用模型数：{len(models)}"
            self.root.after(0, lambda: self.api_test_result(True, detail))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            self.root.after(0, lambda: self.api_test_result(False, f"HTTP {error.code}\n\n{detail[:500]}"))
        except Exception as error:
            self.root.after(0, lambda: self.api_test_result(False, str(error)))

    def api_test_result(self, success, detail):
        if success:
            self.show_status("API 测试成功，可以开始识别。", "ok")
            messagebox.showinfo("API 测试成功", detail)
        else:
            self.show_status("API 测试失败，请检查地址和密钥。", "warn")
            messagebox.showerror("API 测试失败", detail)

    def request_models(self, base_url, api_key):
        try:
            request = urllib.request.Request(base_url + "/models", headers={"Authorization": "Bearer " + api_key})
            with urllib.request.urlopen(request, timeout=30) as response:
                models = [item["id"] for item in json.loads(response.read().decode("utf-8")).get("data", []) if item.get("id")]
            if not models:
                raise ValueError("接口没有返回可用模型。")
            self.root.after(0, lambda: self.set_models(models))
        except Exception as error:
            self.root.after(0, lambda: self.show_error("获取模型失败：" + str(error)))

    def set_models(self, models):
        models = sorted(set(models), key=str.lower)
        self.model_select.configure(values=models)
        if self.fields["model"].get() not in models:
            self.fields["model"].set(models[0])
        self.show_status(f"已获取 {len(models)} 个模型；请选择支持图片输入的模型。", "ok")

    def start_hotkey(self):
        if self.hotkey_listener:
            self.hotkey_listener.stop()
        hotkey = self.fields["hotkey"].get().strip() if hasattr(self, "fields") else self.settings.get("hotkey")
        hotkey = hotkey or "Shift+Space"
        aliases = {"ctrl": "<ctrl>", "control": "<ctrl>", "shift": "<shift>", "alt": "<alt>", "win": "<cmd>"}
        parts = [part.strip().lower() for part in hotkey.replace("+", " ").split() if part.strip()]
        key_name = "+".join(aliases.get(part, f"<{part}>" if part.startswith("f") and part[1:].isdigit() else part) for part in parts)
        try:
            self.hotkey_listener = keyboard.GlobalHotKeys({key_name: lambda: self.root.after(0, self.capture)})
            self.hotkey_listener.start()
        except Exception:
            self.show_status("全局快捷键注册失败；可使用窗口内的“开始截图”按钮。", "warn")

    def capture(self):
        if self.capture_button.instate(["disabled"]):
            return
        self.root.withdraw()
        self.root.after(180, self.open_capture)

    def open_capture(self):
        try:
            image = ImageGrab.grab(all_screens=False)
            CaptureOverlay(self, image)
        except Exception as error:
            self.root.deiconify()
            messagebox.showerror("截图失败", str(error))

    def recognize(self, image):
        self.last_image = image.copy()
        api_key = self.fields["api_key"].get().strip()
        base_url = self.fields["base_url"].get().strip().rstrip("/")
        model = self.fields["model"].get().strip()
        if not api_key or not base_url or not model:
            self.root.deiconify()
            messagebox.showwarning("配置不完整", "请先填写并保存 API 配置。")
            return
        self.root.deiconify()
        self.capture_button.state(["disabled"])
        self.show_status("正在识别公式…", "normal")
        threading.Thread(target=self.request_model, args=(image, base_url, api_key, model), daemon=True).start()

    def request_model(self, image, base_url, api_key, model):
        try:
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            body = {
                "model": model,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
                ]}],
                "temperature": 0,
            }
            request = urllib.request.Request(
                base_url + "/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                content = json.loads(response.read().decode("utf-8"))["choices"][0]["message"]["content"]
            content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            result = json.loads(content)
            if result.get("formula") != "yes" or not result.get("content"):
                raise ValueError("图片中没有识别到公式。")
            self.root.after(0, lambda: self.show_result(result["content"]))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            self.root.after(0, lambda: self.show_error(f"API 请求失败（{error.code}）：{detail}"))
        except Exception as error:
            self.root.after(0, lambda: self.show_error(str(error)))

    def show_result(self, latex):
        self.output.delete("1.0", "end")
        self.output.insert("1.0", latex)
        self.root.clipboard_clear()
        self.root.clipboard_append(latex)
        self.root.update()
        self.capture_button.state(["!disabled"])
        if self.settings.get("keep_history") != "0" and self.last_image is not None:
            self.history.add(self.last_image, latex, self.settings.get("history_limit") or "100")
            self.refresh_history()
        if self.settings.get("show_toast") != "0":
            self.show_toast("公式识别完成", "LaTeX 已复制到剪贴板")
        self.show_status("完成：LaTeX 已复制到剪贴板。", "ok")

    def show_toast(self, title, text):
        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        toast.configure(bg="#1d1d1f")
        frame = tk.Frame(toast, bg="#1d1d1f", padx=18, pady=12)
        frame.pack()
        tk.Label(frame, text=title, bg="#1d1d1f", fg="#ffffff", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        tk.Label(frame, text=text, bg="#1d1d1f", fg="#d2d2d7", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(3, 0))
        toast.update_idletasks()
        width, height = toast.winfo_width(), toast.winfo_height()
        x = toast.winfo_screenwidth() - width - 24
        y = toast.winfo_screenheight() - height - 48
        toast.geometry(f"{width}x{height}+{x}+{y}")
        toast.after(1500, toast.destroy)

    def show_error(self, message):
        self.capture_button.state(["!disabled"])
        self.show_status("识别失败，请检查 API 配置和模型是否支持图片输入。", "warn")
        messagebox.showerror("公式识别失败", message)

    def show_status(self, text, kind):
        colors = {"ok": "#86efac", "warn": "#fbbf24", "normal": "#cbd5e1"}
        self.status.configure(text=text, foreground=colors.get(kind, "#cbd5e1"))

    def quit(self):
        if self.hotkey_listener:
            self.hotkey_listener.stop()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    FormulaClip().run()
