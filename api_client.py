from __future__ import annotations

from datetime import datetime, timezone
import os
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
    指定した通貨ペアの最新為替レートを取得する。

    - タイムアウト: デフォルト10秒
    - 通信エラー時は最大 max_retries 回（デフォルト2回）リトライする。
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

        except Exception as exc:
            last_error = exc
            if attempt <= max_retries:
                if log_callback:
                    log_callback(
                        f"[警告] API通信失敗（試行 {attempt}/{total_attempts}）: {exc} -> リトライします..."
                    )
                time.sleep(1.5 * attempt)
            else:
                break

    raise RuntimeError(
        f"為替レートの取得に失敗しました（全{total_attempts}回試行）: {last_error}"
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
    Discord または Slack の Incoming Webhook に通知メッセージをPOST送信する。

    - Discord URL ('discord.com/api/webhooks/...' または 'discordapp.com'): {"content": message}
    - Slack URL ('hooks.slack.com/...'): {"text": message}
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

    if custom_message:
        body_text = custom_message
    elif is_test:
        body_text = (
            f"🔔 **【テスト通知】為替レート監視 ＆ Webhook自動通知ダッシュボード**\n"
            f"・送信日時: `{now_str}`\n"
            f"・送信先判定: `{platform_label}`\n"
            f"・設定中の監視条件: `{currency_pair}` が `{target_str}` （{condition}）\n"
            f"Webhook連携は正常に機能しています！"
        )
    else:
        body_text = (
            f"🚨 **【為替アラート条件達成】 {currency_pair} が目標レートに到達しました！**\n"
            f"・検知日時: `{now_str}`\n"
            f"・通貨ペア: `{currency_pair}`\n"
            f"・現在レート: **{rate_str}** （目標比: `{diff_str}`）\n"
            f"・設定条件: `{target_str}` （{condition}）"
        )

    if platform_key == "discord":
        payload = {"content": body_text}
    else:
        # Slack および一般的な Incoming Webhook 互換形式
        payload = {"text": body_text}

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
        return {
            "success": False,
            "platform": platform_label,
            "status_code": response.status_code,
            "message": f"{platform_label} 送信失敗（HTTP {response.status_code}）: {response.text[:120]}",
        }
    except Exception as exc:
        return {
            "success": False,
            "platform": platform_label,
            "status_code": None,
            "message": f"{platform_label} への通信エラー: {exc}",
        }


def append_history_csv(
    record: dict[str, Any],
    output_dir: str,
    filename: str = "rate_history.csv",
) -> str:
    """
    指定フォルダ内の rate_history.csv に履歴1件を UTF-8 (BOM付き utf-8-sig) で追記保存する。

    record の想定キー:
        - fetched_at (または 取得日時)
        - currency_pair (または 通貨ペア)
        - current_rate (または 現在レート)
        - target_rate (または 目標レート)
        - condition (または 判定条件)
        - judgment (または 判定結果)
        - notification (または 通知状況)
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

    df.to_csv(
        csv_path,
        mode="a",
        header=not file_exists,
        index=False,
        encoding="utf-8-sig",
    )
    return csv_path


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
