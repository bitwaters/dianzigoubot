"""共享基础类型与确定性序列化。

策略哈希、数据集哈希和 run_id 共用本模块的实现（总控文档第 6.1、10.3 节）。
"""
from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal

_ZERO = Decimal(0)


def utc_now_ms() -> int:
    """当前 UTC 毫秒时间戳（文档统一时间单位）。"""
    return int(time.time() * 1000)


def decimal_to_canonical(value: Decimal) -> str:
    """Decimal → 无指数、去除无意义尾零的十进制字符串；零统一为 "0"。"""
    if value.is_nan() or value.is_infinite():
        raise ValueError(f"canonical Decimal 不允许 NaN/Infinity: {value}")
    normalized = value.normalize()
    if normalized == _ZERO:
        return "0"
    return format(normalized, "f")


def canonical_json(obj) -> str:
    """canonical JSON：UTF-8、对象键字典序、数组原顺序、无多余空白。"""
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_canonical_default,
    )


def _canonical_default(o):
    if isinstance(o, Decimal):
        return decimal_to_canonical(o)
    raise TypeError(f"canonical_json 不支持的类型: {type(o)!r}")


def stable_sha256(obj) -> str:
    """对对象的 canonical JSON 计算 SHA-256 十六进制摘要。"""
    payload = canonical_json(obj).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
