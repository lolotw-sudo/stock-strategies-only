"""策略檔載入 / 驗證 / 儲存

策略以 strategies/<id>.json 形式存放。本模組負責：
- 讀取單一策略或全部策略
- 寫入新策略 / 更新現有策略 / 刪除
- 把策略的 params 與預設 CONFIG 合併成扁平 dict，給 evaluate / backtest 使用
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import CONFIG

# 策略目錄：環境變數可覆寫（部署時方便）
STRATEGY_DIR = Path(
    os.environ.get(
        "STRATEGY_DIR",
        str(Path(__file__).resolve().parent.parent / "strategies"),
    )
)

# 允許出現在 params 內的鍵 → 預設值
_PARAM_DEFAULTS: dict = {
    # 基本面
    "eps_threshold": CONFIG["eps_threshold"],
    "roe_threshold": CONFIG["roe_threshold"],
    "fundamental_pass_required": True,
    # 回測
    "backtest_years": CONFIG["backtest_years"],
    "hold_days": CONFIG["hold_days"],
    "min_tech_score_for_signal": CONFIG["min_tech_score_for_signal"],
    # 風險
    "target_return": CONFIG["target_return"],
    "stop_loss": CONFIG["stop_loss"],
    # 評分加權
    "weight_fundamental": 0.3,
    "weight_technical": 0.3,
    "weight_backtest": 0.4,
    "min_total_score_for_buy": CONFIG["min_total_score_for_buy"],
    "min_tech_score_for_buy": 50,
    # 技術訊號開關
    "use_ma_alignment": True,
    "use_bollinger_bounce": True,
    "use_kd_golden_cross": True,
    "use_macd_bullish": True,
    "use_volume_patterns": True,
    "use_kline_signals": False,
    # 大盤濾鏡
    "market_filter_enabled": True,
    "market_filter_ma_period": 20,
}


class StrategyError(ValueError):
    """策略驗證失敗"""


def _slugify(text: str) -> str:
    """產生策略 ID。保留 ASCII 英數字與底線；CJK 中文字會被丟掉，
    若清掉後變空字串就回 uuid。"""
    text = re.sub(r"[^a-zA-Z0-9-_]+", "-", text.strip().lower())
    text = text.strip("-")
    if len(text) < 3:
        return "s-" + uuid.uuid4().hex[:8]
    return text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir() -> None:
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)


def merge_params(strategy: Optional[dict]) -> dict:
    """把策略的 params 蓋在預設值上，回傳扁平 dict。"""
    merged = dict(_PARAM_DEFAULTS)
    if not strategy:
        return merged
    params = strategy.get("params") or {}
    for k, v in params.items():
        if k in merged and v is not None:
            merged[k] = v
    # 健全性：權重總和不為 0
    total = (
        merged["weight_fundamental"]
        + merged["weight_technical"]
        + merged["weight_backtest"]
    )
    if total <= 0:
        merged["weight_fundamental"] = 0.3
        merged["weight_technical"] = 0.3
        merged["weight_backtest"] = 0.4
    return merged


def validate_strategy(data: dict) -> dict:
    """驗證並補齊一份策略 JSON，回傳乾淨版本（可寫入）"""
    if not isinstance(data, dict):
        raise StrategyError("策略必須是 JSON 物件")

    name = (data.get("name") or "").strip()
    if not name:
        raise StrategyError("name 不能空白")

    sid = data.get("id") or _slugify(name) or uuid.uuid4().hex[:8]
    source = data.get("source") or "manual"
    if source not in ("default", "manual", "ai"):
        source = "manual"

    raw_params = data.get("params") or {}
    if not isinstance(raw_params, dict):
        raise StrategyError("params 必須是物件")

    clean_params: dict = {}
    for key, default_val in _PARAM_DEFAULTS.items():
        if key in raw_params and raw_params[key] is not None:
            v = raw_params[key]
            # 型別檢查（簡單版）
            if isinstance(default_val, bool):
                v = bool(v)
            elif isinstance(default_val, int) and not isinstance(default_val, bool):
                v = int(v)
            elif isinstance(default_val, float):
                v = float(v)
            clean_params[key] = v
        else:
            clean_params[key] = default_val

    # 範圍夾擠
    clean_params["target_return"] = max(0.01, min(0.5, clean_params["target_return"]))
    clean_params["stop_loss"] = max(0.01, min(0.5, clean_params["stop_loss"]))
    clean_params["min_total_score_for_buy"] = max(0, min(100, clean_params["min_total_score_for_buy"]))
    clean_params["min_tech_score_for_buy"] = max(0, min(100, clean_params["min_tech_score_for_buy"]))
    clean_params["min_tech_score_for_signal"] = max(0, min(100, clean_params["min_tech_score_for_signal"]))
    clean_params["backtest_years"] = max(1, min(10, clean_params["backtest_years"]))
    clean_params["hold_days"] = max(1, min(120, clean_params["hold_days"]))

    return {
        "id": sid,
        "name": name,
        "description": (data.get("description") or "").strip(),
        "source": source,
        "created_at": data.get("created_at") or _now_iso(),
        "updated_at": _now_iso(),
        "params": clean_params,
    }


def list_strategies() -> list[dict]:
    _ensure_dir()
    out = []
    for p in sorted(STRATEGY_DIR.glob("*.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception as e:
            out.append({"id": p.stem, "name": p.stem, "error": str(e)})
    return out


def get_strategy(sid: str) -> Optional[dict]:
    path = STRATEGY_DIR / f"{sid}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_strategy(data: dict) -> dict:
    """新增或覆寫一份策略。回傳乾淨版本。"""
    _ensure_dir()
    clean = validate_strategy(data)
    path = STRATEGY_DIR / f"{clean['id']}.json"
    if not path.exists():
        # 新檔 → created_at 沿用 validate 的
        pass
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    return clean


def delete_strategy(sid: str) -> bool:
    path = STRATEGY_DIR / f"{sid}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def param_defaults() -> dict:
    """讓 API / 前端拿預設值用"""
    return dict(_PARAM_DEFAULTS)
