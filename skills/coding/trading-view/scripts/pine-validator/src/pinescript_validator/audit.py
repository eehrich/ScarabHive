from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import ast as AST
from .data_loader import load_builtin_data, load_function_specs
from .diagnostics import Severity
from .parser import Parser
from .validator import PineScriptValidator


DOC_SYMBOL_RE = re.compile(r"#(?P<kind>fun|var)_(?P<name>[A-Za-z0-9_.]+)")
UNUSED_VARIABLE_RE = re.compile(r"Variable '([^']+)' is declared but never used\.")
FEATURE_PATTERNS = {
    "imports": re.compile(r"(?m)^\s*import\b"),
    "types": re.compile(r"(?m)^\s*type\b"),
    "enums": re.compile(r"(?m)^\s*enum\b"),
    "methods": re.compile(r"(?m)^\s*method\b"),
    "switches": re.compile(r"\bswitch\b"),
    "destructuring": re.compile(r"\[[^\]]*,[^\]]*\]\s*(?:=|:=)"),
    "compound_assignments": re.compile(r"(?<![<>=!])(?:\+=|-=|\*=|/=|%=)"),
    "generic_types": re.compile(r"\b(?:array|map|matrix|[A-Za-z_]\w*)\s*<"),
    "for_destructuring": re.compile(r"(?m)^\s*for\s+\["),
    "if_expressions": re.compile(r"(?m)(?:=|:=)\s*if\b"),
}


@dataclass(slots=True, frozen=True)
class DocSymbolIndex:
    files: int
    functions: frozenset[str]
    variables: frozenset[str]


class UsageCollector:
    def __init__(self, known_namespaces: set[str]) -> None:
        self.known_namespaces = known_namespaces
        self.function_calls: Counter[str] = Counter()
        self.builtin_function_calls: Counter[str] = Counter()
        self.identifiers: Counter[str] = Counter()
        self.member_accesses: Counter[str] = Counter()
        self.instance_method_paths: Counter[str] = Counter()
        self.instance_method_names: Counter[str] = Counter()
        self.namespace_usage: Counter[str] = Counter()

    def collect_program(self, program: AST.Program, builtin_function_names: set[str]) -> None:
        for statement in program.body:
            self.visit_statement(statement, builtin_function_names)

    def visit_statement(self, statement: AST.Statement, builtin_function_names: set[str]) -> None:
        if isinstance(statement, AST.VariableDeclaration):
            self.identifiers[statement.name] += 1
            if statement.init is not None:
                self.visit_expression(statement.init, builtin_function_names)
            return

        if isinstance(statement, AST.DestructuringAssignment):
            for variable in statement.variables:
                self.identifiers[variable.name] += 1
            self.visit_expression(statement.init, builtin_function_names)
            return

        if isinstance(statement, AST.AssignmentStatement):
            self.identifiers[statement.name] += 1
            self.visit_expression(statement.value, builtin_function_names)
            return

        if isinstance(statement, AST.CompoundAssignmentStatement):
            self.identifiers[statement.name] += 1
            self.visit_expression(statement.value, builtin_function_names)
            return

        if isinstance(statement, AST.TargetAssignmentStatement):
            self.visit_expression(statement.target, builtin_function_names)
            self.visit_expression(statement.value, builtin_function_names)
            return

        if isinstance(statement, AST.ExpressionStatement):
            self.visit_expression(statement.expression, builtin_function_names)
            return

        if isinstance(statement, AST.ReturnStatement):
            self.visit_expression(statement.value, builtin_function_names)
            return

        if isinstance(statement, AST.TypeDeclaration):
            self.identifiers[statement.name] += 1
            for field in statement.fields:
                self.identifiers[field.name] += 1
                if field.default_value is not None:
                    self.visit_expression(field.default_value, builtin_function_names)
            return

        if isinstance(statement, AST.ImportStatement):
            if statement.alias is not None:
                self.identifiers[statement.alias] += 1
            return

        if isinstance(statement, AST.FunctionDeclaration):
            self.identifiers[statement.name] += 1
            for param in statement.params:
                self.identifiers[param.name] += 1
                if param.default_value is not None:
                    self.visit_expression(param.default_value, builtin_function_names)
            for child in statement.body:
                self.visit_statement(child, builtin_function_names)
            return

        if isinstance(statement, AST.IfStatement):
            self.visit_expression(statement.condition, builtin_function_names)
            for child in statement.consequent:
                self.visit_statement(child, builtin_function_names)
            if statement.alternate:
                for child in statement.alternate:
                    self.visit_statement(child, builtin_function_names)
            return

        if isinstance(statement, AST.ForStatement):
            if statement.iterator is not None:
                self.identifiers[statement.iterator] += 1
            for iterator in statement.iterators:
                self.identifiers[iterator.name] += 1
            if statement.from_expr is not None:
                self.visit_expression(statement.from_expr, builtin_function_names)
            if statement.to_expr is not None:
                self.visit_expression(statement.to_expr, builtin_function_names)
            if statement.step_expr is not None:
                self.visit_expression(statement.step_expr, builtin_function_names)
            if statement.iterable is not None:
                self.visit_expression(statement.iterable, builtin_function_names)
            for child in statement.body:
                self.visit_statement(child, builtin_function_names)
            return

        if isinstance(statement, AST.WhileStatement):
            self.visit_expression(statement.condition, builtin_function_names)
            for child in statement.body:
                self.visit_statement(child, builtin_function_names)
            return

        if isinstance(statement, AST.SwitchStatement):
            if statement.expression is not None:
                self.visit_expression(statement.expression, builtin_function_names)
            for case in statement.cases:
                if case.condition is not None:
                    self.visit_expression(case.condition, builtin_function_names)
                for child in case.body:
                    self.visit_statement(child, builtin_function_names)

    def visit_expression(self, expression: AST.Expression, builtin_function_names: set[str]) -> None:
        if isinstance(expression, AST.Identifier):
            self.identifiers[expression.name] += 1
            return

        if isinstance(expression, AST.Literal):
            return

        if isinstance(expression, AST.MemberExpression):
            path = self.expression_path(expression)
            if path is not None:
                self.member_accesses[path] += 1
                namespace = path.split(".", 1)[0]
                if namespace in self.known_namespaces:
                    self.namespace_usage[namespace] += 1
            self.visit_expression(expression.object, builtin_function_names)
            return

        if isinstance(expression, AST.CallExpression):
            for argument in expression.arguments:
                self.visit_expression(argument.value, builtin_function_names)

            path = self.expression_path(expression.callee)
            if path is not None:
                self.function_calls[path] += 1
                if path in builtin_function_names:
                    self.builtin_function_calls[path] += 1
                root = path.split(".", 1)[0]
                if root in self.known_namespaces:
                    self.namespace_usage[root] += 1
                elif isinstance(expression.callee, AST.MemberExpression):
                    method_name = path.rsplit(".", 1)[-1]
                    self.instance_method_paths[path] += 1
                    self.instance_method_names[method_name] += 1

            if isinstance(expression.callee, AST.MemberExpression):
                self.visit_expression(expression.callee.object, builtin_function_names)
            elif isinstance(expression.callee, AST.Identifier):
                self.identifiers[expression.callee.name] += 1
            else:
                self.visit_expression(expression.callee, builtin_function_names)
            return

        if isinstance(expression, AST.BinaryExpression):
            self.visit_expression(expression.left, builtin_function_names)
            self.visit_expression(expression.right, builtin_function_names)
            return

        if isinstance(expression, AST.UnaryExpression):
            self.visit_expression(expression.argument, builtin_function_names)
            return

        if isinstance(expression, AST.TernaryExpression):
            self.visit_expression(expression.condition, builtin_function_names)
            self.visit_expression(expression.consequent, builtin_function_names)
            self.visit_expression(expression.alternate, builtin_function_names)
            return

        if isinstance(expression, AST.IfExpression):
            self.visit_expression(expression.condition, builtin_function_names)
            self.visit_expression(expression.consequent, builtin_function_names)
            self.visit_expression(expression.alternate, builtin_function_names)
            return

        if isinstance(expression, AST.SwitchExpression):
            if expression.expression is not None:
                self.visit_expression(expression.expression, builtin_function_names)
            for case in expression.cases:
                if case.condition is not None:
                    self.visit_expression(case.condition, builtin_function_names)
                for child in case.body:
                    self.visit_statement(child, builtin_function_names)
                self.visit_expression(case.value, builtin_function_names)
            return

        if isinstance(expression, AST.ArrayExpression):
            for element in expression.elements:
                self.visit_expression(element, builtin_function_names)
            return

        if isinstance(expression, AST.IndexExpression):
            self.visit_expression(expression.object, builtin_function_names)
            self.visit_expression(expression.index, builtin_function_names)

    def expression_path(self, expression: AST.Expression) -> str | None:
        if isinstance(expression, AST.Identifier):
            return expression.name
        if isinstance(expression, AST.MemberExpression):
            parent = self.expression_path(expression.object)
            if parent is None:
                return None
            return f"{parent}.{expression.property.name}"
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a smart audit over the Pine Script validator using local docs and sample scripts.")
    parser.add_argument(
        "--scripts-root",
        action="append",
        default=[],
        help="Directory or .pine file to audit. Can be passed multiple times. Defaults to ../ExternalScripts.",
    )
    parser.add_argument(
        "--docs-root",
        default=None,
        help="Directory containing scraped Pine docs markdown files. Defaults to ../PineTool/pinescript_docs.",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Path to write the JSON report. Defaults to smart_audit_report.json in the current working directory.",
    )
    parser.add_argument(
        "--md-out",
        default=None,
        help="Path to write the Markdown report. Defaults to smart_audit_report.md in the current working directory.",
    )
    parser.add_argument("--top", type=int, default=15, help="Number of top entries to include per section.")
    return parser


def compiler_root() -> Path:
    return Path(__file__).resolve().parents[2]


def workspace_root() -> Path:
    return compiler_root().parent


def default_scripts_roots() -> list[Path]:
    return [workspace_root() / "ExternalScripts"]


def default_docs_root() -> Path:
    return workspace_root() / "PineTool" / "pinescript_docs"


def iter_pine_files(paths: list[Path]) -> list[Path]:
    output: set[Path] = set()
    for path in paths:
        if path.is_file() and path.suffix.lower() == ".pine":
            output.add(path.resolve())
            continue
        if path.is_dir():
            for file_path in path.rglob("*.pine"):
                output.add(file_path.resolve())
    return sorted(output)


def extract_doc_symbol_index(docs_root: Path) -> DocSymbolIndex:
    functions: set[str] = set()
    variables: set[str] = set()
    files = 0
    for path in sorted(docs_root.rglob("*.md")):
        files += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in DOC_SYMBOL_RE.finditer(text):
            kind = match.group("kind")
            name = match.group("name")
            if kind == "fun":
                functions.add(name)
            elif kind == "var":
                variables.add(name)
    return DocSymbolIndex(files=files, functions=frozenset(functions), variables=frozenset(variables))


def analyze_feature_presence(text: str) -> set[str]:
    return {name for name, pattern in FEATURE_PATTERNS.items() if pattern.search(text)}


def counter_rows(counter: Counter[str], limit: int, *, label: str = "name") -> list[dict[str, object]]:
    return [{label: name, "count": count} for name, count in counter.most_common(limit)]


def build_recommendations(report: dict[str, object], top: int) -> list[str]:
    summary = report["summary"]
    diagnostics = report["diagnostics"]
    coverage = report["coverage"]
    permissive = report["permissive_instance_methods"]

    recommendations: list[str] = []
    if summary["total_errors"] == 0 and summary["total_warnings"] == 0:
        recommendations.append("Hard failures are clean on the audited corpus, so the next cleanup target is precision rather than parser survival.")
    if summary["total_hints"] > 0:
        hint_ratio = diagnostics["unused_variable_hints"] / summary["total_hints"] if summary["total_hints"] else 0.0
        if hint_ratio >= 0.8:
            recommendations.append("Unused-variable hints dominate the remaining noise, so hint precision should be the first semantic cleanup pass.")
    if coverage["used_documented_functions_missing_validator"]:
        names = ", ".join(item["name"] for item in coverage["used_documented_functions_missing_validator"][: min(5, top)])
        recommendations.append(f"Some documented functions are used in real scripts but still absent from explicit validator metadata: {names}.")
    if coverage["used_documented_variables_missing_validator"]:
        names = ", ".join(item["name"] for item in coverage["used_documented_variables_missing_validator"][: min(5, top)])
        recommendations.append(f"Some documented variables are referenced in the corpus without explicit metadata coverage: {names}.")
    if permissive["top_instance_method_names"]:
        names = ", ".join(item["name"] for item in permissive["top_instance_method_names"][: min(5, top)])
        recommendations.append(f"Type-aware validation for instance-style methods is still permissive; high-frequency method names are {names}.")
    if coverage["documented_functions_missing_validator_count"] > 0:
        recommendations.append("A metadata refresh against the local Pine docs would still improve breadth, even though the current corpus already compiles cleanly.")
    if not recommendations:
        recommendations.append("No obvious cleanup hotspots were detected beyond normal metadata maintenance.")
    return recommendations


def render_markdown(report: dict[str, object], top: int) -> str:
    summary = report["summary"]
    diagnostics = report["diagnostics"]
    coverage = report["coverage"]
    corpus = report["corpus"]
    permissive = report["permissive_instance_methods"]

    lines = [
        "# Pine Validator Smart Audit",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "## Summary",
        f"- Scripts scanned: {summary['scripts_scanned']}",
        f"- Docs files scanned: {summary['docs_files_scanned']}",
        f"- Errors: {summary['total_errors']}",
        f"- Warnings: {summary['total_warnings']}",
        f"- Hints: {summary['total_hints']}",
        "",
        "## Remaining Cleanup",
    ]
    lines.extend(f"- {item}" for item in report["recommendations"])
    lines.extend(
        [
            "",
            "## Diagnostics",
            f"- Files with diagnostics: {diagnostics['files_with_diagnostics']}",
            f"- Unused-variable hints: {diagnostics['unused_variable_hints']}",
            "",
            "Top hint messages:",
        ]
    )
    if diagnostics["top_hint_messages"]:
        lines.extend(f"- {item['name']}: {item['count']}" for item in diagnostics["top_hint_messages"][:top])
    else:
        lines.append("- None")
    lines.extend(["", "Top hint-heavy files:"])
    if diagnostics["top_hint_files"]:
        lines.extend(f"- {item['name']}: {item['count']}" for item in diagnostics["top_hint_files"][:top])
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Coverage",
            f"- Documented functions extracted: {coverage['documented_functions']}",
            f"- Documented variables extracted: {coverage['documented_variables']}",
            f"- Validator function specs: {coverage['validator_function_specs']}",
            f"- Validator builtin variables: {coverage['validator_builtin_variables']}",
            f"- Documented functions missing validator metadata: {coverage['documented_functions_missing_validator_count']}",
            f"- Documented variables missing validator metadata: {coverage['documented_variables_missing_validator_count']}",
            "",
            "Used documented functions missing validator metadata:",
        ]
    )
    if coverage["used_documented_functions_missing_validator"]:
        lines.extend(f"- {item['name']}: {item['count']}" for item in coverage["used_documented_functions_missing_validator"][:top])
    else:
        lines.append("- None")
    lines.extend(["", "Used documented variables missing validator metadata:"])
    if coverage["used_documented_variables_missing_validator"]:
        lines.extend(f"- {item['name']}: {item['count']}" for item in coverage["used_documented_variables_missing_validator"][:top])
    else:
        lines.append("- None")
    lines.extend(["", "## Corpus Signals", "Top builtin calls:"])
    if corpus["top_builtin_calls"]:
        lines.extend(f"- {item['name']}: {item['count']}" for item in corpus["top_builtin_calls"][:top])
    else:
        lines.append("- None")
    lines.extend(["", "Syntax feature prevalence:"])
    if corpus["syntax_feature_files"]:
        lines.extend(f"- {item['name']}: {item['count']} files" for item in corpus["syntax_feature_files"][:top])
    else:
        lines.append("- None")
    lines.extend(["", "Permissive instance-method hotspots:"])
    if permissive["top_instance_method_names"]:
        lines.extend(f"- {item['name']}: {item['count']}" for item in permissive["top_instance_method_names"][:top])
    else:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def run_audit(script_roots: list[Path], docs_root: Path, top: int) -> dict[str, object]:
    builtin_data = load_builtin_data()
    function_specs = load_function_specs()
    builtin_function_names = set(function_specs) | set(builtin_data.function_paths)
    builtin_variable_names = set(builtin_data.variable_paths) | set(builtin_data.standalone_variables)

    docs = extract_doc_symbol_index(docs_root)
    validator = PineScriptValidator()
    usage = UsageCollector(set(builtin_data.known_namespaces))

    severity_counter: Counter[str] = Counter()
    hint_messages: Counter[str] = Counter()
    warning_messages: Counter[str] = Counter()
    error_messages: Counter[str] = Counter()
    hint_files: Counter[str] = Counter()
    unused_hint_variables: Counter[str] = Counter()
    feature_file_counter: Counter[str] = Counter()

    pine_files = iter_pine_files(script_roots)
    files_with_diagnostics = 0

    for file_path in pine_files:
        text = file_path.read_text(encoding="utf-8", errors="replace")
        diagnostics = validator.validate_text(text)
        parser = Parser(text)
        program = parser.parse()
        usage.collect_program(program, builtin_function_names)

        features = analyze_feature_presence(text)
        for feature in features:
            feature_file_counter[feature] += 1

        file_has_diagnostics = False
        for diagnostic in diagnostics:
            severity_name = diagnostic.severity.name.lower()
            severity_counter[severity_name] += 1
            file_has_diagnostics = True
            if diagnostic.severity == Severity.HINT:
                hint_messages[diagnostic.message] += 1
                hint_files[str(file_path)] += 1
                match = UNUSED_VARIABLE_RE.fullmatch(diagnostic.message)
                if match:
                    unused_hint_variables[match.group(1)] += 1
            elif diagnostic.severity == Severity.WARNING:
                warning_messages[diagnostic.message] += 1
            elif diagnostic.severity == Severity.ERROR:
                error_messages[diagnostic.message] += 1
        if file_has_diagnostics:
            files_with_diagnostics += 1

    variable_usage = usage.identifiers + usage.member_accesses

    documented_functions_missing_validator = sorted(docs.functions - builtin_function_names)
    documented_variables_missing_validator = sorted(docs.variables - builtin_variable_names)

    used_documented_functions_missing_validator = Counter(
        {
            name: count
            for name, count in usage.function_calls.items()
            if name in docs.functions and name not in builtin_function_names
        }
    )
    used_documented_variables_missing_validator = Counter(
        {
            name: count
            for name, count in variable_usage.items()
            if name in docs.variables and name not in builtin_variable_names
        }
    )

    report: dict[str, object] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": {
            "scripts_scanned": len(pine_files),
            "docs_files_scanned": docs.files,
            "total_errors": severity_counter["error"],
            "total_warnings": severity_counter["warning"],
            "total_hints": severity_counter["hint"],
        },
        "diagnostics": {
            "files_with_diagnostics": files_with_diagnostics,
            "unused_variable_hints": sum(unused_hint_variables.values()),
            "top_error_messages": counter_rows(error_messages, top),
            "top_warning_messages": counter_rows(warning_messages, top),
            "top_hint_messages": counter_rows(hint_messages, top),
            "top_hint_files": counter_rows(hint_files, top),
            "top_unused_variables": counter_rows(unused_hint_variables, top),
        },
        "coverage": {
            "documented_functions": len(docs.functions),
            "documented_variables": len(docs.variables),
            "validator_function_specs": len(function_specs),
            "validator_builtin_variables": len(builtin_variable_names),
            "documented_functions_missing_validator_count": len(documented_functions_missing_validator),
            "documented_variables_missing_validator_count": len(documented_variables_missing_validator),
            "documented_functions_missing_validator_sample": documented_functions_missing_validator[:top],
            "documented_variables_missing_validator_sample": documented_variables_missing_validator[:top],
            "used_documented_functions_missing_validator": counter_rows(used_documented_functions_missing_validator, top),
            "used_documented_variables_missing_validator": counter_rows(used_documented_variables_missing_validator, top),
        },
        "corpus": {
            "top_builtin_calls": counter_rows(usage.builtin_function_calls, top),
            "top_function_calls": counter_rows(usage.function_calls, top),
            "top_variable_references": counter_rows(variable_usage, top),
            "top_namespaces": counter_rows(usage.namespace_usage, top),
            "syntax_feature_files": counter_rows(feature_file_counter, top),
        },
        "permissive_instance_methods": {
            "top_instance_method_names": counter_rows(usage.instance_method_names, top),
            "top_instance_method_paths": counter_rows(usage.instance_method_paths, top),
        },
        "paths": {
            "scripts_roots": [str(path) for path in script_roots],
            "docs_root": str(docs_root),
        },
    }
    report["recommendations"] = build_recommendations(report, top)
    return report


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scripts_root_values = args.scripts_root or [str(path) for path in default_scripts_roots()]
    script_roots = [Path(value).resolve() for value in scripts_root_values]
    docs_root = Path(args.docs_root).resolve() if args.docs_root else default_docs_root().resolve()
    json_out = Path(args.json_out).resolve() if args.json_out else (compiler_root() / "smart_audit_report.json")
    md_out = Path(args.md_out).resolve() if args.md_out else (compiler_root() / "smart_audit_report.md")

    report = run_audit(script_roots, docs_root, args.top)

    json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_out.write_text(render_markdown(report, args.top), encoding="utf-8")

    print(f"Smart audit written to {json_out}")
    print(f"Smart audit written to {md_out}")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
