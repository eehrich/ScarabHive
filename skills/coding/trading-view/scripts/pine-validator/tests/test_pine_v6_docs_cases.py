from __future__ import annotations

import unittest

from pinescript_validator.diagnostics import Severity
from pinescript_validator.validator import PineScriptValidator


class PineV6DocsCasesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = PineScriptValidator()

    def test_request_footprint_and_volume_row_types_are_accepted(self) -> None:
        code = """
indicator("Footprint support", overlay = true)
ticksPerRow = input.int(4, "Ticks per row", minval = 1)
valueAreaPercent = input.int(70, "Value area percent", minval = 1, maxval = 100)
imbalancePercent = input.int(300, "Imbalance percent", minval = 1)

footprint reqFootprint = request.footprint(ticksPerRow, valueAreaPercent, imbalancePercent)
array<volume_row> rows = reqFootprint.rows()
if array.size(rows) > 0
    volume_row row = array.get(rows, 0)
    x = row.total_volume()
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any(d.severity == Severity.ERROR for d in diagnostics))

    def test_strategy_when_parameter_is_rejected(self) -> None:
        code = """
strategy("Conditional strategy", overlay = true)
longCondition = ta.crossover(ta.sma(close, 14), ta.sma(close, 28))
strategy.entry("My Long Entry Id", strategy.long, when = longCondition)
"""
        diagnostics = self.validator.validate_text(code)
        self.assertTrue(any("Invalid parameter 'when'" in d.message for d in diagnostics))

    def test_request_security_inside_local_scope_is_accepted(self) -> None:
        code = """
indicator("Dynamic Request In Local Scope")
if bar_index > 10
    x = request.security("NASDAQ:AAPL", "D", close)
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any(d.severity == Severity.ERROR for d in diagnostics))

    def test_numeric_values_must_not_be_implicitly_cast_to_bool(self) -> None:
        code = "color expr = bar_index ? color.green : color.red"
        diagnostics = self.validator.validate_text(code)
        self.assertTrue(any('Condition expression must be of type "bool"' in d.message for d in diagnostics))

    def test_function_calls_must_reject_duplicate_named_arguments(self) -> None:
        code = "plot(close, linewidth = 1, linewidth = 2)"
        diagnostics = self.validator.validate_text(code)
        self.assertTrue(any('repeated argument for parameter "linewidth"' in d.message for d in diagnostics))

    def test_bool_values_cannot_be_na(self) -> None:
        code = "bool flag = na"
        diagnostics = self.validator.validate_text(code)
        self.assertTrue(any('Cannot assign "na" to a "bool" value in Pine Script v6' in d.message for d in diagnostics))


if __name__ == "__main__":
    unittest.main()
