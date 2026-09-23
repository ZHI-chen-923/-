# -*- coding: utf-8 -*-
"""每日时事新闻汇报 —— 桌面主程序（Tkinter + ttkbootstrap）。

功能：
  - 今日简报（一句话新闻）+ 百度/腾讯/B站/知乎 四大平台热搜
  - 单击：跳转浏览器（简报条目按标题搜索、热搜条目打开原文）；右键：复制单条
  - 启动即显示本地缓存并后台刷新，支持手动刷新与定时自动刷新
  - 深浅主题切换、窗口置顶、一键复制全部

用法：
  python main.py           正常启动（推荐用 启动.bat）
  python main.py --selftest  自检模式：运行数秒后自动退出
"""
import json
import os
import queue
import sys
import tkinter as tk
import traceback
import urllib.parse
import webbrowser
from datetime import datetime

import ttkbootstrap as ttk
from ttkbootstrap.constants import VERTICAL
from ttkbootstrap.themes.legacy import theme_from_legacy_dict
from ttkbootstrap.themes.standard import STANDARD_THEMES

from news_service import CHANNELS, NewsService

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

GRAY_PRIMARY = "#5a6268"  # 去蓝：主色统一为中性灰
THEME_DARK = "news-dark"  # 基于内置 darkly 生成的灰色版深色主题
THEME_LIGHT = "news-light"  # 基于内置 flatly 生成的灰色版浅色主题

DEFAULT_CONFIG = {
    "theme": THEME_DARK,         # 主题：news-dark(深色) / news-light(浅色)
    "refresh_minutes": 30,       # 自动刷新间隔（分钟）
    "always_on_top": False,      # 窗口置顶
}

WEEKDAYS_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

F_HEAD = ("Microsoft YaHei UI", 15, "bold")
F_SUB = ("Microsoft YaHei UI", 9)
F_DATE = ("Microsoft YaHei UI", 12, "bold")
F_TITLE = ("Microsoft YaHei UI", 11)
F_RANK = ("Microsoft YaHei UI", 10, "bold")
F_HOT = ("Microsoft YaHei UI", 9)
F_STATUS = ("Microsoft YaHei UI", 9)

RANK_STYLES = ["danger", "warning", "success"]  # 前三名徽章配色（红/橙/绿）


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


class App:
    def __init__(self):
        self.cfg = load_config()
        self.root = ttk.Window()
        self._register_themes()
        theme = self._resolve_theme(self.cfg["theme"])
        if theme != self.cfg["theme"]:
            self.cfg["theme"] = theme  # 规范化旧配置值（darkly/flatly）
            save_config(self.cfg)
        self.root.style.theme_use(theme)
        self.root.title("每日时事新闻汇报")
        self.root.geometry("1020x720")
        self.root.minsize(880, 600)

        self.service = NewsService()

        self.kind = "brief"            # 当前频道
        self.nav_buttons = {}
        self._flash_job = None         # 状态栏提示恢复计时器
        self._status_base = ""         # 状态栏基础文本
        self._refresh_pending = False  # 手动刷新进行中
        self._refresh_watchdog = None

        self._build_ui()
        self._apply_topmost()
        self.switch_kind("brief")

        # 启动：立即渲染缓存，后台拉取最新数据
        self.service.refresh_all()
        self.root.after(300, self._poll_events)
        self._schedule_auto_refresh()

    # ---------- 主题 ----------
    @staticmethod
    def _resolve_theme(name):
        """兼容旧配置值：darkly 系 -> 深色，flatly 系 -> 浅色。"""
        return THEME_LIGHT if ("light" in name or name == "flatly") else THEME_DARK

    def _register_themes(self):
        """基于内置 darkly/flatly 注册去蓝版主题（主色改为中性灰）。"""
        for src, dst in (("darkly", THEME_DARK), ("flatly", THEME_LIGHT)):
            spec = STANDARD_THEMES[src]
            colors = {**spec["colors"], "primary": GRAY_PRIMARY}
            self.root.style.register_theme(
                theme_from_legacy_dict(dst, {"type": spec["type"], "colors": colors}))

    # ---------- 界面搭建 ----------
    def _color(self, name):
        return getattr(self.root.style.colors, name)

    def _build_ui(self):
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(1, weight=1)

        # ----- 顶栏：日期 + 操作按钮 -----
        top = ttk.Frame(self.root, padding=(16, 12, 16, 4))
        top.grid(row=0, column=0, columnspan=2, sticky="ew")

        self.date_label = ttk.Label(top, text="", font=F_DATE)
        self.date_label.pack(side="left")

        theme_btn_text = "深色" if self.cfg["theme"] == THEME_LIGHT else "浅色"
        self.theme_btn = ttk.Button(top, text=theme_btn_text,
                                    bootstyle="secondary-outline",
                                    command=self.toggle_theme, width=8)
        self.theme_btn.pack(side="right")

        self.top_var = tk.BooleanVar(value=bool(self.cfg.get("always_on_top")))
        self.top_check = ttk.Checkbutton(top, text="置顶", variable=self.top_var,
                                         bootstyle="round-toggle",
                                         command=self.toggle_topmost)
        self.top_check.pack(side="right", padx=(0, 10))

        self.copy_btn = ttk.Button(top, text="复制全部", bootstyle="secondary-outline",
                                   command=self.copy_all)
        self.copy_btn.pack(side="right", padx=(0, 8))

        self.refresh_btn = ttk.Button(top, text="刷新", bootstyle="primary-outline",
                                      command=self.manual_refresh)
        self.refresh_btn.pack(side="right", padx=(0, 8))

        # ----- 左侧频道导航 -----
        side = ttk.Frame(self.root, padding=(12, 6, 8, 10), width=136)
        side.grid(row=1, column=0, sticky="ns")
        side.grid_propagate(False)

        ttk.Label(side, text="频道", font=F_STATUS,
                  bootstyle="secondary").pack(anchor="w", padx=8, pady=(6, 6))
        for kind, name in CHANNELS.items():
            btn = ttk.Button(side, text=name, bootstyle="secondary-outline",
                             command=lambda k=kind: self.switch_kind(k))
            btn.pack(fill="x", pady=3, ipady=6)
            self.nav_buttons[kind] = btn

        # ----- 内容区：标题 + 滚动列表 -----
        body = ttk.Frame(self.root, padding=(6, 4, 16, 4))
        body.grid(row=1, column=1, sticky="nsew")
        body.grid_rowconfigure(1, weight=1)
        body.grid_columnconfigure(0, weight=1)

        head_wrap = ttk.Frame(body)
        head_wrap.grid(row=0, column=0, sticky="ew", pady=(4, 2))
        self.head_label = ttk.Label(head_wrap, text="", font=F_HEAD)
        self.head_label.pack(anchor="w")
        self.sub_label = ttk.Label(head_wrap, text="", font=F_SUB,
                                   bootstyle="secondary")
        self.sub_label.pack(anchor="w")

        wrap = ttk.Frame(body)
        wrap.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(wrap, highlightthickness=0, borderwidth=0,
                                bg=self._color("bg"))
        vsb = ttk.Scrollbar(wrap, orient=VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        self.inner = ttk.Frame(self.canvas, padding=(14, 6))
        self._win_id = self.canvas.create_window((0, 0), window=self.inner,
                                                 anchor="nw")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(
                            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win_id,
                                                             width=e.width))
        wrap.bind("<Enter>",
                  lambda e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        wrap.bind("<Leave>",
                  lambda e: self.canvas.unbind_all("<MouseWheel>"))

        # ----- 底部状态栏 -----
        status = ttk.Frame(self.root, padding=(16, 4, 16, 8))
        status.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.status_label = ttk.Label(status, text="", font=F_STATUS,
                                      bootstyle="secondary")
        self.status_label.pack(side="left")

        self._update_date_label()

    # ---------- 频道切换与渲染 ----------
    def switch_kind(self, kind):
        self.kind = kind
        for k, btn in self.nav_buttons.items():
            btn.configure(bootstyle="secondary" if k == kind else "secondary-outline")
        self._render()

    def _render(self):
        self.canvas.yview_moveto(0)
        for w in self.inner.winfo_children():
            w.destroy()

        name = CHANNELS[self.kind]
        cached = self.service.cached(self.kind)
        items = cached["items"] if cached else []

        if self.kind == "brief" and cached:
            meta = cached.get("meta", {})
            head = f"{name} · {meta.get('date', '')} {meta.get('day_of_week', '')}"
            sub = meta.get("tip") or ""
            if meta.get("lunar_date"):
                sub = (sub + "  " if sub else "") + f"农历 {meta['lunar_date']}"
            if meta.get("link"):
                sub = (sub + "　" if sub else "") + "（点击查看原文）"
                self.sub_label.bind("<Button-1>", lambda e: self._open_url(meta["link"]))
        else:
            head, sub = name, ""
            self.sub_label.unbind("<Button-1>")
        self.head_label.configure(text=head)
        self.sub_label.configure(text=sub)

        if not items:
            ttk.Label(self.inner, text="暂无数据", font=F_TITLE,
                      bootstyle="secondary").pack(pady=48)
            ttk.Label(self.inner, font=F_STATUS, bootstyle="secondary",
                      text="可能正在加载或网络不可用，请稍候或点击右上角“刷新”。"
                           "最近一次成功获取的数据会保留显示。").pack()
            self._update_status(self.kind)
            return

        wrap_len = max(420, (self.canvas.winfo_width() or 900) - 150)
        for item in items:
            self._make_row(item, wrap_len)
        self._update_status(self.kind)

    def _make_row(self, item, wrap_len):
        row = ttk.Frame(self.inner, padding=(6, 5))
        row.pack(fill="x", pady=2)

        rank = item.get("rank", 0)
        bs = RANK_STYLES[rank - 1] if 1 <= rank <= 3 else "secondary"
        ttk.Label(row, text=f"{rank:02d}", bootstyle=bs, font=F_RANK,
                  width=4).pack(side="left")

        title = tk.Label(row, text=item["title"], font=F_TITLE,
                         fg=self._color("fg"), bg=self._color("bg"),
                         anchor="w", justify="left", wraplength=wrap_len,
                         cursor="hand2")
        title.pack(side="left", fill="x", expand=True, padx=(10, 4))

        if item.get("hot"):
            ttk.Label(row, text=item["hot"], bootstyle="secondary",
                      font=F_HOT).pack(side="right", padx=(6, 4))

        for w in (row, title):
            w.bind("<Button-1>", lambda e, it=item: self._do_click(it))
            w.bind("<Button-3>", lambda e, it=item: self._copy_text(it["title"]))

    # ---------- 交互 ----------
    def _do_click(self, item):
        url = item.get("url")
        if not url:
            # 旧缓存中的简报条目没有链接，按标题补一个搜索链接
            url = "https://www.baidu.com/s?wd=" + urllib.parse.quote(item["title"])
        self._open_url(url)

    def _open_url(self, url):
        try:
            webbrowser.open(url)
            self._flash("已在浏览器中打开")
        except Exception:
            self._copy_text(url)

    # ---------- 剪贴板 ----------
    def _copy_text(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self._flash(f"已复制：{text[:22]}…")
        except Exception:
            self._flash("复制失败")

    def copy_all(self):
        cached = self.service.cached(self.kind)
        items = cached["items"] if cached else []
        if not items:
            self._flash("当前频道暂无内容")
            return
        name = CHANNELS[self.kind]
        lines = [f"{name}（{datetime.now().strftime('%Y-%m-%d %H:%M')}）"]
        for it in items:
            line = f"{it['rank']}. {it['title']}"
            if it.get("hot"):
                line += f"  [{it['hot']}]"
            lines.append(line)
        self._copy_text("\n".join(lines))

    # ---------- 刷新 ----------
    def manual_refresh(self):
        if self._refresh_pending:
            self._flash("正在刷新中…")
            return
        self._refresh_pending = True
        self.refresh_btn.configure(text="刷新中…", state="disabled")
        self.service.refresh_all()
        self._flash("正在刷新…")
        self._refresh_watchdog = self.root.after(20000, self._finish_refresh)

    def _finish_refresh(self):
        self._refresh_pending = False
        if self._refresh_watchdog:
            self.root.after_cancel(self._refresh_watchdog)
            self._refresh_watchdog = None
        self.refresh_btn.configure(text="刷新", state="normal")

    def _schedule_auto_refresh(self):
        interval = max(5, int(self.cfg.get("refresh_minutes", 30)))
        self.root.after(interval * 60_000, self._auto_tick)

    def _auto_tick(self):
        self.service.refresh_all()
        self._update_date_label()
        self._render()  # 顺带刷新过期标记
        self._schedule_auto_refresh()

    # ---------- 事件轮询（后台线程 -> UI） ----------
    def _poll_events(self):
        try:
            while True:
                kind, ok, err = self.service.events.get_nowait()
                # AI 聚合事件是每轮刷新的最后一个事件，收到即完成刷新
                if kind == "ai" and self._refresh_pending:
                    self._finish_refresh()
                if kind == self.kind:
                    if ok:
                        self._render()
                    else:
                        self._flash(f"{CHANNELS[kind]}更新失败，继续显示缓存")
                elif ok:
                    self._update_status(self.kind)
        except queue.Empty:
            pass
        self.root.after(300, self._poll_events)

    # ---------- 状态栏 ----------
    def _update_status(self, kind):
        name = CHANNELS[kind]
        cached = self.service.cached(kind)
        if not cached:
            base = f"{name} · 暂无数据"
        else:
            t = datetime.fromtimestamp(cached["fetched_at"]).strftime("%H:%M")
            base = f"{name} · 更新于 {t} · 共 {len(cached['items'])} 条"
            if cached["stale"]:
                base += " · 离线缓存"
        base += "　|　" + ("单击搜索该新闻 · 右键复制" if kind == "brief"
                       else "单击打开原文 · 右键复制")
        self._status_base = base
        if not self._flash_job:
            self.status_label.configure(text=base)

    def _flash(self, msg):
        self.status_label.configure(text=msg)
        if self._flash_job:
            self.root.after_cancel(self._flash_job)
        self._flash_job = self.root.after(2600, self._restore_status)

    def _restore_status(self):
        self._flash_job = None
        self.status_label.configure(text=self._status_base)

    # ---------- 设置 ----------
    def toggle_theme(self):
        new = THEME_LIGHT if self.cfg["theme"] == THEME_DARK else THEME_DARK
        self.cfg["theme"] = new
        save_config(self.cfg)
        self.root.style.theme_use(new)
        self.canvas.configure(bg=self._color("bg"))
        self.theme_btn.configure(text="深色" if new == THEME_LIGHT else "浅色")
        self._render()

    def toggle_topmost(self):
        self.cfg["always_on_top"] = bool(self.top_var.get())
        save_config(self.cfg)
        self._apply_topmost()

    def _apply_topmost(self):
        self.root.attributes("-topmost", bool(self.cfg.get("always_on_top")))

    # ---------- 其他 ----------
    def _update_date_label(self):
        now = datetime.now()
        self.date_label.configure(
            text=f"{now.year}年{now.month}月{now.day}日 {WEEKDAYS_CN[now.weekday()]}")

    def _on_wheel(self, event):
        if self.canvas.winfo_exists():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


def main():
    app = App()
    if "--selftest" in sys.argv:
        # 依次切换每个频道，验证各分类的渲染路径
        kinds = list(CHANNELS)

        def cycle(i=0):
            if i < len(kinds):
                app.switch_kind(kinds[i])
                app.root.after(1500, cycle, i + 1)

        app.root.after(4000, cycle)
        app.root.after(11000, app.toggle_theme)  # 验证主题切换路径
        app.root.after(13500, app.toggle_theme)  # 再切回，不改变用户配置
        app.root.after(16000, app.root.destroy)
        app.root.mainloop()
        print("SELFTEST-DONE")
        for kind in CHANNELS:
            cached = app.service.cached(kind)
            n = len(cached["items"]) if cached else 0
            print(f"  {kind}: cached_items={n} stale={cached['stale'] if cached else '-'}")
        return
    app.root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with open(os.path.join(BASE_DIR, "error.log"), "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now()}]\n{traceback.format_exc()}\n")
        raise
