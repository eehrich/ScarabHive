"""One reading of yes/no and number arguments for every plugin.

Plugins kept their own copies and they disagreed: "on" was false in some and
true in others, "null" was TRUE in one (any non-empty text), YAML `.inf`
crashed a plugin's __init__ with OverflowError, `limit: "20.0"` failed with a
bare int() message.
"""
import math

import pytest

from agent_system.utils.params import as_bool, as_float, as_int, bool_param, flag_param


class TestAsBool:
    @pytest.mark.parametrize("value", [True, 1, 1.0, "true", "TRUE", " yes ", "on", "1"])
    def test_true_values(self, value):
        assert as_bool(value) is True

    @pytest.mark.parametrize("value", [False, 0, "false", "No", "off", "0"])
    def test_false_values(self, value):
        assert as_bool(value, True) is False

    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_missing_is_the_default(self, value):
        assert as_bool(value, True) is True
        assert as_bool(value, False) is False

    @pytest.mark.parametrize("value", ["null", "maybe", 2, [1]])
    def test_unusable_text_is_the_default_not_true(self, value):
        assert as_bool(value, False) is False

    def test_strict_names_the_parameter(self):
        with pytest.raises(ValueError, match="append"):
            as_bool("maybe", name="append", strict=True)

    def test_bool_param_raises_and_flag_param_defaults(self):
        with pytest.raises(ValueError, match="force"):
            bool_param({"force": "maybe"}, "force")
        assert flag_param({"force": "maybe"}, "force") is False
        assert flag_param({"force": "on"}, "force") is True


class TestAsInt:
    @pytest.mark.parametrize("value,expected", [
        (5, 5), (5.0, 5), ("5", 5), (" 20.0 ", 20), ("-3", -3), (10**30, 10**30)])
    def test_whole_numbers(self, value, expected):
        assert as_int(value, None) == expected

    @pytest.mark.parametrize("value", [True, False, 2.5, "2.5", "ten", math.inf, math.nan,
                                       "inf", "1e400", [1]])
    def test_unusable_is_the_default(self, value):
        assert as_int(value, 7) == 7

    def test_huge_integer_text_keeps_precision(self):
        assert as_int(str(2**60 + 1), None) == 2**60 + 1

    def test_clamped_but_default_is_not(self):
        assert as_int(500, 0, low=1, high=100) == 100
        assert as_int(-5, 0, low=1, high=100) == 1
        assert as_int(None, None, low=1, high=100) is None

    def test_strict_names_the_parameter(self):
        with pytest.raises(ValueError, match="limit"):
            as_int("ten", 1, name="limit", strict=True)


class TestAsFloat:
    def test_finite_numbers(self):
        assert as_float("2.5", None) == 2.5
        assert as_float(3, None, high=2.0) == 2.0

    @pytest.mark.parametrize("value", [True, "nan", math.inf, "x"])
    def test_unusable_is_the_default(self, value):
        assert as_float(value, 1.5) == 1.5
