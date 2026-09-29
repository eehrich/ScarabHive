from __future__ import annotations

import re

from .data_loader import KEYWORDS, TYPE_NAMES, load_builtin_data, load_function_specs
from .diagnostics import Diagnostic, Severity


class PatternValidator:
    def __init__(self) -> None:
        self.builtins = load_builtin_data()
        self.function_specs = load_function_specs()
        self.errors: list[Diagnostic] = []
        self.declared_variables: set[str] = set()
        self.declared_functions: set[str] = set()

    def validate(self, text: str) -> list[Diagnostic]:
        self.errors = []
        self.declared_variables.clear()
        self.declared_functions.clear()

        lines = text.splitlines()
        for line in lines:
            self.collect_declarations(line)

        for line_number, line in enumerate(lines, start=1):
            trimmed = line.strip()
            if not trimmed or trimmed.startswith("//"):
                continue
            _ = self.strip_inline_comment(self.remove_string_literals(line))
        return self.errors

    @staticmethod
    def remove_string_literals(line: str) -> str:
        return re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', '""', line)

    @staticmethod
    def strip_inline_comment(line: str) -> str:
        comment_index = line.find("//")
        if comment_index == -1:
            return line
        return line[:comment_index]

    def collect_declarations(self, line: str) -> None:
        variable_pattern = re.compile(
            r"\b(?:var|varip|const)?\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)(?:\[\])?\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*="
        )
        for match in variable_pattern.finditer(line):
            name = match.group(1)
            if name and not self.is_reserved(name):
                self.declared_variables.add(name)

        destructuring_pattern = re.compile(r"\[\s*([A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)\s*\]\s*=")
        for match in destructuring_pattern.finditer(line):
            names = [name.strip() for name in match.group(1).split(",")]
            for name in names:
                if name and not self.is_reserved(name):
                    self.declared_variables.add(name)

        function_pattern = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)\s*=>")
        for match in function_pattern.finditer(line):
            name = match.group(1)
            if name:
                self.declared_functions.add(name)
                self.declared_variables.add(name)

        method_pattern = re.compile(r"\bmethod\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
        for match in method_pattern.finditer(line):
            name = match.group(1)
            if name:
                self.declared_functions.add(name)

        type_pattern = re.compile(r"\btype\s+([A-Za-z_][A-Za-z0-9_]*)\b")
        for match in type_pattern.finditer(line):
            name = match.group(1)
            if name and not self.is_reserved(name):
                self.declared_variables.add(name)

    def check_undefined_namespaces(self, line: str, line_number: int) -> None:
        pattern = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")
        for match in pattern.finditer(line):
            namespace, member = match.group(1), match.group(2)
            column = match.start(1) + 1
            if re.search(r"\w+\s*=\s*$", line[: match.start(1)]):
                continue
            if (
                namespace not in self.builtins.known_namespaces
                and namespace not in self.declared_variables
                and namespace not in self.builtins.standalone_variables
            ):
                self.errors.append(
                    Diagnostic(
                        line=line_number,
                        column=column,
                        length=len(namespace) + len(member) + 1,
                        message=f"Undefined namespace or variable '{namespace}'",
                        severity=Severity.ERROR,
                        source="pattern",
                    )
                )
                continue

            if namespace in self.builtins.known_namespaces:
                continue

    def check_incomplete_references(self, line: str, line_number: int) -> None:
        pattern = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.\s*(?:$|[^A-Za-z_])")
        for match in pattern.finditer(line):
            namespace = match.group(1)
            if namespace in self.builtins.known_namespaces:
                self.errors.append(
                    Diagnostic(
                        line=line_number,
                        column=match.start(1) + 1,
                        length=len(namespace) + 1,
                        message=f"Incomplete reference to '{namespace}' namespace",
                        severity=Severity.ERROR,
                        source="pattern",
                    )
                )

    def check_invalid_var_declarations(self, line: str, line_number: int) -> None:
        pattern = re.compile(
            r"\b(var|varip)\s+(int|float|bool|string|color)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[^,\n]+,\s*([A-Za-z_][A-Za-z0-9_]*)\s*="
        )
        for match in pattern.finditer(line):
            declaration_mode, data_type, first_var, second_var = match.groups()
            self.errors.append(
                Diagnostic(
                    line=line_number,
                    column=match.start(1) + 1,
                    length=len(match.group(0)),
                    message=(
                        "Invalid comma-separated variable declaration. Use separate declarations: "
                        f"{declaration_mode} {data_type} {first_var} = ... and {declaration_mode} {data_type} {second_var} = ..."
                    ),
                    severity=Severity.ERROR,
                    source="pattern",
                )
            )

    def check_undefined_functions(self, line: str, line_number: int) -> None:
        bare_pattern = re.compile(r"(?<!\.)\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
        for match in bare_pattern.finditer(line):
            name = match.group(1)
            if (
                name not in self.function_specs
                and name not in self.builtins.standalone_functions
                and name not in self.declared_functions
                and name not in KEYWORDS
                and name not in TYPE_NAMES
                and name not in {"true", "false", "na"}
            ):
                self.errors.append(
                    Diagnostic(
                        line=line_number,
                        column=match.start(1) + 1,
                        length=len(name),
                        message=f"Undefined function '{name}'",
                        severity=Severity.ERROR,
                        source="pattern",
                    )
                )

        namespaced_pattern = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
        for match in namespaced_pattern.finditer(line):
            namespace, function_name = match.group(1), match.group(2)
            full_name = f"{namespace}.{function_name}"
            generic_known = any(name.startswith(f"{full_name}<") for name in self.function_specs) or any(
                name.startswith(f"{full_name}<") for name in self.builtins.function_paths
            )
            if (
                namespace in self.builtins.known_namespaces
                and full_name not in self.function_specs
                and full_name not in self.builtins.function_paths
                and not generic_known
            ):
                self.errors.append(
                    Diagnostic(
                        line=line_number,
                        column=match.start(2) + 1,
                        length=len(function_name),
                        message=f"Undefined function '{full_name}'",
                        severity=Severity.ERROR,
                        source="pattern",
                    )
                )

    @staticmethod
    def is_reserved(word: str) -> bool:
        return word in KEYWORDS or word in TYPE_NAMES or word in {"true", "false", "break", "continue"}
