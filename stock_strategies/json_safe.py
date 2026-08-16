"""JSON 序列化共用工具

把 numpy 純量（bool_/float64/int64…）轉回 Python 原生型別，否則
json.dump / FastAPI 的 jsonable_encoder 會對 numpy 型別報錯無法序列化。
原本只存在於 api/main.py，抽成共用模組讓 scripts/export_board.py 也能用。
"""

import numpy as np


def json_safe(obj):
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    return obj
