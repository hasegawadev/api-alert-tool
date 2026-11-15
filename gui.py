from __future__ import annotations

from datetime import datetime
import json
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any
import webbrowser
import customtkinter as ctk

from api_client import (
    append_history_csv,
    evaluate_condition,
    fetch_exchange_rate,
    load_history_csv,
    mask_urls,
    send_webhook_notification,
)

# 全体テーマの初期設定
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# カラーパレット定義（ダーク / ライト両対応、ダーク基調は #121212 / #1E1E1E / #2E2E2E / #10B981）
COLOR_BG = ("#F3F4F6", "#121212")
COLOR_CARD_BG = ("#FFFFFF", "#1E1E1E")
COLOR_CARD_BORDER = ("#E5E7EB", "#2E2E2E")
COLOR_INPUT_BG = ("#F9FAFB", "#141414")
COLOR_ACCENT = "#10B981"
COLOR_ACCENT_HOVER = "#059669"
COLOR_GHOST_BORDER = "#4B5563"
COLOR_GHOST_HOVER = ("#E5E7EB", "#2A2A2A")
COLOR_DANGER = "#EF4444"
COLOR_DANGER_HOVER = "#DC2626"
COLOR_WARNING = "#F59E0B"
COLOR_START_DISABLED = ("#D1D5DB", "#2E2E2E")
COLOR_SECONDARY_BG = ("#E5E7EB", "#262626")
COLOR_SECONDARY_HOVER = ("#D1D5DB", "#333333")
COLOR_TEXT_MAIN = ("#111827", "#F9FAFB")
COLOR_TEXT_SUB = ("#6B7280", "#9CA3AF")

FONT_FAMILY = "Yu Gothic UI"
LOG_PLACEHOLDER_TEXT = "ここに実行ログが表示されます..."

CURRENCY_PAIRS = ["USD/JPY", "EUR/JPY", "GBP/JPY", "AUD/JPY"]
CONDITION_OPTIONS = ["以下になったら通知", "以上になったら通知"]

MIN_INTERVAL_SEC = 60
MAX_INTERVAL_SEC = 86400
DEFAULT_INTERVAL_SEC = 3600
EXCHANGE_RATE_API_URL = "https://www.exchangerate-api.com"


def get_base_dir() -> str:
    """実行ファイル（.exe）またはスクリプトと同階層のディレクトリを取得する。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_FILE = os.path.join(get_base_dir(), "config.json")
DEFAULT_OUTPUT_DIR = os.path.join(get_base_dir(), "output")


class RateAlertDashboardApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__(fg_color=COLOR_BG)
        self.title("為替レート監視 ＆ Webhook自動通知ダッシュボード")
        self.geometry("960x890")
        self.minsize(820, 770)

        # スレッド・状態管理フラグと排他ロック
        self.stop_event = threading.Event()
        self.check_lock = threading.Lock()
        self.monitor_thread: threading.Thread | None = None
        self.single_fetch_running = False
        self.is_monitoring = False
        self.has_log_entries = False
        self.sort_reverse_map: dict[str, bool] = {}
        self.help_dialog: ctk.CTkToplevel | None = None

        # 連続スパム通知防止用フラグ（一度通知したら条件が解除されるまで再通知しない）
        self.already_notified = False
        self.last_alert_key: tuple[str, float, str] | None = None

        # 設定変数の初期化（メインスレッド専用）
        self.currency_pair_var = ctk.StringVar(value="USD/JPY")
        self.target_rate_var = ctk.StringVar(value="150.00")
        self.condition_var = ctk.StringVar(value="以下になったら通知")
        self.interval_sec_var = ctk.StringVar(value=str(DEFAULT_INTERVAL_SEC))
        self.webhook_url_var = ctk.StringVar(value="")
        self.show_webhook_var = ctk.BooleanVar(value=False)
        self.output_dir_var = ctk.StringVar(value=DEFAULT_OUTPUT_DIR)

        # サブスレッド参照用の純粋Python設定スナップショット（Tkinter非依存）
        self.config_snapshot: dict[str, Any] = {
            "currency_pair": "USD/JPY",
            "target_rate": 150.0,
            "condition": "以下になったら通知",
            "interval_sec": DEFAULT_INTERVAL_SEC,
            "webhook_url": "",
            "output_dir": DEFAULT_OUTPUT_DIR,
        }

        # KPI / 進捗ステータス変数
        self.kpi_rate_var = ctk.StringVar(value="--- 円")
        self.kpi_diff_var = ctk.StringVar(value="--- 円")
        self.kpi_status_var = ctk.StringVar(value="待機中")
        self.progress_detail_var = ctk.StringVar(
            value="「今すぐ1回取得」または「自動監視を開始」ボタンを押してください"
        )
        self.dark_mode_var = ctk.BooleanVar(value=True)

        # config.json から前回設定を読み込み
        self.load_config()

        # ダッシュボードUI構築
        self._build_ui()

        # 既存の履歴CSVがあればプレビュー表に読み込み
        self._load_existing_history()

        # ウィンドウ終了時の安全停止ハンドラ
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def load_config(self) -> None:
        """config.json から前回の設定値を復元する。"""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    return
                if data.get("currency_pair") in CURRENCY_PAIRS:
                    self.currency_pair_var.set(str(data["currency_pair"]))
                if data.get("target_rate") is not None:
                    self.target_rate_var.set(str(data["target_rate"]))
                if data.get("condition") in CONDITION_OPTIONS:
                    self.condition_var.set(str(data["condition"]))
                if data.get("interval_sec") is not None:
                    try:
                        iv = int(str(data["interval_sec"]).strip())
                        if MIN_INTERVAL_SEC <= iv <= MAX_INTERVAL_SEC:
                            self.interval_sec_var.set(str(iv))
                        else:
                            self.interval_sec_var.set(str(DEFAULT_INTERVAL_SEC))
                    except ValueError:
                        self.interval_sec_var.set(str(DEFAULT_INTERVAL_SEC))
                if isinstance(data.get("webhook_url"), str):
                    self.webhook_url_var.set(data["webhook_url"])
                if isinstance(data.get("output_dir"), str) and data["output_dir"].strip():
                    self.output_dir_var.set(data["output_dir"].strip())
            except Exception:
                pass

    def save_config(self) -> None:
        """現在の設定値を config.json に保存する（メインスレッドから呼び出す）。"""
        data = {
            "currency_pair": self.currency_pair_var.get().strip() or "USD/JPY",
            "target_rate": self.target_rate_var.get().strip(),
            "condition": self.condition_var.get().strip() or "以下になったら通知",
            "interval_sec": self.interval_sec_var.get().strip(),
            "webhook_url": self.webhook_url_var.get().strip(),
            "output_dir": self.output_dir_var.get().strip() or DEFAULT_OUTPUT_DIR,
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.append_log(f"[警告] 設定ファイルの保存に失敗しました: {e}")

    def toggle_appearance_mode(self) -> None:
        """ダークモード／ライトモードを切り替え、Treeviewやログ色も連動させる。"""
        is_dark = self.dark_mode_var.get()
        if is_dark:
            ctk.set_appearance_mode("dark")
            self.theme_switch.configure(text="ダークモード")
        else:
            ctk.set_appearance_mode("light")
            self.theme_switch.configure(text="ライトモード")
        self._apply_treeview_theme(is_dark)
        self._configure_log_tags(is_dark)

    def _apply_treeview_theme(self, is_dark: bool) -> None:
        """ダーク／ライトテーマに合わせて ttk.Treeview の配色を調整する。"""
        style = ttk.Style(self)
        style.theme_use("clam")

        if is_dark:
            bg_color = "#141414"
            fg_color = "#F9FAFB"
            header_bg = "#262626"
            header_fg = "#F9FAFB"
            border_color = "#2E2E2E"
        else:
            bg_color = "#F9FAFB"
            fg_color = "#111827"
            header_bg = "#E5E7EB"
            header_fg = "#111827"
            border_color = "#D1D5DB"

        style.configure(
            "Preview.Treeview",
            background=bg_color,
            foreground=fg_color,
            fieldbackground=bg_color,
            bordercolor=border_color,
            lightcolor=border_color,
            darkcolor=border_color,
            rowheight=26,
            font=(FONT_FAMILY, 10),
        )
        style.map(
            "Preview.Treeview",
            background=[("selected", COLOR_ACCENT)],
            foreground=[("selected", "#FFFFFF")],
        )
        style.configure(
            "Preview.Treeview.Heading",
            background=header_bg,
            foreground=header_fg,
            bordercolor=border_color,
            relief="flat",
            font=(FONT_FAMILY, 10, "bold"),
            padding=(6, 5),
        )
        style.map(
            "Preview.Treeview.Heading",
            background=[("active", COLOR_ACCENT)],
            foreground=[("active", "#FFFFFF")],
        )

    def _configure_log_tags(self, is_dark: bool) -> None:
        """CTkTextbox 内部の tk.Text タグによるログ色分けを設定する。"""
        text_widget: tk.Text = self.log_text._textbox
        normal_fg = "#F9FAFB" if is_dark else "#111827"
        placeholder_fg = "#6B7280" if is_dark else "#9CA3AF"
        text_widget.tag_config("normal", foreground=normal_fg)
        text_widget.tag_config("success", foreground=COLOR_ACCENT)
        text_widget.tag_config("warning", foreground=COLOR_WARNING)
        text_widget.tag_config("error", foreground=COLOR_DANGER)
        text_widget.tag_config("placeholder", foreground=placeholder_fg)

    def _show_log_placeholder(self) -> None:
        """初期状態として実行ログにプレースホルダーを表示する。"""
        self.has_log_entries = False
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text._textbox.insert(tk.END, LOG_PLACEHOLDER_TEXT, "placeholder")
        self.log_text.configure(state="disabled")

    def _build_ui(self) -> None:
        """モダンSaaSダッシュボード風のUIレイアウトを構築する。"""
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)
        self.grid_rowconfigure(5, weight=1)

        # フォント定義（すべて Yu Gothic UI に統一）
        font_title = ctk.CTkFont(family=FONT_FAMILY, size=20, weight="bold")
        font_section = ctk.CTkFont(family=FONT_FAMILY, size=14, weight="bold")
        font_body = ctk.CTkFont(family=FONT_FAMILY, size=13)
        font_body_bold = ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold")
        font_sub = ctk.CTkFont(family=FONT_FAMILY, size=12)
        font_footer = ctk.CTkFont(family=FONT_FAMILY, size=11, underline=True)
        font_kpi_val = ctk.CTkFont(family=FONT_FAMILY, size=22, weight="bold")
        font_kpi_lbl = ctk.CTkFont(family=FONT_FAMILY, size=12)

        # --- 0. ヘッダーエリア（タイトル＋右上ライト／ダークモード切替スイッチ） ---
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.grid(row=0, column=0, sticky="ew", padx=24, pady=(14, 6))
        header_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header_frame,
            text="為替レート監視 ＆ Webhook自動通知ダッシュボード",
            font=font_title,
            text_color=COLOR_TEXT_MAIN,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        self.theme_switch = ctk.CTkSwitch(
            header_frame,
            text="ダークモード",
            variable=self.dark_mode_var,
            onvalue=True,
            offvalue=False,
            command=self.toggle_appearance_mode,
            font=font_sub,
            text_color=COLOR_TEXT_SUB,
            progress_color=COLOR_ACCENT,
        )
        self.theme_switch.grid(row=0, column=1, sticky="e")

        # --- 1. KPIダッシュボードカード（横並び3枚） ---
        kpi_container = ctk.CTkFrame(self, fg_color="transparent")
        kpi_container.grid(row=1, column=0, sticky="ew", padx=24, pady=5)
        kpi_container.grid_columnconfigure((0, 1, 2), weight=1, uniform="kpi")

        # KPI 1: 現在のレート
        kpi_card_rate = ctk.CTkFrame(
            kpi_container,
            fg_color=COLOR_CARD_BG,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            corner_radius=12,
        )
        kpi_card_rate.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkLabel(
            kpi_card_rate,
            text="現在のレート",
            font=font_kpi_lbl,
            text_color=COLOR_TEXT_SUB,
            anchor="w",
        ).pack(anchor="w", padx=18, pady=(10, 2))
        self.kpi_rate_label = ctk.CTkLabel(
            kpi_card_rate,
            textvariable=self.kpi_rate_var,
            font=font_kpi_val,
            text_color=COLOR_TEXT_MAIN,
            anchor="w",
        )
        self.kpi_rate_label.pack(anchor="w", padx=18, pady=(0, 10))

        # KPI 2: 目標レートとの差
        kpi_card_diff = ctk.CTkFrame(
            kpi_container,
            fg_color=COLOR_CARD_BG,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            corner_radius=12,
        )
        kpi_card_diff.grid(row=0, column=1, sticky="ew", padx=6)
        ctk.CTkLabel(
            kpi_card_diff,
            text="目標レートとの差",
            font=font_kpi_lbl,
            text_color=COLOR_TEXT_SUB,
            anchor="w",
        ).pack(anchor="w", padx=18, pady=(10, 2))
        self.kpi_diff_label = ctk.CTkLabel(
            kpi_card_diff,
            textvariable=self.kpi_diff_var,
            font=font_kpi_val,
            text_color=COLOR_ACCENT,
            anchor="w",
        )
        self.kpi_diff_label.pack(anchor="w", padx=18, pady=(0, 10))

        # KPI 3: 監視ステータス
        kpi_card_status = ctk.CTkFrame(
            kpi_container,
            fg_color=COLOR_CARD_BG,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            corner_radius=12,
        )
        kpi_card_status.grid(row=0, column=2, sticky="ew", padx=(6, 0))
        ctk.CTkLabel(
            kpi_card_status,
            text="監視ステータス",
            font=font_kpi_lbl,
            text_color=COLOR_TEXT_SUB,
            anchor="w",
        ).pack(anchor="w", padx=18, pady=(10, 2))
        self.kpi_status_label = ctk.CTkLabel(
            kpi_card_status,
            textvariable=self.kpi_status_var,
            font=font_kpi_val,
            text_color=COLOR_TEXT_MAIN,
            anchor="w",
        )
        self.kpi_status_label.pack(anchor="w", padx=18, pady=(0, 10))

        # --- 2. 監視条件 ＆ Webhook設定カード ---
        settings_card = ctk.CTkFrame(
            self,
            fg_color=COLOR_CARD_BG,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            corner_radius=12,
        )
        settings_card.grid(row=2, column=0, sticky="ew", padx=24, pady=5)
        settings_card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            settings_card,
            text="監視条件 ＆ Webhook通知設定",
            font=font_section,
            text_color=COLOR_TEXT_MAIN,
            anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=20, pady=(12, 6))

        # 行1: 監視通貨ペア ＆ チェック間隔
        ctk.CTkLabel(
            settings_card,
            text="監視通貨ペア:",
            font=font_body,
            text_color=COLOR_TEXT_MAIN,
        ).grid(row=1, column=0, sticky="w", padx=(20, 14), pady=4)

        pair_interval_frame = ctk.CTkFrame(settings_card, fg_color="transparent")
        pair_interval_frame.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 20), pady=4)

        self.pair_combo = ctk.CTkComboBox(
            pair_interval_frame,
            variable=self.currency_pair_var,
            values=CURRENCY_PAIRS,
            width=140,
            height=32,
            corner_radius=8,
            border_width=1,
            fg_color=COLOR_INPUT_BG,
            border_color=COLOR_CARD_BORDER,
            button_color=COLOR_SECONDARY_BG,
            button_hover_color=COLOR_SECONDARY_HOVER,
            dropdown_fg_color=COLOR_CARD_BG,
            dropdown_text_color=COLOR_TEXT_MAIN,
            text_color=COLOR_TEXT_MAIN,
            font=font_body,
            dropdown_font=font_body,
            state="readonly",
        )
        self.pair_combo.pack(side="left", padx=(0, 24))

        ctk.CTkLabel(
            pair_interval_frame,
            text="チェック間隔 (秒):",
            font=font_body,
            text_color=COLOR_TEXT_MAIN,
        ).pack(side="left", padx=(0, 10))

        self.interval_entry = ctk.CTkEntry(
            pair_interval_frame,
            textvariable=self.interval_sec_var,
            width=90,
            height=32,
            corner_radius=8,
            border_width=1,
            fg_color=COLOR_INPUT_BG,
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_TEXT_MAIN,
            font=font_body,
        )
        self.interval_entry.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(
            pair_interval_frame,
            text="※60〜86400秒（デフォルト: 3600秒）",
            font=font_sub,
            text_color=COLOR_TEXT_SUB,
        ).pack(side="left")

        # 行2: 目標レート ＆ 判定条件
        ctk.CTkLabel(
            settings_card,
            text="目標レート (しきい値):",
            font=font_body,
            text_color=COLOR_TEXT_MAIN,
        ).grid(row=2, column=0, sticky="w", padx=(20, 14), pady=4)

        target_cond_frame = ctk.CTkFrame(settings_card, fg_color="transparent")
        target_cond_frame.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(0, 20), pady=4)

        self.target_entry = ctk.CTkEntry(
            target_cond_frame,
            textvariable=self.target_rate_var,
            width=140,
            height=32,
            corner_radius=8,
            border_width=1,
            fg_color=COLOR_INPUT_BG,
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_TEXT_MAIN,
            font=font_body,
        )
        self.target_entry.pack(side="left", padx=(0, 14))

        self.condition_menu = ctk.CTkOptionMenu(
            target_cond_frame,
            variable=self.condition_var,
            values=CONDITION_OPTIONS,
            width=185,
            height=32,
            corner_radius=8,
            fg_color=COLOR_SECONDARY_BG,
            button_color=COLOR_SECONDARY_BG,
            button_hover_color=COLOR_SECONDARY_HOVER,
            text_color=COLOR_TEXT_MAIN,
            dropdown_fg_color=COLOR_CARD_BG,
            dropdown_text_color=COLOR_TEXT_MAIN,
            font=font_body,
            dropdown_font=font_body,
        )
        self.condition_menu.pack(side="left", padx=(0, 12))

        ctk.CTkLabel(
            target_cond_frame,
            text="※条件達成時は重複スパム防止のため1回のみ自動通知します",
            font=font_sub,
            text_color=COLOR_TEXT_SUB,
        ).pack(side="left")

        # 行3: Webhook URL ＆ 表示切替 ＆ 取得方法ヘルプ ＆ テスト送信ボタン
        ctk.CTkLabel(
            settings_card,
            text="Webhook URL:",
            font=font_body,
            text_color=COLOR_TEXT_MAIN,
        ).grid(row=3, column=0, sticky="w", padx=(20, 14), pady=4)

        webhook_subframe = ctk.CTkFrame(settings_card, fg_color="transparent")
        webhook_subframe.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(0, 20), pady=4)
        webhook_subframe.grid_columnconfigure(0, weight=1)

        self.webhook_entry = ctk.CTkEntry(
            webhook_subframe,
            textvariable=self.webhook_url_var,
            placeholder_text="https://discord.com/api/webhooks/... または https://hooks.slack.com/...",
            height=32,
            corner_radius=8,
            border_width=1,
            fg_color=COLOR_INPUT_BG,
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_TEXT_MAIN,
            font=font_body,
            show="•",
        )
        self.webhook_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self.show_webhook_check = ctk.CTkCheckBox(
            webhook_subframe,
            text="URL表示",
            variable=self.show_webhook_var,
            command=self._toggle_webhook_visibility,
            width=76,
            checkbox_width=18,
            checkbox_height=18,
            corner_radius=5,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color=COLOR_TEXT_SUB,
            font=font_sub,
        )
        self.show_webhook_check.grid(row=0, column=1, padx=(0, 8))

        self.webhook_help_btn = ctk.CTkButton(
            webhook_subframe,
            text="取得方法",
            width=80,
            height=32,
            corner_radius=8,
            font=font_sub,
            fg_color="transparent",
            hover_color=COLOR_GHOST_HOVER,
            border_width=1,
            border_color=COLOR_GHOST_BORDER,
            text_color=COLOR_TEXT_SUB,
            command=self.show_webhook_help_dialog,
        )
        self.webhook_help_btn.grid(row=0, column=2, padx=(0, 8))

        self.test_webhook_btn = ctk.CTkButton(
            webhook_subframe,
            text="テスト送信",
            width=100,
            height=32,
            corner_radius=8,
            font=font_body,
            fg_color=COLOR_SECONDARY_BG,
            hover_color=COLOR_SECONDARY_HOVER,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_TEXT_MAIN,
            command=self.send_test_webhook,
        )
        self.test_webhook_btn.grid(row=0, column=3)

        # 行4: CSV出力先フォルダ
        ctk.CTkLabel(
            settings_card,
            text="CSV出力先フォルダ:",
            font=font_body,
            text_color=COLOR_TEXT_MAIN,
        ).grid(row=4, column=0, sticky="w", padx=(20, 14), pady=(4, 14))

        dir_subframe = ctk.CTkFrame(settings_card, fg_color="transparent")
        dir_subframe.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(0, 20), pady=(4, 14))
        dir_subframe.grid_columnconfigure(0, weight=1)

        self.dir_entry = ctk.CTkEntry(
            dir_subframe,
            textvariable=self.output_dir_var,
            height=32,
            corner_radius=8,
            border_width=1,
            fg_color=COLOR_INPUT_BG,
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_TEXT_MAIN,
            font=font_body,
        )
        self.dir_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self.browse_btn = ctk.CTkButton(
            dir_subframe,
            text="参照...",
            width=100,
            height=32,
            corner_radius=8,
            font=font_body,
            fg_color=COLOR_SECONDARY_BG,
            hover_color=COLOR_SECONDARY_HOVER,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_TEXT_MAIN,
            command=self.browse_output_dir,
        )
        self.browse_btn.grid(row=0, column=1)

        # --- 3. 操作ボタン＆監視タイマー進行状況カード ---
        progress_card = ctk.CTkFrame(
            self,
            fg_color=COLOR_CARD_BG,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            corner_radius=12,
        )
        progress_card.grid(row=3, column=0, sticky="ew", padx=24, pady=5)
        progress_card.grid_columnconfigure(0, weight=1)

        action_frame = ctk.CTkFrame(progress_card, fg_color="transparent")
        action_frame.grid(row=0, column=0, sticky="ew", padx=20, pady=(12, 8))
        action_frame.grid_columnconfigure(3, weight=1)

        # 今すぐ1回取得ボタン
        self.fetch_once_btn = ctk.CTkButton(
            action_frame,
            text="今すぐ1回取得",
            width=140,
            height=36,
            corner_radius=8,
            font=font_body_bold,
            fg_color=COLOR_SECONDARY_BG,
            hover_color=COLOR_SECONDARY_HOVER,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_TEXT_MAIN,
            command=self.fetch_once,
        )
        self.fetch_once_btn.grid(row=0, column=0, padx=(0, 10))

        # 自動監視を開始ボタン（エメラルドグリーン）
        self.start_btn = ctk.CTkButton(
            action_frame,
            text="自動監視を開始",
            width=155,
            height=36,
            corner_radius=8,
            font=font_body_bold,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            command=self.start_monitoring,
        )
        self.start_btn.grid(row=0, column=1, padx=(0, 10))

        # 自動監視を停止ボタン（待機時ゴースト枠・監視中のみ赤色点灯 #EF4444）
        self.stop_btn = ctk.CTkButton(
            action_frame,
            text="自動監視を停止",
            width=145,
            height=36,
            corner_radius=8,
            font=font_body,
            fg_color="transparent",
            border_width=1,
            border_color=COLOR_GHOST_BORDER,
            hover_color=COLOR_GHOST_HOVER,
            text_color=COLOR_TEXT_MAIN,
            state="disabled",
            command=self.stop_monitoring,
        )
        self.stop_btn.grid(row=0, column=2, padx=(0, 10))

        # 出力先フォルダを開くボタン
        self.open_dir_btn = ctk.CTkButton(
            action_frame,
            text="出力先フォルダを開く",
            width=175,
            height=36,
            corner_radius=8,
            font=font_body,
            fg_color=COLOR_SECONDARY_BG,
            hover_color=COLOR_SECONDARY_HOVER,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_TEXT_MAIN,
            command=self.open_output_dir,
        )
        self.open_dir_btn.grid(row=0, column=4, sticky="e")

        # 進行状況ラベル＆カウントダウンプログレスバー
        status_header = ctk.CTkFrame(progress_card, fg_color="transparent")
        status_header.grid(row=1, column=0, sticky="ew", padx=20, pady=(2, 4))
        status_header.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            status_header,
            text="監視タイマー状況:",
            font=font_section,
            text_color=COLOR_TEXT_MAIN,
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))

        self.progress_detail_label = ctk.CTkLabel(
            status_header,
            textvariable=self.progress_detail_var,
            font=font_body,
            text_color=COLOR_TEXT_SUB,
            anchor="w",
        )
        self.progress_detail_label.grid(row=0, column=1, sticky="w")

        self.progress_bar = ctk.CTkProgressBar(
            progress_card,
            height=10,
            corner_radius=5,
            fg_color=COLOR_INPUT_BG,
            progress_color=COLOR_ACCENT,
        )
        self.progress_bar.grid(row=2, column=0, sticky="ew", padx=20, pady=(4, 14))
        self.progress_bar.set(0.0)

        # --- 4. 実行ログカード ---
        log_card = ctk.CTkFrame(
            self,
            fg_color=COLOR_CARD_BG,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            corner_radius=12,
        )
        log_card.grid(row=4, column=0, sticky="nsew", padx=24, pady=5)
        log_card.grid_columnconfigure(0, weight=1)
        log_card.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            log_card,
            text="実行ログ",
            font=font_section,
            text_color=COLOR_TEXT_MAIN,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(10, 4))

        self.log_text = ctk.CTkTextbox(
            log_card,
            height=110,
            font=font_body,
            corner_radius=8,
            fg_color=COLOR_INPUT_BG,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_TEXT_MAIN,
            wrap="word",
            state="disabled",
        )
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 12))
        self._configure_log_tags(is_dark=self.dark_mode_var.get())
        self._show_log_placeholder()

        # --- 5. 履歴プレビューカード（ttk.Treeview） ---
        preview_card = ctk.CTkFrame(
            self,
            fg_color=COLOR_CARD_BG,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            corner_radius=12,
        )
        preview_card.grid(row=5, column=0, sticky="nsew", padx=24, pady=(5, 4))
        preview_card.grid_columnconfigure(0, weight=1)
        preview_card.grid_rowconfigure(1, weight=1)

        preview_header = ctk.CTkFrame(preview_card, fg_color="transparent")
        preview_header.grid(row=0, column=0, sticky="ew", padx=20, pady=(10, 4))
        preview_header.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            preview_header,
            text="取得履歴プレビュー（rate_history.csv）",
            font=font_section,
            text_color=COLOR_TEXT_MAIN,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            preview_header,
            text="※列ヘッダーをクリックすると昇順・降順でソートできます",
            font=font_sub,
            text_color=COLOR_TEXT_SUB,
            anchor="e",
        ).grid(row=0, column=1, sticky="e")

        table_container = ctk.CTkFrame(
            preview_card,
            fg_color=COLOR_INPUT_BG,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            corner_radius=8,
        )
        table_container.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 12))
        table_container.grid_columnconfigure(0, weight=1)
        table_container.grid_rowconfigure(0, weight=1)

        self._apply_treeview_theme(is_dark=self.dark_mode_var.get())

        columns = (
            "fetched_at",
            "currency_pair",
            "current_rate",
            "target_rate",
            "judgment",
            "notification",
        )
        self.preview_tree = ttk.Treeview(
            table_container,
            columns=columns,
            show="headings",
            style="Preview.Treeview",
            height=6,
        )

        self.base_tree_titles = {
            "fetched_at": "取得日時",
            "currency_pair": "通貨ペア",
            "current_rate": "現在レート",
            "target_rate": "目標（条件）",
            "judgment": "判定結果",
            "notification": "通知状況",
        }
        col_configs = [
            ("fetched_at", "取得日時", 155, "center"),
            ("currency_pair", "通貨ペア", 95, "center"),
            ("current_rate", "現在レート", 115, "e"),
            ("target_rate", "目標（条件）", 170, "center"),
            ("judgment", "判定結果", 130, "center"),
            ("notification", "通知状況", 175, "center"),
        ]
        for col_id, heading_text, width, anchor in col_configs:
            self.preview_tree.heading(
                col_id,
                text=heading_text,
                command=lambda c=col_id: self.sort_preview_column(c),
            )
            self.preview_tree.column(
                col_id,
                width=width,
                minwidth=70,
                anchor=anchor,
                stretch=True,
            )

        tree_scroll = ttk.Scrollbar(
            table_container, orient=tk.VERTICAL, command=self.preview_tree.yview
        )
        self.preview_tree.configure(yscrollcommand=tree_scroll.set)

        self.preview_tree.grid(row=0, column=0, sticky="nsew", padx=(2, 0), pady=2)
        tree_scroll.grid(row=0, column=1, sticky="ns", padx=(0, 2), pady=2)

        # --- 6. 最下部：API利用規約に基づく出典表記リンク ---
        footer_frame = ctk.CTkFrame(self, fg_color="transparent")
        footer_frame.grid(row=6, column=0, sticky="ew", padx=24, pady=(0, 8))
        footer_frame.grid_columnconfigure(0, weight=1)

        self.attribution_label = ctk.CTkLabel(
            footer_frame,
            text="Rates By Exchange Rate API",
            font=font_footer,
            text_color=COLOR_TEXT_SUB,
            cursor="hand2",
        )
        self.attribution_label.grid(row=0, column=0)
        self.attribution_label.bind(
            "<Button-1>",
            lambda _event: webbrowser.open(EXCHANGE_RATE_API_URL),
        )

    def show_webhook_help_dialog(self) -> None:
        """Discord / Slack の Webhook URL 発行手順（3ステップ）をポップアップダイアログで表示する。"""
        if self.help_dialog is not None and self.help_dialog.winfo_exists():
            self.help_dialog.focus_force()
            self.help_dialog.lift()
            return

        dlg = ctk.CTkToplevel(self)
        dlg.title("Webhook URL の取得方法（Discord / Slack）")
        dlg.geometry("560x420")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.configure(fg_color=COLOR_BG)
        self.help_dialog = dlg

        font_h = ctk.CTkFont(family=FONT_FAMILY, size=15, weight="bold")
        font_sub_h = ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold")
        font_txt = ctk.CTkFont(family=FONT_FAMILY, size=12)

        container = ctk.CTkFrame(
            dlg,
            fg_color=COLOR_CARD_BG,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            corner_radius=12,
        )
        container.pack(fill="both", expand=True, padx=18, pady=18)

        ctk.CTkLabel(
            container,
            text="Webhook URL の発行手順（3ステップ）",
            font=font_h,
            text_color=COLOR_TEXT_MAIN,
            anchor="w",
        ).pack(fill="x", padx=20, pady=(16, 10))

        # Discord 手順
        discord_box = ctk.CTkFrame(
            container,
            fg_color=COLOR_INPUT_BG,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            corner_radius=8,
        )
        discord_box.pack(fill="x", padx=20, pady=(0, 10))
        ctk.CTkLabel(
            discord_box,
            text="■ Discord の場合",
            font=font_sub_h,
            text_color=COLOR_ACCENT,
            anchor="w",
        ).pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(
            discord_box,
            text=(
                "1. 通知したいチャンネルの「チャンネルの編集（歯車アイコン）」を開きます。\n"
                "2. 左メニューの「連携サービス」→「ウェブフックを作成」をクリックします。\n"
                "3.「ウェブフックURLをコピー」を押し、本ツールの入力欄に貼り付けます。"
            ),
            font=font_txt,
            text_color=COLOR_TEXT_MAIN,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 10))

        # Slack 手順
        slack_box = ctk.CTkFrame(
            container,
            fg_color=COLOR_INPUT_BG,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            corner_radius=8,
        )
        slack_box.pack(fill="x", padx=20, pady=(0, 14))
        ctk.CTkLabel(
            slack_box,
            text="■ Slack の場合",
            font=font_sub_h,
            text_color=COLOR_ACCENT,
            anchor="w",
        ).pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(
            slack_box,
            text=(
                "1. Slack App Directory で「Incoming WebHooks」を検索して追加します。\n"
                "2. 通知を投稿したいチャンネルを選択して「インテグレーションの追加」を押します。\n"
                "3. 発行された「Webhook URL (https://hooks.slack.com/...)」をコピーして貼り付けます。"
            ),
            font=font_txt,
            text_color=COLOR_TEXT_MAIN,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 10))

        ctk.CTkButton(
            container,
            text="閉じる",
            width=120,
            height=34,
            corner_radius=8,
            font=font_sub_h,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            command=dlg.destroy,
        ).pack(pady=(2, 14))

    def _toggle_webhook_visibility(self) -> None:
        """Webhook URL入力欄の伏字（•）と平文表示を切り替える。"""
        if self.show_webhook_var.get():
            self.webhook_entry.configure(show="")
        else:
            self.webhook_entry.configure(show="•")

    def _load_existing_history(self) -> None:
        """起動時に既存の rate_history.csv があればプレビュー表に反映する。"""
        out_dir = self.output_dir_var.get().strip() or DEFAULT_OUTPUT_DIR
        df = load_history_csv(out_dir, limit=100)
        if df.empty:
            return
        # 新しい履歴が上に来るよう逆順で挿入
        for _, row in df.iloc[::-1].iterrows():
            target_val = str(row.get("目標レート", ""))
            cond_val = str(row.get("判定条件", ""))
            target_disp = f"{target_val} ({cond_val})" if cond_val else target_val
            self.preview_tree.insert(
                "",
                tk.END,
                values=(
                    str(row.get("取得日時", "")),
                    str(row.get("通貨ペア", "")),
                    str(row.get("現在レート", "")),
                    target_disp,
                    str(row.get("判定結果", "")),
                    str(row.get("通知状況", "")),
                ),
            )

    def insert_history_row(self, record: dict) -> None:
        """新しく取得した1件分の履歴を Treeview の先頭に追加する。"""
        target_disp = f"{record['target_rate_str']} ({record['condition']})"
        values = (
            record["fetched_at"],
            record["currency_pair"],
            record["current_rate_str"],
            target_disp,
            record["judgment"],
            record["notification"],
        )

        def _insert() -> None:
            self.preview_tree.insert("", 0, values=values)

        self.after(0, _insert)

    def sort_preview_column(self, col: str) -> None:
        """プレビュー表の指定列をクリックした際に昇順・降順でソートする。"""
        items = list(self.preview_tree.get_children(""))
        if not items:
            return

        reverse = self.sort_reverse_map.get(col, False)

        def _sort_key(item_id: str):
            val = self.preview_tree.set(item_id, col)
            if col in ("current_rate", "target_rate"):
                m = re.search(r"[-+]?\d+(?:\.\d+)?", str(val))
                return float(m.group(0)) if m else 0.0
            return str(val).lower()

        items.sort(key=_sort_key, reverse=reverse)
        for index, item_id in enumerate(items):
            self.preview_tree.move(item_id, "", index)

        self.sort_reverse_map[col] = not reverse

        for c_id, title in self.base_tree_titles.items():
            if c_id == col:
                arrow = " ▼" if reverse else " ▲"
                self.preview_tree.heading(c_id, text=f"{title}{arrow}")
            else:
                self.preview_tree.heading(c_id, text=title)

    def browse_output_dir(self) -> None:
        """出力先フォルダ選択ダイアログを開き、選択されたパスを保存する。"""
        if self.is_monitoring:
            return
        current_dir = self.output_dir_var.get().strip() or DEFAULT_OUTPUT_DIR
        initial_dir = current_dir if os.path.exists(current_dir) else get_base_dir()
        selected = filedialog.askdirectory(initialdir=initial_dir, title="CSV出力先フォルダを選択")
        if selected:
            self.output_dir_var.set(selected)
            self.dir_entry.configure(border_color=COLOR_CARD_BORDER)
            self.save_config()
            self.append_log(f"CSV出力先フォルダを変更しました: {selected}")

    def open_output_dir(self) -> None:
        """出力先フォルダをエクスプローラーで開く。存在しない場合は作成して開く。"""
        out_dir = self.output_dir_var.get().strip() or DEFAULT_OUTPUT_DIR
        try:
            os.makedirs(out_dir, exist_ok=True)
            os.startfile(out_dir)
        except Exception as e:
            messagebox.showerror("エラー", f"フォルダを開けませんでした:\n{e}")

    def _classify_log_tag(self, message: str) -> str:
        """ログメッセージの内容に応じて色分けタグ（error / warning / success / normal）を判定する。"""
        if any(k in message for k in ("[エラー]", "失敗", "通信エラー", "入力エラー", "HTTP 429", "レート制限")):
            return "error"
        if any(k in message for k in ("[警告]", "[停止]", "スキップ", "未設定", "停止しました", "退避保存")):
            return "warning"
        if any(
            k in message
            for k in ("[条件達成]", "[通知成功]", "[テスト成功]", "送信しました", "=== 自動監視開始")
        ):
            return "success"
        return "normal"

    def append_log(self, message: str) -> None:
        """メインスレッド上で安全に実行ログエリアへ色分け付きメッセージを追記する（URLマスク処理付き）。"""
        safe_message = mask_urls(message)
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {safe_message}\n"
        tag = self._classify_log_tag(safe_message)

        def _append() -> None:
            self.log_text.configure(state="normal")
            if not self.has_log_entries:
                self.log_text.delete("1.0", tk.END)
                self.has_log_entries = True
            self.log_text._textbox.insert(tk.END, line, tag)
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")

        self.after(0, _append)

    def update_kpi(
        self,
        current_rate: float | None = None,
        target_rate: float | None = None,
        status_text: str | None = None,
        is_alert: bool | None = None,
    ) -> None:
        """メインスレッド上で3つのKPIカード（現在のレート・目標レートとの差・監視ステータス）を更新する。"""

        def _update() -> None:
            if current_rate is not None:
                self.kpi_rate_var.set(f"{current_rate:.2f} 円")
            if current_rate is not None and target_rate is not None:
                diff = current_rate - target_rate
                self.kpi_diff_var.set(f"{diff:+.2f} 円")
            if status_text is not None:
                self.kpi_status_var.set(status_text)
                if status_text == "条件達成！" or is_alert:
                    self.kpi_status_label.configure(text_color=COLOR_ACCENT)
                elif "エラー" in status_text:
                    self.kpi_status_label.configure(text_color=COLOR_DANGER)
                elif status_text == "自動監視中":
                    self.kpi_status_label.configure(text_color=COLOR_ACCENT)
                else:
                    self.kpi_status_label.configure(text_color=COLOR_TEXT_MAIN)

        self.after(0, _update)

    def _reset_input_borders(self) -> None:
        """入力欄の枠線色を通常のボーダー色に戻す。"""
        self.target_entry.configure(border_color=COLOR_CARD_BORDER)
        self.interval_entry.configure(border_color=COLOR_CARD_BORDER)
        self.webhook_entry.configure(border_color=COLOR_CARD_BORDER)
        self.dir_entry.configure(border_color=COLOR_CARD_BORDER)

    def validate_inputs(
        self, require_webhook: bool = False
    ) -> dict[str, Any] | None:
        """
        メインスレッド上で画面入力値を検証し、純粋Python辞書（スナップショット）を返す。
        目標レートやチェック間隔が空欄・不正値の場合は勝手にフォールバックせずエラーとして扱う。
        """
        self._reset_input_borders()
        errors: list[str] = []

        currency_pair = self.currency_pair_var.get().strip()
        if currency_pair not in CURRENCY_PAIRS:
            errors.append("監視通貨ペアを正しく選択してください")

        condition = self.condition_var.get().strip()
        if condition not in CONDITION_OPTIONS:
            errors.append("判定条件を正しく選択してください")

        target_rate: float | None = None
        raw_target = self.target_rate_var.get().strip()
        if not raw_target:
            self.target_entry.configure(border_color=COLOR_DANGER)
            errors.append("目標レートが空欄です（0より大きい数値を入力してください）")
        else:
            try:
                parsed_target = float(raw_target)
                if parsed_target <= 0:
                    raise ValueError
                target_rate = parsed_target
            except ValueError:
                self.target_entry.configure(border_color=COLOR_DANGER)
                errors.append("目標レートは 0 より大きい数値（例: 150.00）で入力してください")

        interval_sec: int | None = None
        raw_interval = self.interval_sec_var.get().strip()
        if not raw_interval:
            self.interval_entry.configure(border_color=COLOR_DANGER)
            errors.append(
                f"チェック間隔が空欄です（{MIN_INTERVAL_SEC}〜{MAX_INTERVAL_SEC}秒で入力してください）"
            )
        else:
            try:
                parsed_interval = int(raw_interval)
                if not (MIN_INTERVAL_SEC <= parsed_interval <= MAX_INTERVAL_SEC):
                    raise ValueError
                interval_sec = parsed_interval
            except ValueError:
                self.interval_entry.configure(border_color=COLOR_DANGER)
                errors.append(
                    f"チェック間隔は {MIN_INTERVAL_SEC}〜{MAX_INTERVAL_SEC} の整数（秒）で入力してください"
                )

        webhook_url = self.webhook_url_var.get().strip()
        if require_webhook:
            if not webhook_url or not (
                webhook_url.startswith("https://") or webhook_url.startswith("http://")
            ):
                self.webhook_entry.configure(border_color=COLOR_DANGER)
                errors.append(
                    "Webhook URL を https://... 形式で入力してください（Discord / Slack 対応）"
                )
        elif webhook_url and not (
            webhook_url.startswith("https://") or webhook_url.startswith("http://")
        ):
            self.webhook_entry.configure(border_color=COLOR_DANGER)
            errors.append("Webhook URL は http:// または https:// から始まる形式で入力してください")

        output_dir = self.output_dir_var.get().strip()
        if not output_dir:
            self.dir_entry.configure(border_color=COLOR_DANGER)
            errors.append("CSV出力先フォルダパスを入力してください")

        if errors or target_rate is None or interval_sec is None:
            error_msg = "入力エラー: " + " / ".join(errors)
            self.update_kpi(status_text="入力エラー")
            self.progress_detail_var.set(error_msg)
            self.progress_detail_label.configure(text_color=COLOR_DANGER)
            self.append_log(f"[警告] {error_msg}（処理をスキップしました）")
            return None

        self.progress_detail_label.configure(text_color=COLOR_TEXT_SUB)
        snapshot = {
            "currency_pair": currency_pair,
            "target_rate": target_rate,
            "condition": condition,
            "interval_sec": interval_sec,
            "webhook_url": webhook_url,
            "output_dir": output_dir,
        }
        self.config_snapshot = dict(snapshot)
        return snapshot

    def _set_settings_inputs_state(self, locked: bool) -> None:
        """自動監視中は設定入力欄をロック（disabled）し、停止時に解除する。"""
        combo_state = "disabled" if locked else "readonly"
        entry_state = "disabled" if locked else "normal"

        self.pair_combo.configure(state=combo_state)
        self.interval_entry.configure(state=entry_state)
        self.target_entry.configure(state=entry_state)
        self.condition_menu.configure(state=entry_state)
        self.webhook_entry.configure(state=entry_state)
        self.dir_entry.configure(state=entry_state)
        self.browse_btn.configure(state=entry_state)

    def _set_monitoring_ui(self, monitoring: bool) -> None:
        """自動監視中かどうかに応じて設定入力欄のロックと開始・停止ボタンの見た目を切り替える。"""
        self.is_monitoring = monitoring
        self._set_settings_inputs_state(locked=monitoring)
        if monitoring:
            self.start_btn.configure(state="disabled", fg_color=COLOR_START_DISABLED)
            self.stop_btn.configure(
                state="normal",
                text="自動監視を停止",
                fg_color=COLOR_DANGER,
                hover_color=COLOR_DANGER_HOVER,
                border_color=COLOR_DANGER,
                text_color="#FFFFFF",
            )
        else:
            self.start_btn.configure(state="normal", fg_color=COLOR_ACCENT)
            self.stop_btn.configure(
                state="disabled",
                text="自動監視を停止",
                fg_color="transparent",
                hover_color=COLOR_GHOST_HOVER,
                border_color=COLOR_GHOST_BORDER,
                text_color=COLOR_TEXT_MAIN,
            )

    def send_test_webhook(self) -> None:
        """「テスト送信」ボタン押下時にバックグラウンドでWebhookテスト通知を送信する。"""
        snapshot = self.validate_inputs(require_webhook=True)
        if snapshot is None:
            return

        self.save_config()
        currency_pair = str(snapshot["currency_pair"])
        target_rate = float(snapshot["target_rate"])
        condition = str(snapshot["condition"])
        webhook_url = str(snapshot["webhook_url"])

        self.test_webhook_btn.configure(state="disabled", text="送信中...")
        self.append_log(f"Webhookテスト通知を送信しています... ({currency_pair})")

        def _worker() -> None:
            res = send_webhook_notification(
                webhook_url=webhook_url,
                currency_pair=currency_pair,
                current_rate=target_rate,
                target_rate=target_rate,
                condition=condition,
                is_test=True,
            )
            if res["success"]:
                self.append_log(f"[テスト成功] {res['message']}")
            else:
                self.append_log(f"[エラー] テスト送信失敗: {res['message']}")

            def _restore() -> None:
                self.test_webhook_btn.configure(state="normal", text="テスト送信")

            self.after(0, _restore)

        threading.Thread(target=_worker, daemon=True).start()

    def fetch_once(self) -> None:
        """「今すぐ1回取得」ボタン押下時の単発レート取得＆判定処理。"""
        if self.single_fetch_running:
            return
        snapshot = self.validate_inputs(require_webhook=False)
        if snapshot is None:
            return

        self.save_config()
        currency_pair = str(snapshot["currency_pair"])
        target_rate = float(snapshot["target_rate"])
        condition = str(snapshot["condition"])
        webhook_url = str(snapshot["webhook_url"])
        output_dir = str(snapshot["output_dir"])
        is_periodic_now = bool(self.is_monitoring)

        self.single_fetch_running = True
        self.fetch_once_btn.configure(state="disabled", text="取得中...")
        if not is_periodic_now:
            self.progress_detail_var.set(f"{currency_pair} の最新為替レートを取得中...")

        def _worker() -> None:
            try:
                self._execute_check_cycle(
                    currency_pair=currency_pair,
                    target_rate=target_rate,
                    condition=condition,
                    webhook_url=webhook_url,
                    output_dir=output_dir,
                    is_periodic=is_periodic_now,
                )
            finally:
                def _restore() -> None:
                    self.single_fetch_running = False
                    self.fetch_once_btn.configure(state="normal", text="今すぐ1回取得")

                self.after(0, _restore)

        threading.Thread(target=_worker, daemon=True).start()

    def start_monitoring(self) -> None:
        """「自動監視を開始」ボタン押下時の処理。設定スナップショットを渡してバックグラウンド監視を開始する。"""
        if self.is_monitoring:
            return
        snapshot = self.validate_inputs(require_webhook=False)
        if snapshot is None:
            return

        self.save_config()
        currency_pair = str(snapshot["currency_pair"])
        target_rate = float(snapshot["target_rate"])
        condition = str(snapshot["condition"])
        interval_sec = int(snapshot["interval_sec"])
        webhook_url = str(snapshot["webhook_url"])

        # 監視開始時にスパム防止フラグを初期化
        self.already_notified = False
        self.last_alert_key = (currency_pair, target_rate, condition)

        self.stop_event.clear()
        self._set_monitoring_ui(True)
        self.progress_bar.set(0.0)
        self.update_kpi(status_text="自動監視中")
        self.append_log(
            f"=== 自動監視開始: {currency_pair} / 目標 {target_rate:.2f} 円 ({condition}) / 間隔 {interval_sec} 秒 ==="
        )
        if not webhook_url:
            self.append_log(
                "[警告] Webhook URLが未設定のため、条件達成時は画面表示とCSV記録のみ行います。"
            )

        self.monitor_thread = threading.Thread(
            target=self._run_monitor_loop,
            args=(dict(snapshot),),
            daemon=True,
        )
        self.monitor_thread.start()

    def stop_monitoring(self) -> None:
        """「自動監視を停止」ボタン押下時の処理。安全に監視スレッドを停止する。"""
        if self.is_monitoring:
            self.stop_event.set()
            self.stop_btn.configure(
                state="disabled",
                text="停止中...",
                fg_color="transparent",
                border_color=COLOR_GHOST_BORDER,
            )
            self.progress_detail_var.set("自動監視を停止しています...")
            self.append_log("[停止] 自動監視の停止リクエストを受け付けました。")

    def _run_monitor_loop(self, snapshot: dict[str, Any]) -> None:
        """
        バックグラウンドで指定間隔ごとに為替レートを取得し、カウントダウンを更新する。
        Tkinter の StringVar には一切直接触れず、メインスレッドから渡された設定スナップショットを使用する。
        """
        try:
            while not self.stop_event.is_set():
                interval_sec = snapshot.get("interval_sec")
                target_rate = snapshot.get("target_rate")
                currency_pair = str(snapshot.get("currency_pair", "")).strip()
                condition = str(snapshot.get("condition", "")).strip()
                webhook_url = str(snapshot.get("webhook_url", "")).strip()
                output_dir = str(snapshot.get("output_dir", "")).strip()

                # 目標レートや監視間隔が不正な場合は勝手にフォールバックせず警告を出してスキップ
                if (
                    not isinstance(target_rate, (int, float))
                    or target_rate <= 0
                    or not isinstance(interval_sec, int)
                    or not (MIN_INTERVAL_SEC <= interval_sec <= MAX_INTERVAL_SEC)
                    or not currency_pair
                    or not condition
                    or not output_dir
                ):
                    self.append_log(
                        "[警告] 監視設定（目標レートまたは監視間隔）が不正なため、このサイクルをスキップしました。"
                    )
                    wait_sec = (
                        interval_sec
                        if isinstance(interval_sec, int) and interval_sec >= MIN_INTERVAL_SEC
                        else DEFAULT_INTERVAL_SEC
                    )
                else:
                    wait_sec = interval_sec
                    try:
                        current_key = (currency_pair, float(target_rate), condition)
                        if self.last_alert_key != current_key:
                            self.already_notified = False
                            self.last_alert_key = current_key

                        # 1回分のチェックを実行（1回の失敗で監視全体が落ちないよう保護）
                        self._execute_check_cycle(
                            currency_pair=currency_pair,
                            target_rate=float(target_rate),
                            condition=condition,
                            webhook_url=webhook_url,
                            output_dir=output_dir,
                            is_periodic=True,
                        )
                    except Exception as cycle_exc:
                        self.append_log(
                            f"[警告] 監視サイクル実行中に予期しないエラーが発生しましたが監視を継続します: {cycle_exc}"
                        )

                # 次回チェックまでのカウントダウン（0.25秒刻みで停止イベントを監視）
                start_wait = time.perf_counter()
                while not self.stop_event.is_set():
                    elapsed = time.perf_counter() - start_wait
                    remaining = max(0, int(round(wait_sec - elapsed)))
                    ratio = min(1.0, elapsed / wait_sec) if wait_sec > 0 else 1.0

                    def _update_timer(r: float = ratio, rem: int = remaining) -> None:
                        if not self.stop_event.is_set():
                            self.progress_bar.set(r)
                            self.progress_detail_var.set(
                                f"自動監視稼働中 — 次回チェックまで残り {rem} 秒"
                            )

                    self.after(0, _update_timer)

                    if elapsed >= wait_sec:
                        break
                    self.stop_event.wait(0.25)

        except Exception as exc:
            self.append_log(f"[エラー] 自動監視ループで予期しない例外が発生しました: {exc}")
        finally:
            def _on_stopped() -> None:
                self._set_monitoring_ui(False)
                self.progress_bar.set(0.0)
                self.update_kpi(status_text="待機中")
                self.progress_detail_var.set("自動監視を停止しました（待機中）")
                self.append_log("[停止] 自動監視を停止しました。")

            self.after(0, _on_stopped)

    def _execute_check_cycle(
        self,
        currency_pair: str,
        target_rate: float,
        condition: str,
        webhook_url: str,
        output_dir: str,
        is_periodic: bool,
    ) -> None:
        """
        為替レートを1回取得し、条件判定・Webhook通知・CSV追記・UI更新を行う。
        単発取得と定期監視サイクルが同時実行されて重複通知しないよう check_lock で排他制御する。
        """
        with self.check_lock:
            try:
                rate_info = fetch_exchange_rate(
                    currency_pair=currency_pair,
                    timeout=10,
                    max_retries=2,
                    log_callback=self.append_log,
                )
            except Exception as exc:
                err_text = str(exc)
                self.append_log(f"[エラー] {err_text}")
                self.update_kpi(status_text="通信エラー")
                if not is_periodic:
                    self.after(
                        0,
                        lambda msg=err_text: self.progress_detail_var.set(msg),
                    )
                return

            current_rate = float(rate_info["rate"])
            fetched_at = str(rate_info["fetched_at"])
            is_matched = evaluate_condition(current_rate, target_rate, condition)
            diff = current_rate - target_rate

            if is_matched:
                judgment_str = "条件達成！"
                status_disp = "条件達成！"
                if self.already_notified:
                    notification_str = "スキップ（通知済）"
                    self.append_log(
                        f"[警告] {currency_pair} = {current_rate:.2f} 円 （目標 {target_rate:.2f} 円: 条件達成継続中のためWebhook通知をスキップ）"
                    )
                else:
                    if webhook_url:
                        res = send_webhook_notification(
                            webhook_url=webhook_url,
                            currency_pair=currency_pair,
                            current_rate=current_rate,
                            target_rate=target_rate,
                            condition=condition,
                            is_test=False,
                        )
                        if res["success"]:
                            notification_str = f"通知済 ({res['platform']})"
                            self.already_notified = True
                            self.append_log(
                                f"[条件達成] {currency_pair} = {current_rate:.2f} 円 （目標比 {diff:+.2f} 円） -> [通知成功] {res['message']}"
                            )
                        else:
                            notification_str = "送信失敗"
                            self.append_log(
                                f"[エラー] 条件達成（{current_rate:.2f} 円）ですがWebhook送信に失敗しました: {res['message']}"
                            )
                    else:
                        notification_str = "未送信（URL未設定）"
                        self.already_notified = True
                        self.append_log(
                            f"[条件達成] {currency_pair} = {current_rate:.2f} 円 （目標 {target_rate:.2f} 円 / Webhook URL未設定）"
                        )
            else:
                # 条件未達に戻ったら通知済みフラグをリセット
                if self.already_notified:
                    self.already_notified = False
                    self.append_log(
                        f"レートが目標範囲外（{current_rate:.2f} 円）に戻ったため、通知済みフラグをリセットしました。"
                    )
                judgment_str = "監視中（未達）"
                notification_str = "通知なし"
                status_disp = "自動監視中" if is_periodic else "待機中"
                self.append_log(
                    f"取得完了: {currency_pair} = {current_rate:.2f} 円 （目標 {target_rate:.2f} 円 / 差 {diff:+.2f} 円 -> 未達）"
                )

            # KPIカード更新
            self.update_kpi(
                current_rate=current_rate,
                target_rate=target_rate,
                status_text=status_disp,
                is_alert=is_matched,
            )

            if not is_periodic:
                api_update_str = str(rate_info.get("api_last_update", ""))
                self.after(
                    0,
                    lambda f=fetched_at, u=api_update_str: self.progress_detail_var.set(
                        f"単発取得完了（{f}）— API更新時刻: {u}"
                    ),
                )

            # CSV追記（ロック時の自動退避保存対応）＆ Treeviewプレビュー更新
            record = {
                "fetched_at": fetched_at,
                "currency_pair": currency_pair,
                "current_rate": f"{current_rate:.2f}",
                "current_rate_str": f"{current_rate:.2f} 円",
                "target_rate": f"{target_rate:.2f}",
                "target_rate_str": f"{target_rate:.2f} 円",
                "condition": condition,
                "judgment": judgment_str,
                "notification": notification_str,
            }
            try:
                append_history_csv(
                    record,
                    output_dir=output_dir,
                    log_callback=self.append_log,
                )
            except Exception as exc:
                self.append_log(f"[エラー] CSV履歴の保存に失敗しました: {exc}")

            self.insert_history_row(record)

    def _on_close(self) -> None:
        """ウィンドウ終了時に設定を保存し、監視スレッドを停止して閉じる。"""
        self.stop_event.set()
        self.save_config()
        self.destroy()


def main() -> None:
    app = RateAlertDashboardApp()
    app.mainloop()


if __name__ == "__main__":
    main()
