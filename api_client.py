from __future__ import annotations

from datetime import datetime, timezone
import os
import re
import time
from typing import Any, Callable
import pandas as pd
import requests

API_BASE_URL = "https://open.er-api.com/v6/latest/{base}"
DEFAULT_HEADERS = {
    "User-Agent": "RateAlertDashboard/1.0 (+https://open.er-api.com)"
}

HISTORY_COLUMNS = [
    "取得日時",
    "通貨ペア",
    "現在レート",
    "目標レート",
    "判定条件",
    "判定結果",
    "通知状況",
]

_URL_PATTERN = re.compile(r"https?://\S+")


class RateLimitError(RuntimeError):
    """HTTP 429 レート制限検知時にリトライせず即時中断するための例外。"""


def mask_urls(text: str) -> str:
    """エラーメッセージ等に含まれる http:// または https:// URL を [MASKED_URL] に置換する。"""
    if not text:
        return ""
    return _URL_PATTERN.sub("[MASKED_URL]", str(text))


def parse_currency_pair(currency_pair: str) -> tuple[str, str]:
    """'USD/JPY' 形式の通貨ペア文字列を (base, target) に分割する。"""
    cleaned = currency_pair.strip().upper().replace(" ", "")
    if "/" not in cleaned:
        raise ValueError(f"通貨ペアの形式が不正です（例: USD/JPY）: {currency_pair}")
    parts = cleaned.split("/", 1)
    base, target = parts[0].strip(), parts[1].strip()
    if len(base) != 3 or len(target) != 3:
        raise ValueError(f"通貨コードは3文字で指定してください（例: USD/JPY）: {currency_pair}")
    return base, target


def fetch_exchange_rate(
    currency_pair: str = "USD/JPY",
    timeout: int = 10,
    max_retries: int = 2,
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    ExchangeRate-API (https://open.er-api.com/v6/latest/{base_currency}) から
    指定した通貨ペアの為替レートを取得する。

    - タイムアウト: デフォルト10秒
    - HTTP 429 (レート制限) の場合はリトライを行わず即座にエラーを返す。
    - その他の通信エラー時は最大 max_retries 回（デフォルト2回）リトライする。
    - 戻り値:
        {
            "currency_pair": "USD/JPY",
            "base_currency": "USD",
            "target_currency": "JPY",
            "rate": 150.25,
            "api_last_update": "2026-11-16 00:00:01 UTC",
            "fetched_at": "2026-11-16 03:15:00"
        }
    """
    base_currency, target_currency = parse_currency_pair(currency_pair)
    url = API_BASE_URL.format(base=base_currency)

    last_error: Exception | None = None
    total_attempts = max_retries + 1

    for attempt in range(1, total_attempts + 1):
        try:
            response = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
            if response.status_code == 429:
                raise RateLimitError(
                    "レート制限（HTTP 429）を検知しました。約20分後に再試行してください。"
                )
            response.raise_for_status()
            data = response.json()

            if data.get("result") != "success":
                error_type = data.get("error-type", "unknown_error")
                raise RuntimeError(f"API側エラー応答: {error_type}")

            rates = data.get("rates", {})
            if target_currency not in rates:
                raise KeyError(f"対象通貨 '{target_currency}' のレートがAPI応答に含まれていません。")

            rate_val = float(rates[target_currency])

            # API最終更新時刻の整形
            unix_ts = data.get("time_last_update_unix")
            if isinstance(unix_ts, (int, float)):
                dt_utc = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
                api_last_update = dt_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
            else:
                api_last_update = str(data.get("time_last_update_utc", "不明"))

            fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            return {
                "currency_pair": f"{base_currency}/{target_currency}",
                "base_currency": base_currency,
                "target_currency": target_currency,
                "rate": rate_val,
                "api_last_update": api_last_update,
                "fetched_at": fetched_at,
            }

        except RateLimitError:
            # HTTP 429 の場合はリトライせず即時送出
            raise
        except Exception as exc:
            last_error = exc
            masked_err = mask_urls(str(exc))
            if attempt <= max_retries:
                if log_callback:
                    log_callback(
                        f"[警告] API通信失敗（試行 {attempt}/{total_attempts}）: {masked_err} -> リトライします..."
                    )
                time.sleep(1.5 * attempt)
            else:
                break

    raise RuntimeError(
        f"為替レートの取得に失敗しました（全{total_attempts}回試行）: {mask_urls(str(last_error))}"
    ) from last_error


def evaluate_condition(current_rate: float, target_rate: float, condition: str) -> bool:
    """
    現在レートが目標レートの条件を満たしているか判定する。
    condition: '以下になったら通知' または '以上になったら通知'（'<=', '>=' も許容）
    """
    cond = condition.strip()
    if "以下" in cond or "<=" in cond:
        return current_rate <= target_rate
    if "以上" in cond or ">=" in cond:
        return current_rate >= target_rate
    raise ValueError(f"未対応の判定条件です: {condition}")


def detect_webhook_platform(webhook_url: str) -> str:
    """Webhook URLから送信先プラットフォーム（'discord' / 'slack' / 'generic'）を判別する。"""
    lowered = webhook_url.strip().lower()
    if "discord.com/api/webhooks" in lowered or "discordapp.com/api/webhooks" in lowered:
        return "discord"
    if "hooks.slack.com" in lowered:
        return "slack"
    return "generic"


def send_webhook_notification(
    webhook_url: str,
    currency_pair: str = "USD/JPY",
    current_rate: float = 0.0,
    target_rate: float = 0.0,
    condition: str = "以下になったら通知",
    is_test: bool = False,
    custom_message: str | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    """
    Discord または Slack の Incoming Webhook にリッチカード形式（Embeds / Attachments）で通知をPOST送信する。
    通信例外やエラー応答に含まれる URL は [MASKED_URL] にマスクして秘密トークン漏洩を防止する。

    - Discord URL ('discord.com/api/webhooks/...' または 'discordapp.com'): {"embeds": [...]}
    - Slack URL ('hooks.slack.com/...'): {"attachments": [...]}
    - 戻り値:
        {
            "success": bool,
            "platform": "Discord" | "Slack" | "Webhook",
            "status_code": int | None,
            "message": str
        }
    """
    cleaned_url = webhook_url.strip()
    if not cleaned_url:
        return {
            "success": False,
            "platform": "未設定",
            "status_code": None,
            "message": "Webhook URLが入力されていません。",
        }

    if not (cleaned_url.startswith("https://") or cleaned_url.startswith("http://")):
        return {
            "success": False,
            "platform": "不正なURL",
            "status_code": None,
            "message": "Webhook URLは http:// または https:// から始まる必要があります。",
        }

    platform_key = detect_webhook_platform(cleaned_url)
    platform_label = (
        "Discord"
        if platform_key == "discord"
        else ("Slack" if platform_key == "slack" else "Webhook")
    )

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    unit = "円" if currency_pair.upper().endswith("/JPY") else ""
    diff = current_rate - target_rate
    diff_str = f"{diff:+.4f}" if not unit else f"{diff:+.2f} {unit}"
    rate_str = f"{current_rate:.4f}" if not unit else f"{current_rate:.2f} {unit}"
    target_str = f"{target_rate:.4f}" if not unit else f"{target_rate:.2f} {unit}"
    target_cond_str = f"{target_str} ({condition})"
    footer_text = f"検知日時: {now_str} | Rates By Exchange Rate API"

    if is_test:
        card_title = "🔔 【テスト通知】為替レート監視 ＆ Webhook自動通知ダッシュボード"
        card_desc = (
            custom_message
            if custom_message
            else "Webhook連携は正常に機能しています！条件達成時にこのカード形式で通知が届きます。"
        )
        discord_color = 0x3B82F6
        slack_color = "#3B82F6"
        rate_disp_discord = f"`{rate_str}` (テスト値)"
        rate_disp_slack = f"{rate_str} (テスト値)"
    else:
        card_title = f"🚨 【為替アラート条件達成】 {currency_pair} が目標レートに到達しました！"
        card_desc = (
            custom_message
            if custom_message
            else f"設定した監視条件（{target_cond_str}）を満たしたため自動通知しました。"
        )
        discord_color = 0x10B981
        slack_color = "#10B981"
        rate_disp_discord = f"**{rate_str}**"
        rate_disp_slack = rate_str

    if platform_key == "discord":
        payload: dict[str, Any] = {
            "embeds": [
                {
                    "title": card_title,
                    "description": card_desc,
                    "color": discord_color,
                    "fields": [
                        {
                            "name": "通貨ペア",
                            "value": f"`{currency_pair}`",
                            "inline": True,
                        },
                        {
                            "name": "現在レート",
                            "value": rate_disp_discord,
                            "inline": True,
                        },
                        {
                            "name": "目標レート（条件）",
                            "value": f"`{target_cond_str}`",
                            "inline": True,
                        },
                        {
                            "name": "目標比",
                            "value": f"`{diff_str}`",
                            "inline": True,
                        },
                    ],
                    "footer": {
                        "text": footer_text,
                    },
                }
            ]
        }
    else:
        # Slack および一般的な Incoming Webhook 互換のリッチカード（attachments）形式
        payload = {
            "attachments": [
                {
                    "fallback": f"{card_title} - {currency_pair}: {rate_str} (目標: {target_cond_str})",
                    "color": slack_color,
                    "title": card_title,
                    "text": card_desc,
                    "fields": [
                        {
                            "title": "通貨ペア",
                            "value": currency_pair,
                            "short": True,
                        },
                        {
                            "title": "現在レート",
                            "value": rate_disp_slack,
                            "short": True,
                        },
                        {
                            "title": "目標レート（条件）",
                            "value": target_cond_str,
                            "short": True,
                        },
                        {
                            "title": "目標比",
                            "value": diff_str,
                            "short": True,
                        },
                    ],
                    "footer": footer_text,
                }
            ]
        }

    try:
        response = requests.post(
            cleaned_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        if 200 <= response.status_code < 300:
            kind_str = "テスト通知" if is_test else "アラート通知"
            return {
                "success": True,
                "platform": platform_label,
                "status_code": response.status_code,
                "message": f"{platform_label} へ{kind_str}を送信しました（HTTP {response.status_code}）",
            }
        masked_body = mask_urls(response.text[:120])
        return {
            "success": False,
            "platform": platform_label,
            "status_code": response.status_code,
            "message": f"{platform_label} 送信失敗（HTTP {response.status_code}）: {masked_body}",
        }
    except Exception as exc:
        masked_exc = mask_urls(str(exc))
        return {
            "success": False,
            "platform": platform_label,
            "status_code": None,
            "message": f"{platform_label} への通信エラー: {masked_exc}",
        }


def append_history_csv(
    record: dict[str, Any],
    output_dir: str,
    filename: str = "rate_history.csv",
    log_callback: Callable[[str], None] | None = None,
) -> str:
    """
    指定フォルダ内の rate_history.csv に履歴1件を UTF-8 (BOM付き utf-8-sig) で追記保存する。
    Excel等でファイルが開かれていて PermissionError となった場合は、
    同フォルダ内に rate_history_backup_YYYYMMDD_HHMMSS.csv として退避保存する。
    """
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, filename)

    row_data = {
        "取得日時": record.get("fetched_at") or record.get("取得日時", ""),
        "通貨ペア": record.get("currency_pair") or record.get("通貨ペア", ""),
        "現在レート": record.get("current_rate")
        if "current_rate" in record
        else record.get("現在レート", ""),
        "目標レート": record.get("target_rate")
        if "target_rate" in record
        else record.get("目標レート", ""),
        "判定条件": record.get("condition") or record.get("判定条件", ""),
        "判定結果": record.get("judgment") or record.get("判定結果", ""),
        "通知状況": record.get("notification") or record.get("通知状況", ""),
    }

    df = pd.DataFrame([row_data], columns=HISTORY_COLUMNS)
    file_exists = os.path.isfile(csv_path) and os.path.getsize(csv_path) > 0

    try:
        df.to_csv(
            csv_path,
            mode="a",
            header=not file_exists,
            index=False,
            encoding="utf-8-sig",
        )
        return csv_path
    except PermissionError:
        backup_name = f"rate_history_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        backup_path = os.path.join(output_dir, backup_name)
        backup_exists = os.path.isfile(backup_path) and os.path.getsize(backup_path) > 0
        df.to_csv(
            backup_path,
            mode="a",
            header=not backup_exists,
            index=False,
            encoding="utf-8-sig",
        )
        if log_callback:
            log_callback(
                f"[警告] {filename} がExcel等で開かれていて書き込めないため、{backup_name} に退避保存しました。"
            )
        return backup_path


def load_history_csv(
    output_dir: str,
    filename: str = "rate_history.csv",
    limit: int = 200,
) -> pd.DataFrame:
    """既存の rate_history.csv があれば読み込んで返す（存在しない場合は空のDataFrame）。"""
    csv_path = os.path.join(output_dir, filename)
    if not os.path.isfile(csv_path) or os.path.getsize(csv_path) == 0:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig")
        for col in HISTORY_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        df = df[HISTORY_COLUMNS]
        if len(df) > limit:
            df = df.tail(limit).reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
