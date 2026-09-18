"""Formula Clip: a lightweight Windows formula screenshot-to-LaTeX app."""
import base64
import configparser
import ctypes
import json
import os
import threading
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

from PIL import ImageGrab, ImageTk
from pynput import keyboard

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
PROMPT = """Read the mathematical formula in this image and return standard LaTeX.
Ignore surrounding prose, page numbers, and captions. Return only JSON with this exact schema:
{\"formula\": \"yes\", \"content\": \"LaTeX without dollar delimiters\"}
If there is no formula return {\"formula\": \"no\", \"content\": \"\"}. Do not use Markdown fences."""


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


class CaptureOverlay:
    def __init__(self, app, image):
        self.app = app
        self.image = image
        self.start = None
        self.rectangle = None
        self.window = tk.Toplevel(app.root)
        self.window.attributes("-fullscreen", True)
        self.window.attributes("-topmost", True)
        self.window.configure(cursor="crosshair", bg="#101828")
        self.canvas = tk.Canvas(self.window, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.preview = ImageTk.PhotoImage(image)
        self.canvas.create_image(0, 0, image=self.preview, anchor="nw")
        self.canvas.bind("<ButtonPress-1>", self.begin)
        self.canvas.bind("<B1-Motion>", self.move)
        self.canvas.bind("<ButtonRelease-1>", self.finish)
        self.window.bind("<Escape>", lambda _event: self.cancel())
        self.window.focus_force()

    def begin(self, event):
        self.start = (event.x, event.y)
        self.rectangle = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#7dd3fc", width=2)

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
        self.window.destroy()
        if right - left < 8 or bottom - top < 8:
            self.app.show_status("截图区域太小，请重新选择。", "warn")
            self.app.root.deiconify()
            return
        self.app.recognize(self.image.crop((left, top, right, bottom)))

    def cancel(self):
        self.window.destroy()
        self.app.root.deiconify()
        self.app.show_status("已取消截图。", "normal")


class FormulaClip:
    def __init__(self):
        self.settings = Settings()
        self.root = tk.Tk()
        self.root.title("Formula Clip")
        self.root.geometry("780x650")
        self.root.minsize(720, 580)
        self.root.configure(bg="#0f172a")
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.hotkey_listener = None
        self.build_ui()
        self.start_hotkey()

    def build_ui(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        self.root.configure(bg="#ffffff")
        style.configure("Page.TFrame", background="#ffffff")
        style.configure("Card.TFrame", background="#ffffff")
        ui_font = "Microsoft YaHei UI"
        style.configure("Title.TLabel", background="#ffffff", foreground="#1d1d1f", font=(ui_font, 27, "bold"))
        style.configure("Sub.TLabel", background="#ffffff", foreground="#6e6e73", font=(ui_font, 11))
        style.configure("Card.TLabel", background="#ffffff", foreground="#1d1d1f", font=(ui_font, 10))
        style.configure("Hint.TLabel", background="#ffffff", foreground="#86868b", font=(ui_font, 9))
        style.configure("Accent.TButton", font=(ui_font, 10, "bold"), padding=(18, 10), background="#1d1d1f", foreground="#ffffff", borderwidth=0)
        style.map("Accent.TButton", background=[("active", "#424245")])
        style.configure("Plain.TButton", font=(ui_font, 10), padding=(14, 9), background="#ffffff", foreground="#1d1d1f", bordercolor="#d2d2d7")
        style.map("Plain.TButton", background=[("active", "#f5f5f7")])
        style.configure("TEntry", fieldbackground="#ffffff", foreground="#1d1d1f", font=(ui_font, 10), padding=8, bordercolor="#d2d2d7", lightcolor="#d2d2d7", darkcolor="#d2d2d7")
        style.configure("TCombobox", fieldbackground="#ffffff", foreground="#1d1d1f", font=(ui_font, 10), padding=7)

        page = ttk.Frame(self.root, padding=(42, 28), style="Page.TFrame")
        page.pack(fill="both", expand=True)
        container = ttk.Frame(page, padding=26, style="Card.TFrame")
        container.pack(fill="both", expand=True)
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
                ttk.Button(grid, text="录入", style="Plain.TButton", command=self.record_hotkey).grid(row=row, column=2, padx=(10, 0), pady=8)
        grid.columnconfigure(1, weight=1)

        actions = ttk.Frame(container, style="Card.TFrame")
        actions.pack(fill="x", pady=(20, 12))
        ttk.Button(actions, text="保存配置", style="Plain.TButton", command=self.save_settings).pack(side="left")
        ttk.Button(actions, text="获取模型", style="Plain.TButton", command=self.fetch_models).pack(side="left", padx=(10, 0))
        self.capture_button = ttk.Button(actions, text=f"开始截图  {self.fields['hotkey'].get() or 'F1'}", style="Accent.TButton", command=self.capture)
        self.capture_button.pack(side="right")

        ttk.Separator(container, orient="horizontal").pack(fill="x", pady=(4, 14))
        ttk.Label(container, text="识别结果", style="Card.TLabel").pack(anchor="w", pady=(0, 7))
        self.output = tk.Text(container, height=6, bg="#ffffff", fg="#1d1d1f", insertbackground="#1d1d1f", relief="solid", borderwidth=1, font=("Cascadia Mono", 11), wrap="word", padx=12, pady=10)
        self.output.pack(fill="both", expand=True)
        self.status = ttk.Label(container, text="准备就绪。填写支持图片输入的模型后，按 Shift+Space 开始截图。", style="Hint.TLabel")
        self.status.pack(anchor="w", pady=(10, 0))

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
        self.show_status("完成：LaTeX 已复制到剪贴板。", "ok")

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
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    FormulaClip().run()
