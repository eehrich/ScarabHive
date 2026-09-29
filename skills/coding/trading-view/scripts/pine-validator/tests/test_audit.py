from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pinescript_validator.audit import extract_doc_symbol_index, run_audit


class AuditTests(unittest.TestCase):
    def test_extract_doc_symbol_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            docs_root = Path(tmpdir)
            (docs_root / "built-ins.md").write_text(
                """
                [ta.sma](https://www.tradingview.com/pine-script-reference/v6/#fun_ta.sma)
                [close](https://www.tradingview.com/pine-script-reference/v6/#var_close)
                [chart.bg_color](https://www.tradingview.com/pine-script-reference/v6/#var_chart.bg_color)
                """,
                encoding="utf-8",
            )

            index = extract_doc_symbol_index(docs_root)

            self.assertEqual(index.files, 1)
            self.assertIn("ta.sma", index.functions)
            self.assertIn("close", index.variables)
            self.assertIn("chart.bg_color", index.variables)

    def test_run_audit_reports_instance_method_hotspots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs_root = root / "docs"
            scripts_root = root / "scripts"
            docs_root.mkdir()
            scripts_root.mkdir()

            (docs_root / "refs.md").write_text(
                """
                [ta.sma](https://www.tradingview.com/pine-script-reference/v6/#fun_ta.sma)
                [plot](https://www.tradingview.com/pine-script-reference/v6/#fun_plot)
                [close](https://www.tradingview.com/pine-script-reference/v6/#var_close)
                """,
                encoding="utf-8",
            )
            (scripts_root / "sample.pine").write_text(
                """
//@version=6
indicator("Audit")
arr = array.new_float()
value = arr.size()
plot(ta.sma(close, 10))
""",
                encoding="utf-8",
            )

            report = run_audit([scripts_root], docs_root, top=10)

            self.assertEqual(report["summary"]["scripts_scanned"], 1)
            self.assertEqual(report["summary"]["total_errors"], 0)
            self.assertTrue(
                any(item["name"] == "ta.sma" for item in report["corpus"]["top_builtin_calls"])
            )
            self.assertTrue(
                any(item["name"] == "size" for item in report["permissive_instance_methods"]["top_instance_method_names"])
            )
            self.assertGreaterEqual(report["diagnostics"]["unused_variable_hints"], 1)


if __name__ == "__main__":
    unittest.main()
