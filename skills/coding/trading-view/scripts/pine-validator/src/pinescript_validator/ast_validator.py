from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from . import ast as AST
from .data_loader import KEYWORDS, TYPE_NAMES, FunctionSpec, load_builtin_data, load_function_specs
from .diagnostics import Diagnostic, Severity

CONSISTENCY_SENSITIVE_BUILTINS = frozenset(
    {
        "ta.cum",
        "ta.cross",
        "ta.crossover",
        "ta.crossunder",
        "ta.highest",
        "ta.lowest",
        "ta.highestbars",
        "ta.lowestbars",
        "ta.stdev",
        "ta.variance",
    }
)

CONSISTENCY_PROPAGATION_BUILTINS = CONSISTENCY_SENSITIVE_BUILTINS | frozenset({"ta.atr"})

PLOT_COUNT_LIMIT = 64


@dataclass(slots=True)
class Symbol:
    name: str
    line: int
    column: int
    kind: str
    type_name: str | None = None
    used: bool = False


@dataclass(slots=True)
class Scope:
    parent: "Scope | None" = None
    symbols: dict[str, Symbol] = field(default_factory=dict)
    children: list["Scope"] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.parent is not None:
            self.parent.children.append(self)

    def define(self, symbol: Symbol) -> Symbol | None:
        existing = self.symbols.get(symbol.name)
        if existing is not None:
            return existing
        self.symbols[symbol.name] = symbol
        return None

    def lookup(self, name: str) -> Symbol | None:
        scope: Scope | None = self
        while scope is not None:
            symbol = scope.symbols.get(name)
            if symbol is not None:
                return symbol
            scope = scope.parent
        return None

    def all_symbols(self) -> list[Symbol]:
        output = list(self.symbols.values())
        for child in self.children:
            output.extend(child.all_symbols())
        return output


class AstValidator:
    def __init__(self) -> None:
        self.builtins = load_builtin_data()
        self.function_specs = load_function_specs()
        self.errors: list[Diagnostic] = []
        self.function_declarations: dict[str, AST.FunctionDeclaration] = {}
        self.consistency_sensitive_functions: set[str] = set(CONSISTENCY_SENSITIVE_BUILTINS)
        self.plot_count = 0
        self.plot_count_reported = False

    def validate(self, program: AST.Program) -> list[Diagnostic]:
        self.errors = []
        self.function_declarations = {}
        self.consistency_sensitive_functions = set(CONSISTENCY_SENSITIVE_BUILTINS)
        self.plot_count = 0
        self.plot_count_reported = False
        self.collect_function_declarations(program.body)
        self.mark_consistency_sensitive_functions()
        global_scope = Scope()
        self.collect_direct_declarations(program.body, global_scope)
        self.validate_block(program.body, global_scope)
        self.check_unused_variables(global_scope)
        return self.errors

    def collect_function_declarations(self, statements: list[AST.Statement]) -> None:
        for statement in statements:
            if isinstance(statement, AST.FunctionDeclaration):
                self.function_declarations[statement.name] = statement
                self.collect_function_declarations(statement.body)
                continue
            if isinstance(statement, AST.IfStatement):
                self.collect_function_declarations(statement.consequent)
                if statement.alternate:
                    self.collect_function_declarations(statement.alternate)
                continue
            if isinstance(statement, AST.ForStatement | AST.WhileStatement):
                self.collect_function_declarations(statement.body)
                continue
            if isinstance(statement, AST.SwitchStatement):
                for case in statement.cases:
                    self.collect_function_declarations(case.body)

    def mark_consistency_sensitive_functions(self) -> None:
        changed = True
        while changed:
            changed = False
            for name, declaration in self.function_declarations.items():
                if name in self.consistency_sensitive_functions:
                    continue
                if any(self.statement_uses_consistency_sensitive_state(statement) for statement in declaration.body):
                    self.consistency_sensitive_functions.add(name)
                    changed = True

    def collect_direct_declarations(self, statements: list[AST.Statement], scope: Scope) -> None:
        for statement in statements:
            if isinstance(statement, AST.FunctionDeclaration):
                self.define_symbol(scope, statement.name, statement.line, statement.column, "function")
            elif isinstance(statement, AST.TypeDeclaration):
                self.define_symbol(scope, statement.name, statement.line, statement.column, "type")
            elif isinstance(statement, AST.ImportStatement):
                if statement.alias:
                    self.define_symbol(scope, statement.alias, statement.line, statement.column, "namespace")
            elif isinstance(statement, AST.VariableDeclaration):
                self.define_symbol(
                    scope,
                    statement.name,
                    statement.name_line,
                    statement.name_column,
                    "variable",
                    self.extract_declared_type_name(statement.type_annotation),
                )
            elif isinstance(statement, AST.DestructuringAssignment):
                for variable in statement.variables:
                    self.define_symbol(scope, variable.name, variable.line, variable.column, "variable")

    def define_symbol(
        self,
        scope: Scope,
        name: str,
        line: int,
        column: int,
        kind: str,
        type_name: str | None = None,
    ) -> None:
        if name == "_":
            return
        if kind in {"variable", "parameter", "iterator"} and name in self.builtins.standalone_variables:
            self.errors.append(
                Diagnostic(
                    line=line,
                    column=column,
                    length=len(name),
                    message=f'Shadowing built-in variable "{name}"',
                    severity=Severity.WARNING,
                    source="ast",
                )
            )
        existing = scope.symbols.get(name)
        if existing is None:
            scope.symbols[name] = Symbol(name=name, line=line, column=column, kind=kind, type_name=type_name)
            return

        if existing.kind == kind:
            self.errors.append(
                Diagnostic(
                    line=line,
                    column=column,
                    length=len(name),
                    message=f"{kind.capitalize()} '{name}' is already defined at line {existing.line}",
                    severity=Severity.ERROR,
                    source="ast",
                )
            )
            return

        # Pine scripts frequently reuse names across type/function/variable domains.
        # Keep the first declaration and avoid false duplicate diagnostics in those cases.
        if {existing.kind, kind}.issubset({"type", "function", "variable"}):
            return

        self.errors.append(
            Diagnostic(
                line=line,
                column=column,
                length=len(name),
                message=f"{kind.capitalize()} '{name}' conflicts with existing {existing.kind} declared at line {existing.line}",
                severity=Severity.ERROR,
                source="ast",
            )
        )

    def validate_block(self, statements: list[AST.Statement], scope: Scope, conditional_context: str | None = None) -> None:
        for statement in statements:
            self.validate_statement(statement, scope, conditional_context)

    def validate_statement(self, statement: AST.Statement, scope: Scope, conditional_context: str | None = None) -> None:
        if isinstance(statement, AST.VariableDeclaration):
            if statement.type_annotation is not None:
                self.validate_type_annotation(statement.type_annotation, scope, statement.line, statement.column)
            if statement.init is not None:
                self.validate_expression(statement.init, scope, conditional_context)
                if statement.type_annotation is None and self.is_na_literal(statement.init):
                    self.untyped_na_declaration_error(statement.name_line, statement.name_column, statement.name)
                if self.is_bool_type_annotation(statement.type_annotation) and self.is_na_literal(statement.init):
                    self.bool_na_error(statement.init.line, statement.init.column)
            return

        if isinstance(statement, AST.DestructuringAssignment):
            self.validate_expression(statement.init, scope, conditional_context)
            return

        if isinstance(statement, AST.AssignmentStatement):
            if statement.name == "_":
                self.validate_expression(statement.value, scope, conditional_context)
                return
            symbol = self.lookup_accessible_symbol(scope, statement.name, statement.name_line, statement.name_column)
            if symbol is None:
                self.undefined_name(statement.name, statement.name_line, statement.name_column)
            elif symbol.kind == "parameter":
                self.mutable_argument_error(statement.name, statement.name_line, statement.name_column)
            self.validate_expression(statement.value, scope, conditional_context)
            return

        if isinstance(statement, AST.CompoundAssignmentStatement):
            if statement.name == "_":
                self.validate_expression(statement.value, scope, conditional_context)
                return
            symbol = self.lookup_accessible_symbol(scope, statement.name, statement.name_line, statement.name_column)
            if symbol is None:
                self.undefined_name(statement.name, statement.name_line, statement.name_column)
            elif symbol.kind == "parameter":
                self.mutable_argument_error(statement.name, statement.name_line, statement.name_column)
            else:
                symbol.used = True
            self.validate_expression(statement.value, scope, conditional_context)
            return

        if isinstance(statement, AST.TargetAssignmentStatement):
            if isinstance(statement.target, AST.Identifier):
                if statement.target.name == "_":
                    self.validate_expression(statement.value, scope, conditional_context)
                    return
                symbol = self.lookup_accessible_symbol(scope, statement.target.name, statement.target.line, statement.target.column)
                if symbol is None:
                    self.undefined_name(statement.target.name, statement.target.line, statement.target.column)
                else:
                    symbol.used = True
            else:
                self.validate_expression(statement.target, scope, conditional_context)
            self.validate_expression(statement.value, scope, conditional_context)
            return

        if isinstance(statement, AST.ExpressionStatement):
            self.validate_expression(statement.expression, scope, conditional_context)
            return

        if isinstance(statement, AST.ReturnStatement):
            self.validate_expression(statement.value, scope, conditional_context)
            return

        if isinstance(statement, AST.TypeDeclaration):
            for field in statement.fields:
                if field.type_annotation is not None:
                    self.validate_type_annotation(field.type_annotation, scope, field.line, field.column)
                if field.default_value is not None:
                    if self.is_bool_type_annotation(field.type_annotation) and self.is_na_literal(field.default_value):
                        self.bool_na_error(field.default_value.line, field.default_value.column)
                    if not self.is_valid_type_field_default(field.default_value):
                        self.errors.append(
                            Diagnostic(
                                line=field.default_value.line,
                                column=field.default_value.column,
                                length=len(self.describe_type_field_default(field.default_value)),
                                message=(
                                    f'Cannot use "{self.describe_type_field_default(field.default_value)}" as the default value of a type\'s field. '
                                    "The default value cannot be a function, variable or calculation."
                                ),
                                severity=Severity.WARNING,
                                source="ast",
                            )
                        )
                    self.validate_expression(field.default_value, scope, conditional_context)
            return

        if isinstance(statement, AST.ImportStatement):
            return

        if isinstance(statement, AST.FunctionDeclaration):
            function_scope = Scope(parent=scope)
            for param in statement.params:
                if param.type_annotation is not None:
                    self.validate_type_annotation(param.type_annotation, scope, param.line, param.column)
                self.define_symbol(
                    function_scope,
                    param.name,
                    param.line,
                    param.column,
                    "parameter",
                    self.extract_declared_type_name(param.type_annotation),
                )
                if param.default_value is not None:
                    self.validate_expression(param.default_value, function_scope)
                    if self.is_bool_type_annotation(param.type_annotation) and self.is_na_literal(param.default_value):
                        self.bool_na_error(param.default_value.line, param.default_value.column)
            self.collect_direct_declarations(statement.body, function_scope)
            self.validate_block(statement.body, function_scope)
            return

        if isinstance(statement, AST.IfStatement):
            self.validate_boolean_condition_expression(statement.condition, scope, conditional_context)
            consequent_scope = Scope(parent=scope)
            self.collect_direct_declarations(statement.consequent, consequent_scope)
            self.validate_block(statement.consequent, consequent_scope, conditional_context or "scope")
            if statement.alternate:
                alternate_scope = Scope(parent=scope)
                self.collect_direct_declarations(statement.alternate, alternate_scope)
                self.validate_block(statement.alternate, alternate_scope, conditional_context or "scope")
            return

        if isinstance(statement, AST.ForStatement):
            loop_scope = Scope(parent=scope)
            if statement.iterators:
                for iterator in statement.iterators:
                    self.define_symbol(loop_scope, iterator.name, iterator.line, iterator.column, "iterator")
            elif statement.iterator is not None:
                self.define_symbol(loop_scope, statement.iterator, statement.line, statement.column, "iterator")
            if statement.from_expr is not None:
                self.validate_expression(statement.from_expr, scope, conditional_context)
            if statement.to_expr is not None:
                self.validate_expression(statement.to_expr, scope, conditional_context)
            if statement.step_expr is not None:
                self.validate_expression(statement.step_expr, scope, conditional_context)
            if statement.iterable is not None:
                self.validate_expression(statement.iterable, scope, conditional_context)
            self.collect_direct_declarations(statement.body, loop_scope)
            self.validate_block(statement.body, loop_scope, conditional_context)
            return

        if isinstance(statement, AST.WhileStatement):
            self.validate_boolean_condition_expression(statement.condition, scope, conditional_context)
            loop_scope = Scope(parent=scope)
            self.collect_direct_declarations(statement.body, loop_scope)
            self.validate_block(statement.body, loop_scope, conditional_context)
            return

        if isinstance(statement, AST.SwitchStatement):
            if statement.expression is not None:
                self.validate_expression(statement.expression, scope, conditional_context)
            for case in statement.cases:
                case_scope = Scope(parent=scope)
                if case.condition is not None:
                    self.validate_expression(case.condition, scope, conditional_context)
                self.collect_direct_declarations(case.body, case_scope)
                self.validate_block(case.body, case_scope, conditional_context or "scope")

    def validate_expression(self, expression: AST.Expression, scope: Scope, conditional_context: str | None = None) -> None:
        if isinstance(expression, AST.Literal):
            return

        if isinstance(expression, AST.Identifier):
            symbol = self.lookup_accessible_symbol(scope, expression.name, expression.line, expression.column)
            if symbol is not None:
                symbol.used = True
                return
            if expression.name in KEYWORDS or expression.name in TYPE_NAMES:
                return
            if expression.name in self.builtins.standalone_variables:
                return
            if expression.name in self.builtins.known_namespaces:
                return
            self.undefined_name(expression.name, expression.line, expression.column)
            return

        if isinstance(expression, AST.MemberExpression):
            if isinstance(expression.object, AST.Identifier):
                symbol = self.lookup_accessible_symbol(scope, expression.object.name, expression.line, expression.column)
                if symbol is not None and symbol.kind in {"type", "namespace"}:
                    return
                if expression.object.name in self.builtins.known_namespaces:
                    namespace_members = self.builtins.namespace_members.get(expression.object.name, frozenset())
                    if namespace_members and expression.property.name not in namespace_members:
                        self.errors.append(
                            Diagnostic(
                                line=expression.property.line,
                                column=expression.property.column,
                                length=len(expression.property.name),
                                message=f"Unknown property '{expression.property.name}' on namespace '{expression.object.name}'",
                                severity=Severity.ERROR,
                                source="ast",
                            )
                        )
                    return
            self.validate_expression(expression.object, scope, conditional_context)
            return

        if isinstance(expression, AST.CallExpression):
            function_name = self.extract_function_name(expression.callee)
            for argument in expression.arguments:
                self.validate_expression(argument.value, scope, conditional_context)
            if self.is_instance_method_call(expression, scope):
                self.validate_expression(expression.callee, scope, conditional_context)
                return
            if function_name is None:
                self.validate_expression(expression.callee, scope, conditional_context)
                return
            symbol = self.lookup_accessible_symbol(scope, function_name, expression.line, expression.column)
            if symbol is not None and symbol.kind == "function":
                symbol.used = True
            resolved_function_name = self.resolve_generic_function_name(function_name)
            if (
                symbol is None
                and resolved_function_name not in self.function_specs
                and resolved_function_name not in self.builtins.function_paths
                and function_name not in self.builtins.standalone_functions
            ):
                self.errors.append(
                    Diagnostic(
                        line=expression.line,
                        column=expression.column,
                        length=len(function_name),
                        message=f"Undefined function '{function_name}'",
                        severity=Severity.ERROR,
                        source="ast",
                    )
                )
                return
            self.warn_on_inconsistent_call(expression, function_name, resolved_function_name, symbol, conditional_context)
            spec = self.function_specs.get(resolved_function_name)
            if spec is not None:
                self.validate_call_signature(expression, resolved_function_name, spec)
            self.validate_special_cases(expression, resolved_function_name)
            self.track_plot_count(expression, resolved_function_name, scope)
            return

        if isinstance(expression, AST.BinaryExpression):
            self.validate_expression(expression.left, scope, conditional_context)
            self.validate_expression(expression.right, scope, conditional_context)
            return

        if isinstance(expression, AST.UnaryExpression):
            self.validate_expression(expression.argument, scope, conditional_context)
            return

        if isinstance(expression, AST.TernaryExpression):
            self.validate_boolean_condition_expression(expression.condition, scope, conditional_context)
            ternary_context = conditional_context or "ternary"
            self.validate_expression(expression.consequent, scope, ternary_context)
            self.validate_expression(expression.alternate, scope, ternary_context)
            return

        if isinstance(expression, AST.IfExpression):
            self.validate_boolean_condition_expression(expression.condition, scope, conditional_context)
            ternary_context = conditional_context or "ternary"
            self.validate_expression(expression.consequent, scope, ternary_context)
            self.validate_expression(expression.alternate, scope, ternary_context)
            return

        if isinstance(expression, AST.SwitchExpression):
            if expression.expression is not None:
                self.validate_expression(expression.expression, scope, conditional_context)
            for case in expression.cases:
                case_scope = Scope(parent=scope)
                if case.condition is not None:
                    self.validate_expression(case.condition, scope, conditional_context)
                self.collect_direct_declarations(case.body, case_scope)
                case_context = conditional_context or "scope"
                self.validate_block(case.body, case_scope, case_context)
                self.validate_expression(case.value, case_scope, case_context)
            return

        if isinstance(expression, AST.ArrayExpression):
            for element in expression.elements:
                self.validate_expression(element, scope, conditional_context)
            return

        if isinstance(expression, AST.IndexExpression):
            self.validate_expression(expression.object, scope, conditional_context)
            self.validate_expression(expression.index, scope, conditional_context)

    def warn_on_inconsistent_call(
        self,
        call: AST.CallExpression,
        function_name: str,
        resolved_function_name: str,
        symbol: Symbol | None,
        conditional_context: str | None,
    ) -> None:
        if conditional_context is None:
            return
        if not self.is_consistency_sensitive_function(function_name, resolved_function_name, symbol):
            return

        if conditional_context == "ternary":
            message = (
                f'The function "{function_name}" should be called on each calculation for consistency. '
                "It is recommended to extract the call from the ternary operator or from the scope"
            )
        else:
            message = (
                f'The function "{function_name}" should be called on each calculation for consistency. '
                "It is recommended to extract the call from this scope"
            )

        self.errors.append(
            Diagnostic(
                line=call.line,
                column=call.column,
                length=len(function_name),
                message=message,
                severity=Severity.WARNING,
                source="ast",
            )
        )

    def is_consistency_sensitive_function(
        self,
        function_name: str,
        resolved_function_name: str,
        symbol: Symbol | None,
    ) -> bool:
        if function_name in self.consistency_sensitive_functions:
            return True
        if resolved_function_name in self.consistency_sensitive_functions:
            return True
        if symbol is not None and symbol.kind == "function" and symbol.name in self.consistency_sensitive_functions:
            return True
        return False

    def statement_uses_consistency_sensitive_state(self, statement: AST.Statement) -> bool:
        if isinstance(statement, AST.VariableDeclaration):
            return statement.init is not None and self.expression_uses_consistency_sensitive_state(statement.init)
        if isinstance(statement, AST.DestructuringAssignment):
            return self.expression_uses_consistency_sensitive_state(statement.init)
        if isinstance(statement, AST.AssignmentStatement | AST.CompoundAssignmentStatement):
            return self.expression_uses_consistency_sensitive_state(statement.value)
        if isinstance(statement, AST.TargetAssignmentStatement):
            return self.expression_uses_consistency_sensitive_state(statement.target) or self.expression_uses_consistency_sensitive_state(statement.value)
        if isinstance(statement, AST.ExpressionStatement):
            return self.expression_uses_consistency_sensitive_state(statement.expression)
        if isinstance(statement, AST.ReturnStatement):
            return self.expression_uses_consistency_sensitive_state(statement.value)
        if isinstance(statement, AST.TypeDeclaration):
            return any(
                field.default_value is not None and self.expression_uses_consistency_sensitive_state(field.default_value)
                for field in statement.fields
            )
        if isinstance(statement, AST.IfStatement):
            if self.expression_uses_consistency_sensitive_state(statement.condition):
                return True
            if any(self.statement_uses_consistency_sensitive_state(item) for item in statement.consequent):
                return True
            if statement.alternate and any(self.statement_uses_consistency_sensitive_state(item) for item in statement.alternate):
                return True
            return False
        if isinstance(statement, AST.ForStatement):
            if statement.from_expr is not None and self.expression_uses_consistency_sensitive_state(statement.from_expr):
                return True
            if statement.to_expr is not None and self.expression_uses_consistency_sensitive_state(statement.to_expr):
                return True
            if statement.step_expr is not None and self.expression_uses_consistency_sensitive_state(statement.step_expr):
                return True
            if statement.iterable is not None and self.expression_uses_consistency_sensitive_state(statement.iterable):
                return True
            return any(self.statement_uses_consistency_sensitive_state(item) for item in statement.body)
        if isinstance(statement, AST.WhileStatement):
            return self.expression_uses_consistency_sensitive_state(statement.condition) or any(
                self.statement_uses_consistency_sensitive_state(item) for item in statement.body
            )
        if isinstance(statement, AST.SwitchStatement):
            if statement.expression is not None and self.expression_uses_consistency_sensitive_state(statement.expression):
                return True
            for case in statement.cases:
                if case.condition is not None and self.expression_uses_consistency_sensitive_state(case.condition):
                    return True
                if any(self.statement_uses_consistency_sensitive_state(item) for item in case.body):
                    return True
            return False
        return False

    def expression_uses_consistency_sensitive_state(self, expression: AST.Expression) -> bool:
        if isinstance(expression, AST.Literal | AST.Identifier):
            return False
        if isinstance(expression, AST.IndexExpression):
            return True
        if isinstance(expression, AST.MemberExpression):
            return self.expression_uses_consistency_sensitive_state(expression.object)
        if isinstance(expression, AST.CallExpression):
            function_name = self.extract_function_name(expression.callee)
            resolved_function_name = self.resolve_generic_function_name(function_name) if function_name is not None else None
            if function_name in self.consistency_sensitive_functions or function_name in CONSISTENCY_PROPAGATION_BUILTINS:
                return True
            if (
                resolved_function_name is not None
                and (
                    resolved_function_name in self.consistency_sensitive_functions
                    or resolved_function_name in CONSISTENCY_PROPAGATION_BUILTINS
                )
            ):
                return True
            if self.expression_uses_consistency_sensitive_state(expression.callee):
                return True
            return any(self.expression_uses_consistency_sensitive_state(argument.value) for argument in expression.arguments)
        if isinstance(expression, AST.BinaryExpression):
            return self.expression_uses_consistency_sensitive_state(expression.left) or self.expression_uses_consistency_sensitive_state(expression.right)
        if isinstance(expression, AST.UnaryExpression):
            return self.expression_uses_consistency_sensitive_state(expression.argument)
        if isinstance(expression, AST.TernaryExpression):
            return (
                self.expression_uses_consistency_sensitive_state(expression.condition)
                or self.expression_uses_consistency_sensitive_state(expression.consequent)
                or self.expression_uses_consistency_sensitive_state(expression.alternate)
            )
        if isinstance(expression, AST.IfExpression):
            return (
                self.expression_uses_consistency_sensitive_state(expression.condition)
                or self.expression_uses_consistency_sensitive_state(expression.consequent)
                or self.expression_uses_consistency_sensitive_state(expression.alternate)
            )
        if isinstance(expression, AST.SwitchExpression):
            if expression.expression is not None and self.expression_uses_consistency_sensitive_state(expression.expression):
                return True
            for case in expression.cases:
                if case.condition is not None and self.expression_uses_consistency_sensitive_state(case.condition):
                    return True
                if any(self.statement_uses_consistency_sensitive_state(item) for item in case.body):
                    return True
                if self.expression_uses_consistency_sensitive_state(case.value):
                    return True
            return False
        if isinstance(expression, AST.ArrayExpression):
            return any(self.expression_uses_consistency_sensitive_state(element) for element in expression.elements)
        return False

    @staticmethod
    def extract_function_name(expression: AST.Expression) -> str | None:
        if isinstance(expression, AST.Identifier):
            return expression.name
        if isinstance(expression, AST.MemberExpression) and isinstance(expression.object, AST.Identifier):
            return f"{expression.object.name}.{expression.property.name}"
        return None

    def is_instance_method_call(self, call: AST.CallExpression, scope: Scope) -> bool:
        callee = call.callee
        if not isinstance(callee, AST.MemberExpression):
            return False
        if isinstance(callee.object, AST.Identifier):
            symbol = scope.lookup(callee.object.name)
            if symbol is not None and symbol.kind in {"type", "namespace"}:
                return True
            if callee.object.name in self.builtins.known_namespaces:
                return False
        return True

    def resolve_generic_function_name(self, function_name: str) -> str:
        if function_name in self.function_specs or function_name in self.builtins.function_paths:
            return function_name
        generic_prefix = f"{function_name}<"
        for name in self.function_specs:
            if name.startswith(generic_prefix):
                return name
        for name in self.builtins.function_paths:
            if name.startswith(generic_prefix):
                return name
        return function_name

    def validate_call_signature(self, call: AST.CallExpression, function_name: str, spec: FunctionSpec) -> None:
        positional_args = [arg for arg in call.arguments if arg.name is None]
        named_arguments = [arg for arg in call.arguments if arg.name is not None]
        duplicate_named = [name for name, count in Counter(arg.name for arg in named_arguments).items() if count > 1]
        if duplicate_named:
            for name in sorted(duplicate_named):
                self.errors.append(
                    Diagnostic(
                        line=call.line,
                        column=call.column,
                        length=len(name),
                        message=f'Function call cannot include repeated argument for parameter "{name}"',
                        severity=Severity.ERROR,
                        source="ast",
                    )
                )
            return

        provided_named = {arg.name for arg in named_arguments}
        if function_name.startswith("input."):
            provided_named = {
                name
                for name in provided_named
                if name not in {"display", "active"}
            }

        best_score: int | None = None
        best_errors: list[Diagnostic] = []

        for overload in spec.overloads:
            overload_errors: list[Diagnostic] = []
            score = 0
            all_params = list(overload.required_params) + list(overload.optional_params)

            if not overload.variadic and len(positional_args) > len(all_params):
                overload_errors.append(
                    Diagnostic(
                        line=call.line,
                        column=call.column,
                        length=len(function_name),
                        message=f"Too many arguments for '{function_name}'. Expected at most {len(all_params)}, got {len(positional_args)}",
                        severity=Severity.ERROR,
                        source="ast",
                    )
                )
                score += 100

            for name in provided_named:
                if name not in all_params:
                    overload_errors.append(
                        Diagnostic(
                            line=call.line,
                            column=call.column,
                            length=len(name),
                            message=f"Invalid parameter '{name}' for '{function_name}'",
                            severity=Severity.ERROR,
                            source="ast",
                        )
                    )
                    score += 10

            positional_count = len(positional_args)
            for index, param_name in enumerate(overload.required_params):
                provided_positionally = index < positional_count
                provided_by_name = param_name in provided_named
                if not provided_positionally and not provided_by_name:
                    overload_errors.append(
                        Diagnostic(
                            line=call.line,
                            column=call.column,
                            length=len(function_name),
                            message=f"Missing required parameter '{param_name}' for '{function_name}'",
                            severity=Severity.ERROR,
                            source="ast",
                        )
                    )
                    score += 5

            if not overload_errors:
                return
            if best_score is None or score < best_score:
                best_score = score
                best_errors = overload_errors

        self.errors.extend(best_errors)

    def validate_special_cases(self, call: AST.CallExpression, function_name: str) -> None:
        if function_name == "line.new":
            self.validate_line_new_positional_types(call)

        if function_name == "plotshape":
            for argument in call.arguments:
                if argument.name == "shape":
                    self.errors.append(
                        Diagnostic(
                            line=call.line,
                            column=call.column,
                            length=5,
                            message='Invalid parameter "shape". Did you mean "style"?',
                            severity=Severity.ERROR,
                            source="ast",
                        )
                    )
                    break

        if function_name == "plotchar":
            for argument in call.arguments:
                if argument.name == "shape":
                    self.errors.append(
                        Diagnostic(
                            line=call.line,
                            column=call.column,
                            length=5,
                            message='Invalid parameter "shape". Did you mean "char"?',
                            severity=Severity.ERROR,
                            source="ast",
                        )
                    )
                    break

        if function_name in {"indicator", "strategy"}:
            self.validate_declaration_parameter_ranges(call, function_name)
            has_timeframe = any(argument.name == "timeframe" for argument in call.arguments)
            has_timeframe_gaps = any(argument.name == "timeframe_gaps" for argument in call.arguments)
            if has_timeframe_gaps and not has_timeframe:
                self.errors.append(
                    Diagnostic(
                        line=call.line,
                        column=call.column,
                        length=len(function_name),
                        message='"timeframe_gaps" has no effect without a "timeframe" argument',
                        severity=Severity.WARNING,
                        source="ast",
                    )
                )

    def validate_declaration_parameter_ranges(self, call: AST.CallExpression, function_name: str) -> None:
        range_constraints: dict[str, tuple[int, int | None]] = {
            "max_bars_back": (1, 5000),
            "max_boxes_count": (1, 500),
            "max_labels_count": (1, 500),
            "max_lines_count": (1, 500),
            "max_polylines_count": (1, 100),
            "calc_bars_count": (1, None),
        }
        if function_name == "strategy":
            range_constraints["pyramiding"] = (0, 100)

        for argument in call.arguments:
            if argument.name not in range_constraints:
                continue
            numeric_literal = self.extract_integer_literal(argument.value)
            if numeric_literal is None:
                continue
            minimum, maximum = range_constraints[argument.name]
            numeric_value, raw_value, line, column = numeric_literal
            if maximum is None:
                if numeric_value >= minimum:
                    continue
                message = (
                    f'Invalid value "{raw_value}" for "{argument.name}" parameter of the "{function_name}()" function. '
                    f"It must be greater than or equal to {minimum}"
                )
            else:
                if minimum <= numeric_value <= maximum:
                    continue
                message = (
                    f'Invalid value "{raw_value}" for "{argument.name}" parameter of the "{function_name}()" function. '
                    f"It must be between {minimum} and {maximum}"
                )

            self.errors.append(
                Diagnostic(
                    line=line,
                    column=column,
                    length=len(raw_value),
                    message=message,
                    severity=Severity.ERROR,
                    source="ast",
                )
            )

    def validate_line_new_positional_types(self, call: AST.CallExpression) -> None:
        positional_args = [argument.value for argument in call.arguments if argument.name is None]
        if len(positional_args) < 6:
            return

        checks = [
            (5, "extend", {"extend"}, "series string"),
            (6, "color", {"color"}, "series color"),
            (7, "style", {"line_style"}, "series string"),
            (8, "width", {"number"}, "series int"),
            (9, "force_overlay", {"bool"}, "series bool"),
        ]

        for index, param_name, expected_categories, expected_type in checks:
            if index >= len(positional_args):
                break
            actual_category, actual_type, actual_description = self.describe_argument_type(positional_args[index])
            if actual_category is None or actual_category in expected_categories:
                continue
            self.errors.append(
                Diagnostic(
                    line=positional_args[index].line,
                    column=positional_args[index].column,
                    length=max(1, len(actual_description)),
                    message=(
                        f'Cannot call "line.new" with argument "{param_name}"="{actual_description}". '
                        f'An argument of "{actual_type}" type was used but a "{expected_type}" is expected.'
                    ),
                    severity=Severity.ERROR,
                    source="ast",
                )
            )

    def track_plot_count(self, call: AST.CallExpression, function_name: str, scope: Scope) -> None:
        plot_count = self.estimate_plot_count(call, function_name, scope)
        if plot_count <= 0:
            return

        self.plot_count += plot_count
        if self.plot_count_reported or self.plot_count <= PLOT_COUNT_LIMIT:
            return

        self.plot_count_reported = True
        self.errors.append(
            Diagnostic(
                line=call.line,
                column=call.column,
                length=len(function_name),
                message=(
                    f'Estimated plot count is {self.plot_count}, which exceeds the Pine Script limit of {PLOT_COUNT_LIMIT}. '
                    "Reduce plot-producing calls or simplify dynamic color usage."
                ),
                severity=Severity.ERROR,
                source="ast",
            )
        )

    def estimate_plot_count(self, call: AST.CallExpression, function_name: str, scope: Scope) -> int:
        if function_name == "plot":
            return 1 + self.dynamic_plot_argument_count(call, scope, ("color",))
        if function_name == "plotarrow":
            return 1 + self.dynamic_plot_argument_count(call, scope, ("colorup", "colordown"))
        if function_name == "plotbar":
            return 4 + self.dynamic_plot_argument_count(call, scope, ("color",))
        if function_name == "plotcandle":
            return 4 + self.dynamic_plot_argument_count(call, scope, ("color", "wickcolor", "bordercolor"))
        if function_name in {"plotchar", "plotshape"}:
            return 1 + self.dynamic_plot_argument_count(call, scope, ("color", "textcolor"))
        if function_name in {"alertcondition", "bgcolor", "barcolor"}:
            return 1
        if function_name == "fill":
            color_argument = self.get_argument_value(call, "color", 2)
            return 1 if color_argument is not None and not self.is_const_plot_expression(color_argument, scope) else 0
        return 0

    def dynamic_plot_argument_count(
        self,
        call: AST.CallExpression,
        scope: Scope,
        parameter_names: tuple[str, ...],
    ) -> int:
        return sum(
            1
            for index, parameter_name in enumerate(parameter_names)
            if (
                argument := self.get_argument_value(call, parameter_name, index + 1)
            ) is not None
            and not self.is_const_plot_expression(argument, scope)
        )

    def get_argument_value(self, call: AST.CallExpression, name: str, positional_index: int) -> AST.Expression | None:
        for argument in call.arguments:
            if argument.name == name:
                return argument.value

        positional_arguments = [argument.value for argument in call.arguments if argument.name is None]
        if positional_index < len(positional_arguments):
            return positional_arguments[positional_index]
        return None

    def is_const_plot_expression(self, expression: AST.Expression, scope: Scope) -> bool:
        if isinstance(expression, AST.Literal):
            return True

        if isinstance(expression, AST.Identifier):
            symbol = self.lookup_accessible_symbol(scope, expression.name, expression.line, expression.column)
            if symbol is not None and symbol.kind == "variable":
                return False
            return expression.name in self.builtins.standalone_variables

        if isinstance(expression, AST.MemberExpression):
            return isinstance(expression.object, AST.Identifier) and expression.object.name == "color"

        if isinstance(expression, AST.UnaryExpression):
            return self.is_const_plot_expression(expression.argument, scope)

        if isinstance(expression, AST.BinaryExpression):
            return self.is_const_plot_expression(expression.left, scope) and self.is_const_plot_expression(expression.right, scope)

        if isinstance(expression, AST.TernaryExpression):
            return (
                self.is_const_plot_expression(expression.condition, scope)
                and self.is_const_plot_expression(expression.consequent, scope)
                and self.is_const_plot_expression(expression.alternate, scope)
            )

        if isinstance(expression, AST.IfExpression):
            return (
                self.is_const_plot_expression(expression.condition, scope)
                and self.is_const_plot_expression(expression.consequent, scope)
                and self.is_const_plot_expression(expression.alternate, scope)
            )

        if isinstance(expression, AST.CallExpression):
            function_name = self.extract_function_name(expression.callee)
            if function_name in {"color.new", "color.rgb"}:
                return all(self.is_const_plot_expression(argument.value, scope) for argument in expression.arguments)
            return False

        if isinstance(expression, AST.ArrayExpression):
            return all(self.is_const_plot_expression(element, scope) for element in expression.elements)

        if isinstance(expression, AST.IndexExpression):
            return self.is_const_plot_expression(expression.object, scope) and self.is_const_plot_expression(expression.index, scope)

        if isinstance(expression, AST.SwitchExpression):
            if expression.expression is not None and not self.is_const_plot_expression(expression.expression, scope):
                return False
            return all(
                (case.condition is None or self.is_const_plot_expression(case.condition, scope))
                and self.is_const_plot_expression(case.value, scope)
                for case in expression.cases
            )

        return False

    def describe_argument_type(self, expression: AST.Expression) -> tuple[str | None, str, str]:
        if isinstance(expression, AST.Literal):
            if isinstance(expression.value, bool):
                return "bool", "literal bool", expression.raw
            if isinstance(expression.value, float):
                if "." not in expression.raw:
                    return "number", "literal int", expression.raw
                return "number", "literal float", expression.raw
            if expression.value == "na":
                return "na", "literal na", expression.raw
            return "string", "literal string", expression.raw

        if isinstance(expression, AST.Identifier):
            return None, "unknown", expression.name

        if isinstance(expression, AST.MemberExpression) and isinstance(expression.object, AST.Identifier):
            full_name = f"{expression.object.name}.{expression.property.name}"
            if expression.object.name == "extend":
                return "extend", "series string", full_name
            if expression.object.name == "xloc":
                return "xloc", "series string", full_name
            if expression.object.name == "line" and expression.property.name.startswith("style_"):
                return "line_style", "series string", full_name
            if expression.object.name == "color":
                return "color", "const color", full_name
            return None, "unknown", full_name

        if isinstance(expression, AST.CallExpression):
            function_name = self.extract_function_name(expression.callee)
            if function_name and function_name.startswith("color."):
                return "color", "const color", f'call "{function_name}" (const color)'
            if function_name:
                return None, "unknown", f'call "{function_name}"'
            return None, "unknown", "call"

        if isinstance(expression, AST.UnaryExpression):
            category, actual_type, actual_description = self.describe_argument_type(expression.argument)
            return category, actual_type, f"{expression.operator}{actual_description}"

        return None, "unknown", "expression"

    def is_valid_type_field_default(self, expression: AST.Expression) -> bool:
        if isinstance(expression, AST.Literal):
            return True
        if isinstance(expression, AST.UnaryExpression):
            return isinstance(expression.argument, AST.Literal)
        if isinstance(expression, AST.MemberExpression) and isinstance(expression.object, AST.Identifier):
            return expression.object.name in self.builtins.known_namespaces
        return False

    def describe_type_field_default(self, expression: AST.Expression) -> str:
        if isinstance(expression, AST.Identifier):
            return expression.name
        if isinstance(expression, AST.MemberExpression) and isinstance(expression.object, AST.Identifier):
            return f"{expression.object.name}.{expression.property.name}"
        if isinstance(expression, AST.Literal):
            return expression.raw
        if isinstance(expression, AST.CallExpression):
            function_name = self.extract_function_name(expression.callee)
            return function_name or "call"
        return "expression"

    def validate_type_annotation(self, type_annotation: AST.TypeAnnotation, scope: Scope, line: int, column: int) -> None:
        if self.is_valid_type_keyword(type_annotation.name, scope, line, column):
            return
        self.errors.append(
            Diagnostic(
                line=line,
                column=column,
                length=len(type_annotation.name),
                message=f'"{type_annotation.name}" is not a valid type keyword.',
                severity=Severity.ERROR,
                source="ast",
            )
        )

    def is_valid_type_keyword(self, type_name: str, scope: Scope, line: int, column: int) -> bool:
        normalized = self.strip_array_suffix(type_name)
        generic_name, generic_args = self.split_generic_type(normalized)

        if generic_args is not None:
            if generic_name == "array":
                return len(generic_args) == 1 and self.is_valid_type_keyword(generic_args[0], scope, line, column)
            if generic_name == "matrix":
                return len(generic_args) == 1 and self.is_valid_type_keyword(generic_args[0], scope, line, column)
            if generic_name == "map":
                return len(generic_args) == 2 and all(self.is_valid_type_keyword(arg, scope, line, column) for arg in generic_args)
            return False

        if normalized in TYPE_NAMES:
            return True

        symbol = self.lookup_accessible_symbol(scope, normalized, line, column)
        if symbol is not None and symbol.kind in {"type", "namespace"}:
            return True

        if "." in normalized:
            if f"{normalized}.new" in self.builtins.function_paths:
                return True
            root = normalized.split(".", 1)[0]
            root_symbol = self.lookup_accessible_symbol(scope, root, line, column)
            if root_symbol is not None and root_symbol.kind == "namespace":
                return True
            return False

        return False

    @staticmethod
    def strip_array_suffix(type_name: str) -> str:
        normalized = type_name.strip()
        while normalized.endswith("[]"):
            normalized = normalized[:-2].strip()
        return normalized

    @staticmethod
    def split_generic_type(type_name: str) -> tuple[str, list[str] | None]:
        lt_index = type_name.find("<")
        if lt_index == -1 or not type_name.endswith(">"):
            return type_name, None

        outer_name = type_name[:lt_index].strip()
        inner = type_name[lt_index + 1 : -1]
        args: list[str] = []
        start = 0
        depth = 0
        for index, char in enumerate(inner):
            if char == "<":
                depth += 1
            elif char == ">":
                depth -= 1
            elif char == "," and depth == 0:
                args.append(inner[start:index].strip())
                start = index + 1
        args.append(inner[start:].strip())
        if any(not arg for arg in args):
            return outer_name, []
        return outer_name, args

    def extract_integer_literal(self, expression: AST.Expression) -> tuple[int, str, int, int] | None:
        if isinstance(expression, AST.Literal) and isinstance(expression.value, float):
            integer_value = int(expression.value)
            if expression.value == float(integer_value):
                return integer_value, expression.raw, expression.line, expression.column
            return None
        if isinstance(expression, AST.UnaryExpression) and expression.operator in {"-", "+"}:
            inner = self.extract_integer_literal(expression.argument)
            if inner is None:
                return None
            value, raw_value, line, column = inner
            signed_value = value if expression.operator == "+" else -value
            return signed_value, f"{expression.operator}{raw_value}", expression.line, expression.column
        return None

    def validate_boolean_condition_expression(
        self,
        expression: AST.Expression,
        scope: Scope,
        conditional_context: str | None = None,
    ) -> None:
        self.validate_expression(expression, scope, conditional_context)
        inferred = self.infer_boolean_expression(expression, scope)
        if inferred is False:
            self.errors.append(
                Diagnostic(
                    line=expression.line,
                    column=expression.column,
                    length=max(1, len(self.describe_expression(expression))),
                    message='Condition expression must be of type "bool" in Pine Script v6',
                    severity=Severity.ERROR,
                    source="ast",
                )
            )

    def infer_boolean_expression(self, expression: AST.Expression, scope: Scope) -> bool | None:
        if isinstance(expression, AST.Literal):
            if isinstance(expression.value, bool):
                return True
            return False

        if isinstance(expression, AST.Identifier):
            symbol = self.lookup_accessible_symbol(scope, expression.name, expression.line, expression.column)
            if symbol is not None and symbol.type_name == "bool":
                return True
            if expression.name in self.builtins.standalone_variables:
                return False
            return None

        if isinstance(expression, AST.MemberExpression):
            if isinstance(expression.object, AST.Identifier):
                if expression.object.name == "barstate":
                    return True
                symbol = self.lookup_accessible_symbol(scope, expression.object.name, expression.line, expression.column)
                if symbol is not None and symbol.type_name == "bool":
                    return True
            return None

        if isinstance(expression, AST.CallExpression):
            function_name = self.extract_function_name(expression.callee)
            if function_name in {"ta.cross", "ta.crossover", "ta.crossunder"}:
                return True
            return None

        if isinstance(expression, AST.UnaryExpression):
            if expression.operator == "not":
                return True
            return False

        if isinstance(expression, AST.BinaryExpression):
            if expression.operator in {"==", "!=", ">", "<", ">=", "<=", "and", "or"}:
                return True
            return False

        if isinstance(expression, AST.TernaryExpression):
            consequent = self.infer_boolean_expression(expression.consequent, scope)
            alternate = self.infer_boolean_expression(expression.alternate, scope)
            if consequent is True and alternate is True:
                return True
            if consequent is False or alternate is False:
                return False
            return None

        if isinstance(expression, AST.IfExpression):
            consequent = self.infer_boolean_expression(expression.consequent, scope)
            alternate = self.infer_boolean_expression(expression.alternate, scope)
            if consequent is True and alternate is True:
                return True
            if consequent is False or alternate is False:
                return False
            return None

        return None

    @staticmethod
    def extract_declared_type_name(type_annotation: AST.TypeAnnotation | None) -> str | None:
        if type_annotation is None:
            return None
        return type_annotation.name

    def is_bool_type_annotation(self, type_annotation: AST.TypeAnnotation | None) -> bool:
        type_name = self.extract_declared_type_name(type_annotation)
        if type_name is None:
            return False
        return self.strip_array_suffix(type_name) == "bool"

    @staticmethod
    def is_na_literal(expression: AST.Expression) -> bool:
        return isinstance(expression, AST.Literal) and expression.value == "na"

    def describe_expression(self, expression: AST.Expression) -> str:
        if isinstance(expression, AST.Identifier):
            return expression.name
        if isinstance(expression, AST.Literal):
            return expression.raw
        if isinstance(expression, AST.MemberExpression):
            parent = self.describe_expression(expression.object)
            return f"{parent}.{expression.property.name}"
        if isinstance(expression, AST.CallExpression):
            function_name = self.extract_function_name(expression.callee)
            return function_name or "call"
        if isinstance(expression, AST.BinaryExpression):
            return expression.operator
        if isinstance(expression, AST.UnaryExpression):
            return expression.operator
        return "expression"

    def mutable_argument_error(self, name: str, line: int, column: int) -> None:
        self.errors.append(
            Diagnostic(
                line=line,
                column=column,
                length=len(name),
                message=f'Function arguments cannot be mutable ("{name}")',
                severity=Severity.ERROR,
                source="ast",
            )
        )

    def bool_na_error(self, line: int, column: int) -> None:
        self.errors.append(
            Diagnostic(
                line=line,
                column=column,
                length=2,
                message='Cannot assign "na" to a "bool" value in Pine Script v6',
                severity=Severity.ERROR,
                source="ast",
            )
        )

    def untyped_na_declaration_error(self, line: int, column: int, name: str) -> None:
        self.errors.append(
            Diagnostic(
                line=line,
                column=column,
                length=len(name),
                message="Value with NA type cannot be assigned to a variable that was defined without type keyword",
                severity=Severity.ERROR,
                source="ast",
            )
        )

    def lookup_accessible_symbol(
        self,
        scope: Scope,
        name: str,
        reference_line: int,
        reference_column: int | None = None,
    ) -> Symbol | None:
        current_scope: Scope | None = scope
        while current_scope is not None:
            symbol = current_scope.symbols.get(name)
            if symbol is not None:
                if symbol.kind in {"variable", "function", "type", "namespace"}:
                    declared_later_line = symbol.line > reference_line
                    declared_later_column = (
                        reference_column is not None
                        and symbol.line == reference_line
                        and symbol.column > reference_column
                    )
                    if declared_later_line or declared_later_column:
                        current_scope = current_scope.parent
                        continue
                return symbol
            current_scope = current_scope.parent
        return None

    def check_unused_variables(self, scope: Scope) -> None:
        for symbol in scope.all_symbols():
            # Match the extension more closely: loop iterators are scope helpers,
            # not regular variable declarations that should receive unused hints.
            if symbol.kind != "variable":
                continue
            if symbol.used or symbol.name.startswith("_"):
                continue
            self.errors.append(
                Diagnostic(
                    line=symbol.line,
                    column=symbol.column,
                    length=len(symbol.name),
                    message=f"Variable '{symbol.name}' is declared but never used.",
                    severity=Severity.HINT,
                    source="ast",
                )
            )

    def undefined_name(self, name: str, line: int, column: int) -> None:
        self.errors.append(
            Diagnostic(
                line=line,
                column=column,
                length=len(name),
                message=f"Undefined variable '{name}'",
                severity=Severity.ERROR,
                source="ast",
            )
        )
