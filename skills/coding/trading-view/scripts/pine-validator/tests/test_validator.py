from __future__ import annotations

import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory

from pinescript_validator.agent_report import build_agent_report
from pinescript_validator.cli import _expand_paths, _filter_results, _selected_severities
from pinescript_validator.diagnostics import Severity
from pinescript_validator.sarif import build_sarif_run
from pinescript_validator.validator import PineScriptValidator


class ValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = PineScriptValidator()

    def test_duplicate_variable_definition(self) -> None:
        diagnostics = self.validator.validate_text("a = 1\na = 2")
        self.assertTrue(any("already defined" in diagnostic.message for diagnostic in diagnostics))

    def test_reference_to_variable_declared_later_is_reported(self) -> None:
        diagnostics = self.validator.validate_text(
            "f() =>\n    array.size(ltf_candidates)\n\nvar string[] ltf_candidates = array.new_string()"
        )
        self.assertTrue(any("Undefined variable 'ltf_candidates'" in diagnostic.message for diagnostic in diagnostics))

    def test_reference_to_variable_declared_later_on_same_line_is_reported(self) -> None:
        diagnostics = self.validator.validate_text("a = b, b = 1")
        self.assertTrue(any("Undefined variable 'b'" in diagnostic.message for diagnostic in diagnostics))

    def test_reference_to_variable_declared_earlier_on_same_line_is_allowed(self) -> None:
        diagnostics = self.validator.validate_text("b = 1, a = b")
        self.assertFalse(any("Undefined variable 'b'" in diagnostic.message for diagnostic in diagnostics))

    def test_call_to_function_declared_later_is_reported(self) -> None:
        diagnostics = self.validator.validate_text(
            "f_get_delta_color(x) =>\n    f_get_positive_color()\n\nf_get_positive_color() =>\n    color.green"
        )
        self.assertTrue(any("Undefined function 'f_get_positive_color'" in diagnostic.message for diagnostic in diagnostics))

    def test_call_to_function_declared_earlier_is_allowed(self) -> None:
        diagnostics = self.validator.validate_text(
            "f_get_positive_color() =>\n    color.green\n\nf_get_delta_color(x) =>\n    f_get_positive_color()"
        )
        self.assertFalse(any("Undefined function 'f_get_positive_color'" in diagnostic.message for diagnostic in diagnostics))

    def test_unused_variable(self) -> None:
        diagnostics = self.validator.validate_text('indicator("Test")\nvalue = close')
        self.assertTrue(any("never used" in diagnostic.message for diagnostic in diagnostics))

    def test_shadowing_builtin_variable_is_reported(self) -> None:
        diagnostics = self.validator.validate_text("foo(int bar_index) =>\n    bar_index")
        self.assertTrue(any('Shadowing built-in variable "bar_index"' in diagnostic.message for diagnostic in diagnostics))

    def test_untyped_variable_declaration_cannot_use_na(self) -> None:
        diagnostics = self.validator.validate_text("x = na")
        self.assertTrue(
            any(
                "Value with NA type cannot be assigned to a variable that was defined without type keyword" in diagnostic.message
                for diagnostic in diagnostics
            )
        )

    def test_typed_variable_declaration_can_use_na(self) -> None:
        diagnostics = self.validator.validate_text("float x = na")
        self.assertFalse(
            any(
                "Value with NA type cannot be assigned to a variable that was defined without type keyword" in diagnostic.message
                for diagnostic in diagnostics
            )
        )

    def test_invalid_named_argument(self) -> None:
        diagnostics = self.validator.validate_text('plot(close, invalid_param=true)')
        self.assertTrue(any("Invalid parameter 'invalid_param'" in diagnostic.message for diagnostic in diagnostics))

    def test_indicator_max_polylines_count_range_is_validated(self) -> None:
        diagnostics = self.validator.validate_text(
            'indicator("x", overlay = true, max_boxes_count = 500, max_lines_count = 500, max_labels_count = 500, max_polylines_count = 200)'
        )
        self.assertTrue(
            any(
                'Invalid value "200" for "max_polylines_count" parameter of the "indicator()" function. It must be between 1 and 100'
                in diagnostic.message
                for diagnostic in diagnostics
            )
        )

    def test_indicator_max_bars_back_range_is_validated(self) -> None:
        diagnostics = self.validator.validate_text('indicator("x", max_bars_back = 5001)')
        self.assertTrue(
            any(
                'Invalid value "5001" for "max_bars_back" parameter of the "indicator()" function. It must be between 1 and 5000'
                in diagnostic.message
                for diagnostic in diagnostics
            )
        )

    def test_indicator_calc_bars_count_must_be_positive(self) -> None:
        diagnostics = self.validator.validate_text('indicator("x", calc_bars_count = 0)')
        self.assertTrue(
            any(
                'Invalid value "0" for "calc_bars_count" parameter of the "indicator()" function. It must be greater than or equal to 1'
                in diagnostic.message
                for diagnostic in diagnostics
            )
        )

    def test_plot_count_limit_allows_up_to_64(self) -> None:
        code = 'indicator("x")\n' + "\n".join(f"plot(close, title = \"P{i}\")" for i in range(64))
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Estimated plot count is" in diagnostic.message for diagnostic in diagnostics))

    def test_plot_count_limit_reports_when_exceeded(self) -> None:
        code = 'indicator("x")\n' + "\n".join(f"plot(close, title = \"P{i}\")" for i in range(65))
        diagnostics = self.validator.validate_text(code)
        self.assertTrue(
            any(
                "Estimated plot count is 65, which exceeds the Pine Script limit of 64" in diagnostic.message
                for diagnostic in diagnostics
            )
        )

    def test_dynamic_plot_color_counts_as_additional_plot(self) -> None:
        code = """
indicator("x")
dynamicColor = close > open ? color.green : color.red
""" + "\n".join(f'plot(close, title = "P{i}", color = dynamicColor)' for i in range(33))
        diagnostics = self.validator.validate_text(code)
        self.assertTrue(
            any(
                "Estimated plot count is 66, which exceeds the Pine Script limit of 64" in diagnostic.message
                for diagnostic in diagnostics
            )
        )

    def test_fill_with_const_color_does_not_consume_plot_count(self) -> None:
        code = """
indicator("x")
p1 = plot(close)
p2 = plot(open)
""" + "\n".join(f'fill(p1, p2, color = color.green)' for _ in range(80))
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Estimated plot count is" in diagnostic.message for diagnostic in diagnostics))

    def test_indicator_negative_max_bars_back_is_rejected(self) -> None:
        diagnostics = self.validator.validate_text('indicator("x", max_bars_back = -1)')
        self.assertTrue(
            any(
                'Invalid value "-1" for "max_bars_back" parameter of the "indicator()" function. It must be between 1 and 5000'
                in diagnostic.message
                for diagnostic in diagnostics
            )
        )

    def test_strategy_pyramiding_range_is_validated(self) -> None:
        diagnostics = self.validator.validate_text('strategy("x", pyramiding = 101)')
        self.assertTrue(
            any(
                'Invalid value "101" for "pyramiding" parameter of the "strategy()" function. It must be between 0 and 100'
                in diagnostic.message
                for diagnostic in diagnostics
            )
        )

    def test_strategy_negative_pyramiding_is_rejected(self) -> None:
        diagnostics = self.validator.validate_text('strategy("x", pyramiding = -1)')
        self.assertTrue(
            any(
                'Invalid value "-1" for "pyramiding" parameter of the "strategy()" function. It must be between 0 and 100'
                in diagnostic.message
                for diagnostic in diagnostics
            )
        )

    def test_destructuring_reassignment_is_rejected(self) -> None:
        diagnostics = self.validator.validate_text("[a, b] := foo()")
        self.assertTrue(
            any(
                'Tuple destructuring uses "=" only. Reassignment with ":=" is not valid in Pine Script.' in diagnostic.message
                for diagnostic in diagnostics
            )
        )

    def test_typed_array_declaration(self) -> None:
        diagnostics = self.validator.validate_text("var box[] mc_boxes = array.new_box()")
        self.assertFalse(any("Expected variable name" in diagnostic.message for diagnostic in diagnostics))

    def test_multiline_ternary_assignment(self) -> None:
        code = """
style =
     condition ? size.small :
                 size.normal
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Unexpected token" in diagnostic.message for diagnostic in diagnostics))

    def test_function_with_block_then_tuple_return(self) -> None:
        code = """
foo(x) =>
    if x
        y := 1

    [a, b]
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Expected \"]\" after index expression" in diagnostic.message for diagnostic in diagnostics))

    def test_comment_does_not_trigger_function_error(self) -> None:
        diagnostics = self.validator.validate_text("x = 1 // no reclaim (close sweep)")
        self.assertFalse(any("Undefined function 'reclaim'" in diagnostic.message for diagnostic in diagnostics))

    def test_multiline_comment_is_rejected(self) -> None:
        diagnostics = self.validator.validate_text("/*\nblock\n*/\nindicator(\"x\")")
        self.assertTrue(
            any(
                "Pine Script does not support multiline comments. Use '//' comments instead." in diagnostic.message
                for diagnostic in diagnostics
            )
        )

    def test_generic_function_call_and_dotted_array_type(self) -> None:
        code = "var chart.point[] _points = array.new<chart.point>()"
        diagnostics = self.validator.validate_text(code)
        self.assertEqual([d for d in diagnostics if d.severity == Severity.ERROR], [])

    def test_invalid_builtin_namespace_member_type_keyword_is_reported(self) -> None:
        diagnostics = self.validator.validate_text("label.style x = label.style_label_up")
        self.assertTrue(any('"label.style" is not a valid type keyword.' in d.message for d in diagnostics))

    def test_import_alias_dotted_type_keyword_is_accepted(self) -> None:
        code = """
import user/lib/1 as pt
pt.point p = na
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any('"pt.point" is not a valid type keyword.' in d.message for d in diagnostics))

    def test_switch_statement_parses(self) -> None:
        code = """
switch mode
    "A" =>
        x := 1
    =>
        x := 2
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Unexpected token: =>" in d.message for d in diagnostics))

    def test_switch_expression_without_selector_parses(self) -> None:
        code = """
value = switch
    cond1 => 1
    cond2 => 2
    => 3
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Unexpected token: =>" in d.message for d in diagnostics))

    def test_switch_expression_with_selector_parses(self) -> None:
        code = """
value := switch mode
    "A" => 1
    "B" => 2
    => 3
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Unexpected token: =>" in d.message for d in diagnostics))

    def test_if_expression_multiline_parses(self) -> None:
        code = """
foo() =>
    x = if true
        1
    else
        2
    x
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any('Expected "else" in if expression' in d.message for d in diagnostics))
        self.assertFalse(any("Unexpected token" in d.message for d in diagnostics))

    def test_input_timeframe_active_parameter(self) -> None:
        diagnostics = self.validator.validate_text('tf = input.timeframe("5S", "Lower timeframe", active=true)')
        self.assertFalse(any("Invalid parameter 'active'" in d.message for d in diagnostics))

    def test_bare_builtin_namespaces_are_valid_identifiers(self) -> None:
        diagnostics = self.validator.validate_text("x = barstate\ny = timeframe\nz = syminfo")
        self.assertFalse(any("Undefined variable 'barstate'" in d.message for d in diagnostics))
        self.assertFalse(any("Undefined variable 'timeframe'" in d.message for d in diagnostics))
        self.assertFalse(any("Undefined variable 'syminfo'" in d.message for d in diagnostics))

    def test_documented_namespace_constants_are_accepted(self) -> None:
        code = """
x = size.small
y = text.align_left
z = xloc.bar_time
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Unknown property" in d.message for d in diagnostics))
        self.assertFalse(any("Undefined variable" in d.message for d in diagnostics))

    def test_unknown_property_on_known_namespace_is_reported(self) -> None:
        diagnostics = self.validator.validate_text("x = barstate.not_a_real_member")
        self.assertTrue(any("Unknown property 'not_a_real_member' on namespace 'barstate'" in d.message for d in diagnostics))

    def test_consistency_warning_for_conditional_stateful_user_function(self) -> None:
        code = """
f_hist() =>
    close[1]

if cond
    x = f_hist()
"""
        diagnostics = self.validator.validate_text(code)
        self.assertTrue(any('The function "f_hist" should be called on each calculation for consistency.' in d.message for d in diagnostics))
        self.assertTrue(any("extract the call from this scope" in d.message for d in diagnostics))

    def test_consistency_warning_for_ternary_ta_cum(self) -> None:
        code = """
f(flag) =>
    threshold = flag ? ta.cum(close) / bar_index : 0.0
    threshold
"""
        diagnostics = self.validator.validate_text(code)
        self.assertTrue(any('The function "ta.cum" should be called on each calculation for consistency.' in d.message for d in diagnostics))
        self.assertTrue(any("extract the call from the ternary operator or from the scope" in d.message for d in diagnostics))

    def test_consistency_warning_for_ternary_ta_highest_and_ta_lowest(self) -> None:
        code = """
f_calc(cond, length) =>
    hi = cond ? ta.highest(high[1], length) : na
    lo = cond ? ta.lowest(low[1], length) : na
    [hi, lo]
"""
        diagnostics = self.validator.validate_text(code)
        self.assertTrue(any('The function "ta.highest" should be called on each calculation for consistency.' in d.message for d in diagnostics))
        self.assertTrue(any('The function "ta.lowest" should be called on each calculation for consistency.' in d.message for d in diagnostics))

    def test_consistency_warning_for_user_function_wrapping_ema_and_stdev(self) -> None:
        code = """
f_zscore(source, length) =>
    meanValue = ta.ema(source, length)
    deviationValue = ta.stdev(source, length)
    deviationValue > 0 ? (source - meanValue) / deviationValue : 0.0

value = cond ? f_zscore(close, 20) : na
"""
        diagnostics = self.validator.validate_text(code)
        self.assertTrue(any('The function "f_zscore" should be called on each calculation for consistency.' in d.message for d in diagnostics))
        self.assertTrue(any("extract the call from the ternary operator or from the scope" in d.message for d in diagnostics))

    def test_consistency_warning_for_user_function_wrapping_atr(self) -> None:
        code = """
f_push_struct_label_draw(y) =>
    atrv = ta.atr(14)
    y + atrv

if cond
    value = f_push_struct_label_draw(close)
"""
        diagnostics = self.validator.validate_text(code)
        self.assertTrue(any('The function "f_push_struct_label_draw" should be called on each calculation for consistency.' in d.message for d in diagnostics))
        self.assertTrue(any("extract the call from this scope" in d.message for d in diagnostics))
        self.assertFalse(any('The function "ta.atr" should be called on each calculation for consistency.' in d.message for d in diagnostics))

    def test_consistency_warning_for_conditional_cross_functions_and_sensitive_wrapper(self) -> None:
        code = """
f_detect_breakout_direction(level_up, level_down) =>
    int direction = 0
    if active
        if ta.crossover(close, level_up)
            direction := 1
        else if ta.crossunder(close, level_down)
            direction := -1
    direction

if ready
    breakout_direction = f_detect_breakout_direction(high, low)
"""
        diagnostics = self.validator.validate_text(code)
        self.assertTrue(any('The function "ta.crossover" should be called on each calculation for consistency.' in d.message for d in diagnostics))
        self.assertTrue(any('The function "ta.crossunder" should be called on each calculation for consistency.' in d.message for d in diagnostics))
        self.assertTrue(any('The function "f_detect_breakout_direction" should be called on each calculation for consistency.' in d.message for d in diagnostics))

    def test_pure_user_function_in_conditional_scope_does_not_warn(self) -> None:
        code = """
f_pure(x) =>
    x + 1

if cond
    y = f_pure(close)
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any('The function "f_pure" should be called on each calculation for consistency.' in d.message for d in diagnostics))

    def test_instance_method_call_on_variable(self) -> None:
        code = """
arr = array.new_float()
value = arr.size()
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Undefined function 'arr.size'" in d.message for d in diagnostics))

    def test_destructuring_declares_all_names_for_namespace_checks(self) -> None:
        code = """
[uV15, dV15] = sourceSeries
x = uV15.size()
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Undefined namespace or variable 'uV15'" in d.message for d in diagnostics))

    def test_generic_map_type_annotation_parses(self) -> None:
        diagnostics = self.validator.validate_text("var map<string, bool> alerts = map.new<string, bool>()")
        self.assertFalse(any("Expected variable name" in d.message for d in diagnostics))

    def test_type_declaration_and_member_assignment_parse(self) -> None:
        code = """
type srInfo
    int startTime
    bool active = true

render(srInfo sr) =>
    sr.active := false
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Unexpected token: :=" in d.message for d in diagnostics))
        self.assertFalse(any("Undefined variable 'srInfo'" in d.message for d in diagnostics))

    def test_for_destructuring_iterators(self) -> None:
        code = """
for [i, ln] in lines
    lines.remove(i)
    ln.set_x2(bar_index)
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Undefined variable 'i'" in d.message for d in diagnostics))
        self.assertFalse(any("Undefined variable 'ln'" in d.message for d in diagnostics))

    def test_multiline_function_params_parse(self) -> None:
        code = """
foo(
    int a,
    int b,
    string c
) =>
    a + b
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Unexpected token: ," in d.message for d in diagnostics))
        self.assertFalse(any("Undefined function 'foo'" in d.message for d in diagnostics))

    def test_comma_separated_statements_parse(self) -> None:
        diagnostics = self.validator.validate_text("a := 1, b := 2, c := a + b")
        self.assertFalse(any("Unexpected token: ," in d.message for d in diagnostics))

    def test_for_loop_with_by_clause(self) -> None:
        code = """
for i = 0 to 10 by 1
    x := i
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Undefined variable 'by'" in d.message for d in diagnostics))

    def test_unused_loop_iterator_does_not_emit_hint(self) -> None:
        code = """
for index = 0 to 10
    value := close
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Variable 'index' is declared but never used." in d.message for d in diagnostics))

    def test_unused_destructured_loop_iterators_do_not_emit_hint(self) -> None:
        code = """
for [iter, value] in items
    close
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Variable 'iter' is declared but never used." in d.message for d in diagnostics))
        self.assertFalse(any("Variable 'value' is declared but never used." in d.message for d in diagnostics))

    def test_leading_dot_float_literal(self) -> None:
        diagnostics = self.validator.validate_text("x = .1\ny = .0")
        self.assertFalse(any("Unexpected token: ." in d.message for d in diagnostics))

    def test_trailing_dot_float_literal(self) -> None:
        diagnostics = self.validator.validate_text("x = 0.\ny = 10.")
        self.assertFalse(any("Expected property name" in d.message for d in diagnostics))

    def test_switch_expression_case_block(self) -> None:
        code = """
[a, b] = switch mode
    "A" =>
        [x, y] = foo()
        [x, y]
    =>
        [0, 0]
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Unexpected token: =" in d.message for d in diagnostics))

    def test_timestamp_overloads(self) -> None:
        diagnostics = self.validator.validate_text("x = timestamp(2020, 5, 11)")
        self.assertFalse(any("Too many arguments for 'timestamp'" in d.message for d in diagnostics))

    def test_multiline_destructuring_assignment(self) -> None:
        code = """
[a, b,
 c, d] = foo()
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Expected identifier in destructuring pattern" in d.message for d in diagnostics))

    def test_enum_declaration_namespace_usage(self) -> None:
        code = """
enum MA_Type
    SMA = "SMA"
    EMA = "EMA"

x = MA_Type.SMA
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Undefined variable 'MA_Type'" in d.message for d in diagnostics))

    def test_import_with_alias_is_supported(self) -> None:
        code = """
import TradingView/ta/9 as ta
x = ta.ema(close, 20)
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Undefined variable 'ta'" in d.message for d in diagnostics))

    def test_import_alias_namespace_members_are_not_validated_as_builtin_members(self) -> None:
        code = """
import TradingView/ta/7 as ta
x = ta.dema(close, 20)
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Unknown property 'dema' on namespace 'ta'" in d.message for d in diagnostics))
        self.assertFalse(any("Undefined function 'ta.dema'" in d.message for d in diagnostics))

    def test_type_field_default_cannot_reference_variable(self) -> None:
        code = """
direction_none = 0

type TradeRecord
    int direction = direction_none
"""
        diagnostics = self.validator.validate_text(code)
        self.assertTrue(any('Cannot use "direction_none" as the default value of a type\'s field.' in d.message for d in diagnostics))

    def test_function_argument_reassignment_is_reported_as_mutable(self) -> None:
        code = """
f_update(int x) =>
    x := 1
    x
"""
        diagnostics = self.validator.validate_text(code)
        self.assertTrue(any('Function arguments cannot be mutable ("x")' in d.message for d in diagnostics))

    def test_function_argument_member_reassignment_is_not_reported_as_mutable(self) -> None:
        code = """
type TradeRecord
    bool closed = false

f_update(TradeRecord trade) =>
    trade.closed := true
    trade
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any('Function arguments cannot be mutable ("trade")' in d.message for d in diagnostics))

    def test_line_new_positional_type_mismatches_are_reported(self) -> None:
        diagnostics = self.validator.validate_text(
            "ln = line.new(bar_index, high, bar_index, high, xloc.bar_index, color.new(color.lime, 20), 1, line.style_dotted)"
        )
        self.assertTrue(any('Cannot call "line.new" with argument "extend"=' in d.message for d in diagnostics))
        self.assertTrue(any('Cannot call "line.new" with argument "color"="1".' in d.message for d in diagnostics))

    def test_method_keyword_can_be_variable_name(self) -> None:
        diagnostics = self.validator.validate_text('method = input.string("ADX")')
        self.assertFalse(any("Expected method name" in d.message for d in diagnostics))

    def test_user_type_constructor_namespace_over_builtin_namespace(self) -> None:
        code = """
type session
    int t
x = session.new(1)
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Undefined function 'session.new'" in d.message for d in diagnostics))

    def test_var_series_type_qualifier_declaration_parses(self) -> None:
        code = """
var series float cluster_1 = na
var series float cluster_2 = na
var series int trend_regime = 0
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Variable 'float' is already defined" in d.message for d in diagnostics))
        self.assertFalse(any("Variable 'int' is already defined" in d.message for d in diagnostics))

    def test_inline_switch_case_with_compound_assignment_parses(self) -> None:
        code = """
upAndDownVolume(is_green, is_red, is_neut) =>
    bullVol = 0.0
    bearVol = 0.0
    switch
        is_green => bullVol += volume
        is_red => bearVol -= volume
    [bullVol, bearVol, volume]
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Unexpected token: +=" in d.message for d in diagnostics))
        self.assertFalse(any("Unexpected token: =>" in d.message for d in diagnostics))

    def test_if_expression_with_negative_else_if_branch_parses(self) -> None:
        code = """
Pivots(int length) =>
    float ph = ta.highestbars(high, length) == 0 ? high : na
    float pl = ta.lowestbars(low, length) == 0 ? low  : na
    int dir = na
    dir := if not na(ph) and na(pl)
        1
    else if not na(pl) and na(ph)
        -1
    else
        dir[1]
    [dir, ph, pl]
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any('Expected "else" in if expression' in d.message for d in diagnostics))
        self.assertFalse(any("Undefined variable 'dir'" in d.message for d in diagnostics))

    def test_multiline_call_then_index_suffix_parses(self) -> None:
        code = """
foo() =>
    label.delete(label.new(bar_index,
                 "x",
                 textcolor = chart.bg_color)[1])
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any('Expected ")" after arguments' in d.message for d in diagnostics))

    def test_comparison_expression_is_not_treated_as_generic_type(self) -> None:
        code = """
histShouldUseLines(idx, visibleCount, neededSegments) =>
    idx < math.min(2, visibleCount)
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Unterminated generic type annotation" in d.message for d in diagnostics))
        self.assertFalse(any("Undefined function 'histShouldUseLines'" in d.message for d in diagnostics))

    def test_time_bars_back_named_parameter(self) -> None:
        diagnostics = self.validator.validate_text("t = time(timeframe.period, bars_back = -1)")
        self.assertFalse(any("Invalid parameter 'bars_back'" in d.message for d in diagnostics))

    def test_function_and_variable_name_overload_is_allowed(self) -> None:
        code = """
smema(src, length) =>
    ta.sma(src, length)
float smema = smema(close, 10)
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("already defined at line" in d.message for d in diagnostics))

    def test_type_and_function_name_overload_is_allowed(self) -> None:
        code = """
type level
    float value
level(float x) =>
    x
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("already defined at line" in d.message for d in diagnostics))

    def test_variable_named_color_is_tracked_as_variable_not_type(self) -> None:
        code = """
color = close > open ? color.green : color.red
plot(close, color = color)
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Variable 'color' is declared but never used." in d.message for d in diagnostics))
        self.assertFalse(any("Undefined variable 'color'" in d.message for d in diagnostics))

    def test_typed_variable_named_color_is_tracked_as_variable_not_type(self) -> None:
        code = """
color color = close > open ? color.green : color.red
plot(close, color = color)
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Variable 'color' is declared but never used." in d.message for d in diagnostics))
        self.assertFalse(any("Undefined variable 'color'" in d.message for d in diagnostics))

    def test_variable_named_timeframe_is_tracked_as_variable_not_builtin_namespace(self) -> None:
        code = """
timeframe = "D"
value = timeframe
"""
        diagnostics = self.validator.validate_text(code)
        self.assertFalse(any("Variable 'timeframe' is declared but never used." in d.message for d in diagnostics))

    def test_agent_report_contains_excerpt_pointer_and_suggestion(self) -> None:
        code = 'plot(close, invalid_param=true)'
        report = self.validator.build_agent_report_for_text(code)

        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["error"], 1)
        self.assertEqual(report["diagnostics"][0]["excerpt"], code)
        self.assertIn("^", report["diagnostics"][0]["pointer"])
        self.assertIn("signature", report["diagnostics"][0]["suggestion"])

    def test_agent_report_for_clean_script_marks_ok(self) -> None:
        code = 'indicator("X")\nplot(close)'
        report = self.validator.build_agent_report_for_text(code)

        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["total"], 0)
        self.assertTrue(any("No diagnostics" in step for step in report["next_steps"]))

    def test_expand_paths_collects_pine_files_from_directory(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            nested = root / "nested"
            nested.mkdir()
            (root / "one.pine").write_text('indicator("A")', encoding="utf-8")
            (nested / "two.pine").write_text('indicator("B")', encoding="utf-8")
            (nested / "note.txt").write_text("ignore", encoding="utf-8")

            paths = _expand_paths([str(root)])

            self.assertEqual(len(paths), 2)
            self.assertTrue(all(path.suffix == ".pine" for path in paths))

    def test_build_sarif_run_emits_results(self) -> None:
        diagnostics = self.validator.validate_text("plot(close, invalid_param=true)")
        sarif = build_sarif_run([{"path": Path("sample.pine"), "diagnostics": diagnostics}])

        self.assertEqual(sarif["version"], "2.1.0")
        self.assertEqual(len(sarif["runs"]), 1)
        self.assertGreaterEqual(len(sarif["runs"][0]["results"]), 1)

    def test_diagnostics_are_sorted_by_severity_before_location(self) -> None:
        diagnostics = self.validator.validate_text('indicator("X")\nvalue = close\nplot(close, invalid_param=true)')

        severities = [diagnostic.severity for diagnostic in diagnostics]
        self.assertEqual(severities, sorted(severities))

    def test_selected_severities_respects_cli_toggles(self) -> None:
        selected = _selected_severities(
            Namespace(errors=True, warnings=False, information=False, hints=True)
        )

        self.assertEqual(selected, {Severity.ERROR, Severity.HINT})

    def test_filter_results_removes_disabled_severities(self) -> None:
        diagnostics = self.validator.validate_text('indicator("X")\nvalue = close\nplot(close, invalid_param=true)')
        filtered = _filter_results(
            [{"path": Path("sample.pine"), "text": "", "diagnostics": diagnostics}],
            {Severity.ERROR},
        )

        self.assertTrue(filtered[0]["diagnostics"])
        self.assertTrue(all(item.severity == Severity.ERROR for item in filtered[0]["diagnostics"]))

    def test_agent_report_can_be_built_from_filtered_diagnostics(self) -> None:
        diagnostics = self.validator.validate_text('indicator("X")\nvalue = close\nplot(close, invalid_param=true)')
        filtered = [item for item in diagnostics if item.severity == Severity.ERROR]
        report = build_agent_report(filtered, 'indicator("X")\nvalue = close\nplot(close, invalid_param=true)')

        self.assertEqual(report["summary"]["error"], 1)
        self.assertEqual(report["summary"]["hint"], 0)
        self.assertEqual(len(report["diagnostics"]), 1)


if __name__ == "__main__":
    unittest.main()
