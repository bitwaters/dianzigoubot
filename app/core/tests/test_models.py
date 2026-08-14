"""models.py：canonical JSON 与 stable_sha256 测试。"""
from decimal import Decimal

import pytest

from app.core.models import canonical_json, decimal_to_canonical, stable_sha256


class TestDecimalCanonical:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (Decimal("0.5"), "0.5"),
            (Decimal("0.50"), "0.5"),
            (Decimal("1.2300"), "1.23"),
            (Decimal("0"), "0"),
            (Decimal("-0.00"), "0"),
            (Decimal("1E+2"), "100"),
            (Decimal("0.0001"), "0.0001"),
        ],
    )
    def test_format(self, value, expected):
        assert decimal_to_canonical(value) == expected

    def test_reject_non_finite(self):
        with pytest.raises(ValueError):
            decimal_to_canonical(Decimal("NaN"))
        with pytest.raises(ValueError):
            decimal_to_canonical(Decimal("Infinity"))


class TestCanonicalJson:
    def test_sorted_keys_and_no_whitespace(self):
        assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_array_order_preserved(self):
        assert canonical_json([1, 2, 3]) == "[1,2,3]"

    def test_decimal_serialized(self):
        assert canonical_json({"p": Decimal("0.50")}) == '{"p":"0.5"}'

    def test_bool_null_native(self):
        assert canonical_json({"a": True, "b": None}) == '{"a":true,"b":null}'

    def test_nan_rejected(self):
        with pytest.raises(ValueError):
            canonical_json({"x": float("nan")})


class TestStableSha256:
    def test_deterministic(self):
        obj = {"z": 1, "a": Decimal("0.50")}
        assert stable_sha256(obj) == stable_sha256({"a": Decimal("0.5"), "z": 1})

    def test_known_vector(self):
        # 与文档实现共用的测试向量：空对象
        assert (
            stable_sha256({})
            == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
        )
