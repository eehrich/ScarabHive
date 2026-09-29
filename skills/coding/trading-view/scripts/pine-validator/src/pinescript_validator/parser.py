from __future__ import annotations

from dataclasses import dataclass

from . import ast as AST
from .lexer import Lexer, Token, TokenType


TYPE_KEYWORDS = {
    "int",
    "float",
    "bool",
    "string",
    "color",
    "line",
    "label",
    "box",
    "table",
    "array",
    "matrix",
    "map",
    "polyline",
    "linefill",
}

TYPE_QUALIFIERS = {"series", "simple", "input", "const"}
DECLARATION_TYPE_START_KEYWORDS = TYPE_KEYWORDS | TYPE_QUALIFIERS
NON_NAME_KEYWORDS = {
    "if",
    "else",
    "for",
    "while",
    "switch",
    "return",
    "and",
    "or",
    "not",
    "break",
    "continue",
}


@dataclass(slots=True)
class ParserError:
    message: str
    line: int
    column: int
    token: Token | None = None


class Parser:
    def __init__(self, source: str) -> None:
        lexer = Lexer(source)
        tokens = lexer.tokenize()
        self.lexer_errors = lexer.get_errors()
        self.tokens = [
            token
            for token in tokens
            if token.type not in {TokenType.WHITESPACE, TokenType.COMMENT}
        ]
        self.current = 0
        self.errors: list[ParserError] = []
        self.allow_expression_newlines = True

    def get_errors(self) -> list[ParserError]:
        return self.errors

    def parse(self) -> AST.Program:
        body: list[AST.Statement] = []
        start_token = self.peek()
        while not self.is_at_end():
            try:
                stmt = self.statement()
                if stmt is not None:
                    body.append(stmt)
                    self.consume_statement_commas(body)
            except Exception as exc:  # noqa: BLE001
                self.report_error(str(exc))
                self.synchronize()
        return AST.Program(body=body, line=start_token.line, column=start_token.column)

    def report_error(self, message: str, token: Token | None = None) -> None:
        err_token = token or self.peek()
        self.errors.append(ParserError(message=message, line=err_token.line, column=err_token.column, token=err_token))

    def statement(self) -> AST.Statement | None:
        if self.check(TokenType.NEWLINE):
            self.advance()
            return None

        if self.check(TokenType.ANNOTATION):
            self.advance()
            return None

        if self.match((TokenType.KEYWORD, {"if"})):
            return self.if_statement(self.previous())

        if self.match((TokenType.KEYWORD, {"for"})):
            return self.for_statement(self.previous())

        if self.match((TokenType.KEYWORD, {"while"})):
            return self.while_statement(self.previous())

        if self.match((TokenType.KEYWORD, {"switch"})):
            return self.switch_statement(self.previous())

        if self.match((TokenType.KEYWORD, {"return"})):
            return self.return_statement(self.previous())

        if self.is_import_statement_start():
            self.advance()
            return self.import_statement(self.previous())

        if self.is_type_declaration_start():
            self.advance()
            return self.type_declaration(self.previous())

        if self.is_method_declaration_start():
            self.advance()
            return self.method_declaration(self.previous())

        var_start = self.peek()
        if self.match((TokenType.KEYWORD, {"var", "varip", "const"})):
            var_keyword = self.previous().value
            type_annotation = None
            checkpoint = self.current
            tentative_type = None
            try:
                tentative_type = self.parse_type_annotation(allow_qualifier=True, allow_identifier=True)
            except Exception:
                self.current = checkpoint
            if tentative_type is not None and self.check_name():
                type_annotation = tentative_type
            else:
                self.current = checkpoint
            return self.variable_declaration(var_keyword, var_start, type_annotation)

        if self.check((TokenType.KEYWORD, DECLARATION_TYPE_START_KEYWORDS)) or self.check(TokenType.IDENTIFIER):
            checkpoint = self.current
            start = self.peek()
            type_annotation = None
            try:
                type_annotation = self.parse_type_annotation(allow_qualifier=True, allow_identifier=True)
            except Exception:
                self.current = checkpoint
            if (
                type_annotation is not None
                and self.check_name()
                and self.peek_next()
                and self.peek_next().type == TokenType.ASSIGN
            ):
                return self.variable_declaration(None, start, type_annotation)
            self.current = checkpoint

        func_decl_checkpoint = self.current
        is_export = False
        if self.match((TokenType.KEYWORD, {"export"})):
            is_export = True

        if self.check_name():
            name_token = self.advance()
            if self.match(TokenType.LPAREN):
                params: list[AST.FunctionParam] | None = None
                try:
                    params = self.parse_function_params()
                    self.consume(TokenType.RPAREN, 'Expected ")" after function parameters')
                except Exception:
                    self.current = func_decl_checkpoint
                else:
                    if self.match(TokenType.ARROW):
                        start = self.tokens[func_decl_checkpoint]
                        return self.function_declaration(name_token, params or [], is_export, start)
                    self.current = func_decl_checkpoint
            else:
                self.current = func_decl_checkpoint
        else:
            self.current = func_decl_checkpoint

        next_token = self.peek_next()
        if self.check_name() and next_token and next_token.type in {TokenType.ASSIGN, TokenType.COMPOUND_ASSIGN}:
            name_token = self.peek()
            self.advance()
            assign_token = self.advance()
            if assign_token.type == TokenType.COMPOUND_ASSIGN:
                value = self.expression()
                return AST.CompoundAssignmentStatement(
                    name=name_token.value,
                    name_line=name_token.line,
                    name_column=name_token.column,
                    operator=assign_token.value,
                    value=value,
                    line=name_token.line,
                    column=name_token.column,
                )
            if assign_token.value == ":=":
                value = self.expression()
                return AST.AssignmentStatement(
                    name=name_token.value,
                    name_line=name_token.line,
                    name_column=name_token.column,
                    value=value,
                    line=name_token.line,
                    column=name_token.column,
                )
            self.current -= 2
            return self.variable_declaration(None, self.peek(), None)

        target_assignment = self.try_target_assignment_statement()
        if target_assignment is not None:
            return target_assignment

        if self.check(TokenType.LBRACKET):
            checkpoint = self.current
            try:
                start = self.peek()
                self.advance()
                names: list[str] = []
                variables: list[AST.DestructuringVariable] = []
                self.skip_expression_newlines()
                if not self.check(TokenType.RBRACKET):
                    while True:
                        self.skip_expression_newlines()
                        if self.check(TokenType.RBRACKET):
                            break
                        if not self.check_name():
                            raise ValueError("Expected identifier in destructuring pattern")
                        name_token = self.consume_name("Expected identifier in destructuring pattern")
                        names.append(name_token.value)
                        variables.append(AST.DestructuringVariable(name=name_token.value, line=name_token.line, column=name_token.column))
                        self.skip_expression_newlines()
                        if not self.match(TokenType.COMMA):
                            break
                        self.skip_expression_newlines()

                self.consume(TokenType.RBRACKET, 'Expected "]" in destructuring pattern')
                if self.check(TokenType.ASSIGN):
                    assign_token = self.advance()
                    if assign_token.value == "=":
                        return self.destructuring_assignment(names, variables, start)
                    self.report_error(
                        'Tuple destructuring uses "=" only. Reassignment with ":=" is not valid in Pine Script.',
                        assign_token,
                    )
                    self.expression()
                    return None
                self.current = checkpoint
            except Exception:
                self.current = checkpoint

        return self.expression_statement()

    def parse_type_annotation(self, *, allow_qualifier: bool, allow_identifier: bool = False) -> AST.TypeAnnotation | None:
        qualifier = None
        if allow_qualifier and self.check((TokenType.KEYWORD, TYPE_QUALIFIERS)):
            qualifier = self.advance().value

        type_name = None
        if self.check((TokenType.KEYWORD, TYPE_KEYWORDS)):
            type_name = self.advance().value
        elif allow_identifier and (self.check(TokenType.IDENTIFIER) or self.check(TokenType.KEYWORD)):
            type_name = self.advance().value

        if type_name is not None:
            while self.check(TokenType.DOT):
                checkpoint = self.current
                self.advance()
                if self.check_name():
                    type_name = f"{type_name}.{self.advance().value}"
                else:
                    self.current = checkpoint
                    break

            if self.check(TokenType.COMPARE) and self.peek().value == "<":
                type_name = f"{type_name}{self.consume_type_generic_suffix()}"

            if self.check(TokenType.LBRACKET) and self.peek_next() and self.peek_next().type == TokenType.RBRACKET:
                self.advance()
                self.advance()
                type_name = f"{type_name}[]"
            return AST.TypeAnnotation(name=type_name, qualifier=qualifier)
        if qualifier is not None:
            return AST.TypeAnnotation(name=qualifier)
        return None

    def variable_declaration(self, var_type: str | None, start_token: Token, type_annotation: AST.TypeAnnotation | None) -> AST.VariableDeclaration:
        name_token = self.consume_name("Expected variable name")
        init: AST.Expression | None = None
        if self.match(TokenType.ASSIGN):
            init = self.expression()
        return AST.VariableDeclaration(
            name=name_token.value,
            name_line=name_token.line,
            name_column=name_token.column,
            var_type=var_type,
            init=init,
            line=start_token.line,
            column=start_token.column,
            type_annotation=type_annotation,
        )

    def destructuring_assignment(self, names: list[str], variables: list[AST.DestructuringVariable], start_token: Token) -> AST.DestructuringAssignment:
        init = self.expression()
        return AST.DestructuringAssignment(names=names, variables=variables, init=init, line=start_token.line, column=start_token.column)

    def expression_statement(self) -> AST.ExpressionStatement:
        expr = self.expression()
        return AST.ExpressionStatement(expression=expr, line=expr.line, column=expr.column)

    def if_statement(self, start_token: Token) -> AST.IfStatement:
        condition = self.expression()
        consequent = self.parse_indented_block(start_token)
        alternate: list[AST.Statement] | None = None
        if self.match((TokenType.KEYWORD, {"else"})):
            else_token = self.previous()
            if self.match((TokenType.KEYWORD, {"if"})):
                alternate = [self.if_statement(self.previous())]
            else:
                alternate = self.parse_indented_block(else_token)
        return AST.IfStatement(condition=condition, consequent=consequent, alternate=alternate, line=start_token.line, column=start_token.column)

    def for_statement(self, start_token: Token) -> AST.ForStatement:
        iterator_name: str | None = None
        iterators: list[AST.DestructuringVariable] = []
        if self.match(TokenType.LBRACKET):
            self.skip_expression_newlines()
            if not self.check(TokenType.RBRACKET):
                while True:
                    self.skip_expression_newlines()
                    if self.check(TokenType.RBRACKET):
                        break
                    name_token = self.consume_name("Expected iterator variable")
                    if iterator_name is None:
                        iterator_name = name_token.value
                    iterators.append(AST.DestructuringVariable(name=name_token.value, line=name_token.line, column=name_token.column))
                    self.skip_expression_newlines()
                    if not self.match(TokenType.COMMA):
                        break
                    self.skip_expression_newlines()
            self.consume(TokenType.RBRACKET, 'Expected "]" after iterator destructuring')
        else:
            token = self.consume_name("Expected iterator variable")
            iterator_name = token.value
            iterators.append(AST.DestructuringVariable(name=token.value, line=token.line, column=token.column))

        if self.match((TokenType.KEYWORD, {"in"})):
            iterable = self.expression()
            body = self.parse_indented_block(start_token)
            return AST.ForStatement(
                iterator=iterator_name,
                iterators=iterators,
                iterable=iterable,
                body=body,
                line=start_token.line,
                column=start_token.column,
            )

        if len(iterators) != 1 or iterator_name is None:
            raise ValueError(f"Range-based for loop requires a single iterator at line {start_token.line}")
        self.consume(TokenType.ASSIGN, 'Expected "=" in for loop')
        from_expr = self.expression()
        self.match((TokenType.KEYWORD, {"to"}))
        to_expr = self.expression()
        step_expr: AST.Expression | None = None
        if (self.check(TokenType.IDENTIFIER) or self.check(TokenType.KEYWORD)) and self.peek().value == "by":
            self.advance()
            step_expr = self.expression()
        body = self.parse_indented_block(start_token)
        return AST.ForStatement(
            iterator=iterator_name,
            iterators=iterators,
            from_expr=from_expr,
            to_expr=to_expr,
            step_expr=step_expr,
            body=body,
            line=start_token.line,
            column=start_token.column,
        )

    def while_statement(self, start_token: Token) -> AST.WhileStatement:
        condition = self.expression()
        body = self.parse_indented_block(start_token)
        return AST.WhileStatement(condition=condition, body=body, line=start_token.line, column=start_token.column)

    def import_statement(self, start_token: Token) -> AST.ImportStatement:
        parts: list[str] = []
        alias: str | None = None
        while not self.is_at_end() and not self.check(TokenType.NEWLINE):
            if self.match((TokenType.KEYWORD, {"as"})):
                if self.check_name():
                    alias = self.advance().value
                continue
            parts.append(self.advance().value)
        module = "".join(parts).strip() or None
        if alias is None and module is not None:
            module_parts = [part for part in module.split("/") if part]
            for candidate in reversed(module_parts):
                if candidate.isdigit():
                    continue
                alias = candidate
                break
        return AST.ImportStatement(line=start_token.line, column=start_token.column, alias=alias, module=module)

    def type_declaration(self, start_token: Token) -> AST.TypeDeclaration:
        name_token = self.consume_name("Expected type name")
        fields: list[AST.TypeField] = []
        field_indent: int | None = None

        while not self.is_at_end():
            if self.check(TokenType.NEWLINE):
                self.advance()
                continue

            current = self.peek()
            current_indent = current.indent or 0
            start_indent = start_token.indent or 0

            if current.line <= start_token.line:
                break

            if field_indent is None:
                field_indent = current_indent
                if field_indent <= start_indent:
                    break

            if current_indent < field_indent:
                break

            checkpoint = self.current
            type_annotation = self.parse_type_annotation(allow_qualifier=False, allow_identifier=True)
            if type_annotation is not None and self.check_name():
                field_name = self.consume_name("Expected field name")
            else:
                self.current = checkpoint
                type_annotation = None
                if not self.check_name():
                    break
                field_name = self.consume_name("Expected field name")

            default_value: AST.Expression | None = None
            if self.match(TokenType.ASSIGN):
                default_value = self.expression()
            fields.append(
                AST.TypeField(
                    name=field_name.value,
                    line=field_name.line,
                    column=field_name.column,
                    type_annotation=type_annotation,
                    default_value=default_value,
                )
            )

        return AST.TypeDeclaration(name=name_token.value, fields=fields, line=start_token.line, column=start_token.column)

    def method_declaration(self, start_token: Token) -> AST.FunctionDeclaration:
        name_token = self.consume_name("Expected method name")
        self.consume(TokenType.LPAREN, 'Expected "(" after method name')
        params = self.parse_function_params()
        self.consume(TokenType.RPAREN, 'Expected ")" after method parameters')
        self.consume(TokenType.ARROW, 'Expected "=>" after method declaration')
        return self.function_declaration(name_token, params, False, start_token)

    def switch_statement(self, start_token: Token) -> AST.SwitchStatement:
        expression = self.parse_switch_selector(start_token)
        cases: list[AST.SwitchCase] = []
        case_indent: int | None = None

        while not self.is_at_end():
            if self.check(TokenType.NEWLINE):
                self.advance()
                continue

            current = self.peek()
            current_indent = current.indent or 0

            if current.line > start_token.line and case_indent is None:
                case_indent = current_indent

            if case_indent is not None and current.line > start_token.line and current_indent < case_indent:
                break

            condition: AST.Expression | None = None
            if self.match(TokenType.ARROW):
                arrow_token = self.previous()
            else:
                condition = self.expression()
                arrow_token = self.consume(TokenType.ARROW, 'Expected "=>" in switch case')

            body = self.parse_switch_case_body(arrow_token)
            cases.append(AST.SwitchCase(condition=condition, body=body, line=arrow_token.line, column=arrow_token.column))

        return AST.SwitchStatement(expression=expression, cases=cases, line=start_token.line, column=start_token.column)

    def parse_switch_selector(self, start_token: Token) -> AST.Expression | None:
        if self.is_at_end() or self.check(TokenType.NEWLINE):
            return None
        if self.peek().line != start_token.line:
            return None
        return self.expression()

    def switch_expression(self, start_token: Token) -> AST.SwitchExpression:
        expression = self.parse_switch_selector(start_token)
        cases: list[AST.SwitchExpressionCase] = []
        case_indent: int | None = None

        while not self.is_at_end():
            if self.check(TokenType.NEWLINE):
                self.advance()
                continue

            current = self.peek()
            current_indent = current.indent or 0

            if current.line > start_token.line and case_indent is None:
                case_indent = current_indent

            if case_indent is not None and current.line > start_token.line and current_indent < case_indent:
                break

            condition: AST.Expression | None = None
            if self.match(TokenType.ARROW):
                arrow_token = self.previous()
            else:
                condition = self.expression()
                arrow_token = self.consume(TokenType.ARROW, 'Expected "=>" in switch case')

            if not self.check(TokenType.NEWLINE) and self.peek().line == arrow_token.line:
                value = self.expression()
                body: list[AST.Statement] = []
            else:
                block = self.parse_indented_block(arrow_token, require_greater_indent=True)
                value, body = self.extract_switch_expression_value(block, arrow_token)

            cases.append(AST.SwitchExpressionCase(condition=condition, value=value, body=body, line=arrow_token.line, column=arrow_token.column))

        if not cases:
            raise ValueError(f"Expected switch cases at line {start_token.line}")

        return AST.SwitchExpression(expression=expression, cases=cases, line=start_token.line, column=start_token.column)

    def extract_switch_expression_value(self, block: list[AST.Statement], arrow_token: Token) -> tuple[AST.Expression, list[AST.Statement]]:
        if block:
            last_statement = block[-1]
            if isinstance(last_statement, AST.ExpressionStatement):
                return last_statement.expression, block[:-1]
            if isinstance(last_statement, AST.ReturnStatement):
                return last_statement.value, block[:-1]
        value = AST.Literal(value="na", raw="na", line=arrow_token.line, column=arrow_token.column)
        return value, block

    def parse_switch_case_body(self, arrow_token: Token) -> list[AST.Statement]:
        if not self.check(TokenType.NEWLINE) and self.peek().line == arrow_token.line:
            checkpoint = self.current
            stmt = self.statement()
            if stmt is not None:
                return [stmt]
            self.current = checkpoint
            expr = self.expression()
            return [AST.ExpressionStatement(expression=expr, line=expr.line, column=expr.column)]
        return self.parse_indented_block(arrow_token, require_greater_indent=True)

    def if_expression(self, start_token: Token) -> AST.IfExpression:
        condition = self.expression_without_newlines()
        consequent = self.expression()
        self.skip_expression_newlines()
        self.consume(TokenType.KEYWORD, 'Expected "else" in if expression')
        if self.previous().value != "else":
            raise ValueError(f'Expected "else" in if expression at line {self.previous().line}')
        if self.match((TokenType.KEYWORD, {"if"})):
            alternate = self.if_expression(self.previous())
        else:
            alternate = self.expression()
        return AST.IfExpression(condition=condition, consequent=consequent, alternate=alternate, line=start_token.line, column=start_token.column)

    def return_statement(self, start_token: Token) -> AST.ReturnStatement:
        value = self.expression()
        return AST.ReturnStatement(value=value, line=start_token.line, column=start_token.column)

    def function_declaration(self, name_token: Token, params: list[AST.FunctionParam], is_export: bool, start_token: Token) -> AST.FunctionDeclaration:
        body: list[AST.Statement] = []
        next_token = self.peek()
        if next_token.type != TokenType.NEWLINE and next_token.line == start_token.line:
            expr = self.expression()
            body.append(AST.ReturnStatement(value=expr, line=expr.line, column=expr.column))
        else:
            body.extend(self.parse_indented_block(start_token))
        return AST.FunctionDeclaration(name=name_token.value, params=params, body=body, line=name_token.line, column=name_token.column, is_export=is_export)

    def parse_function_params(self) -> list[AST.FunctionParam]:
        params: list[AST.FunctionParam] = []
        self.skip_expression_newlines()
        if self.check(TokenType.RPAREN):
            return params
        while True:
            self.skip_expression_newlines()
            checkpoint = self.current
            type_annotation = self.parse_type_annotation(allow_qualifier=True, allow_identifier=True)
            if type_annotation is not None and self.check_name():
                param_name = self.consume_name("Expected parameter name")
            else:
                self.current = checkpoint
                type_annotation = None
                param_name = self.consume_name("Expected parameter name")
            default_value = None
            if self.match(TokenType.ASSIGN):
                default_value = self.expression()
            params.append(AST.FunctionParam(name=param_name.value, line=param_name.line, column=param_name.column, type_annotation=type_annotation, default_value=default_value))
            self.skip_expression_newlines()
            if not self.match(TokenType.COMMA):
                break
        return params

    def expression(self) -> AST.Expression:
        self.skip_expression_newlines()
        return self.ternary()

    def ternary(self) -> AST.Expression:
        self.skip_expression_newlines()
        expr = self.logical_or()
        self.skip_expression_newlines()
        if self.match(TokenType.TERNARY):
            consequent = self.expression()
            self.skip_expression_newlines()
            self.consume(TokenType.COLON, 'Expected ":" in ternary expression')
            alternate = self.expression()
            return AST.TernaryExpression(condition=expr, consequent=consequent, alternate=alternate, line=expr.line, column=expr.column)
        return expr

    def logical_or(self) -> AST.Expression:
        self.skip_expression_newlines()
        expr = self.logical_and()
        self.skip_expression_newlines()
        while self.match((TokenType.KEYWORD, {"or"})):
            operator = self.previous().value
            right = self.logical_and()
            expr = AST.BinaryExpression(operator=operator, left=expr, right=right, line=expr.line, column=expr.column)
            self.skip_expression_newlines()
        return expr

    def logical_and(self) -> AST.Expression:
        self.skip_expression_newlines()
        expr = self.comparison()
        self.skip_expression_newlines()
        while self.match((TokenType.KEYWORD, {"and"})):
            operator = self.previous().value
            right = self.comparison()
            expr = AST.BinaryExpression(operator=operator, left=expr, right=right, line=expr.line, column=expr.column)
            self.skip_expression_newlines()
        return expr

    def comparison(self) -> AST.Expression:
        self.skip_expression_newlines()
        expr = self.addition()
        self.skip_expression_newlines()
        while self.match(TokenType.COMPARE):
            operator = self.previous().value
            right = self.addition()
            expr = AST.BinaryExpression(operator=operator, left=expr, right=right, line=expr.line, column=expr.column)
            self.skip_expression_newlines()
        return expr

    def addition(self) -> AST.Expression:
        self.skip_expression_newlines()
        expr = self.multiplication()
        self.skip_expression_newlines()
        while self.match(TokenType.PLUS, TokenType.MINUS):
            operator = self.previous().value
            right = self.multiplication()
            expr = AST.BinaryExpression(operator=operator, left=expr, right=right, line=expr.line, column=expr.column)
            self.skip_expression_newlines()
        return expr

    def multiplication(self) -> AST.Expression:
        self.skip_expression_newlines()
        expr = self.unary()
        self.skip_expression_newlines()
        while self.match(TokenType.MULTIPLY, TokenType.DIVIDE, TokenType.MODULO):
            operator = self.previous().value
            right = self.unary()
            expr = AST.BinaryExpression(operator=operator, left=expr, right=right, line=expr.line, column=expr.column)
            self.skip_expression_newlines()
        return expr

    def unary(self) -> AST.Expression:
        self.skip_expression_newlines()
        if self.match(TokenType.MINUS) or self.match(TokenType.PLUS) or self.match((TokenType.KEYWORD, {"not"})):
            operator = self.previous()
            right = self.unary()
            return AST.UnaryExpression(operator=operator.value, argument=right, line=operator.line, column=operator.column)
        return self.postfix()

    def postfix(self) -> AST.Expression:
        self.skip_expression_newlines()
        expr = self.primary()
        while True:
            if self.peek().type in {TokenType.LPAREN, TokenType.DOT, TokenType.LBRACKET} and self.previous().type == TokenType.NEWLINE:
                break
            if self.is_generic_call_suffix():
                self.consume_generic_call_suffix()
                continue
            if self.match(TokenType.LPAREN):
                expr = self.finish_call(expr)
                continue
            if self.match(TokenType.DOT):
                if self.check_name():
                    property_token = self.advance()
                    prop = AST.Identifier(name=property_token.value, line=property_token.line, column=property_token.column)
                    expr = AST.MemberExpression(object=expr, property=prop, line=expr.line, column=expr.column)
                    continue
                raise ValueError(f"Expected property name at line {self.peek().line}")
            if self.check(TokenType.LBRACKET):
                self.advance()
                index = self.expression()
                self.consume(TokenType.RBRACKET, 'Expected "]" after index expression')
                expr = AST.IndexExpression(object=expr, index=index, line=expr.line, column=expr.column)
                continue
            break
        return expr

    def finish_call(self, callee: AST.Expression) -> AST.CallExpression:
        args: list[AST.CallArgument] = []
        self.skip_continuation_newlines()
        if not self.check(TokenType.RPAREN):
            while True:
                self.skip_continuation_newlines()
                if self.check_name() and self.peek_next() and self.peek_next().type == TokenType.ASSIGN:
                    name = self.advance().value
                    self.advance()
                    value = self.expression()
                    args.append(AST.CallArgument(name=name, value=value))
                else:
                    args.append(AST.CallArgument(value=self.expression()))
                self.skip_continuation_newlines()
                if not self.match(TokenType.COMMA):
                    break
        self.skip_continuation_newlines()
        self.consume(TokenType.RPAREN, 'Expected ")" after arguments')
        return AST.CallExpression(callee=callee, arguments=args, line=callee.line, column=callee.column)

    def primary(self) -> AST.Expression:
        self.skip_expression_newlines()
        if self.match(TokenType.NUMBER):
            token = self.previous()
            return AST.Literal(value=float(token.value), raw=token.value, line=token.line, column=token.column)

        if self.match(TokenType.STRING):
            token = self.previous()
            return AST.Literal(value=token.value, raw=token.value, line=token.line, column=token.column)

        if self.match(TokenType.BOOL):
            token = self.previous()
            return AST.Literal(value=token.value == "true", raw=token.value, line=token.line, column=token.column)

        if self.match(TokenType.COLOR):
            token = self.previous()
            return AST.Literal(value=token.value, raw=token.value, line=token.line, column=token.column)

        if self.match((TokenType.KEYWORD, {"na"})):
            token = self.previous()
            return AST.Literal(value="na", raw="na", line=token.line, column=token.column)

        if self.match((TokenType.KEYWORD, {"if"})):
            return self.if_expression(self.previous())

        if self.match((TokenType.KEYWORD, {"switch"})):
            return self.switch_expression(self.previous())

        if self.match(TokenType.IDENTIFIER) or self.match(TokenType.KEYWORD):
            token = self.previous()
            return AST.Identifier(name=token.value, line=token.line, column=token.column)

        if self.match(TokenType.LPAREN):
            expr = self.expression()
            self.consume(TokenType.RPAREN, 'Expected ")" after expression')
            return expr

        if self.match(TokenType.LBRACKET):
            start_token = self.previous()
            elements: list[AST.Expression] = []
            if not self.check(TokenType.RBRACKET):
                while True:
                    elements.append(self.expression())
                    if not self.match(TokenType.COMMA):
                        break
            self.consume(TokenType.RBRACKET, 'Expected "]" after array literal')
            return AST.ArrayExpression(elements=elements, line=start_token.line, column=start_token.column)

        raise ValueError(f"Unexpected token: {self.peek().value}")

    def parse_indented_block(self, start_token: Token, *, require_greater_indent: bool = False) -> list[AST.Statement]:
        body: list[AST.Statement] = []
        block_indent: int | None = None
        start_indent = start_token.indent or 0
        while not self.is_at_end():
            current_token = self.peek()
            current_indent = current_token.indent or 0
            if current_token.type == TokenType.NEWLINE:
                self.advance()
                continue
            if block_indent is None and current_token.line > start_token.line:
                block_indent = current_indent
                if require_greater_indent and block_indent <= start_indent:
                    break
            if block_indent is not None and current_token.line > start_token.line and current_indent < block_indent:
                break
            if self.check((TokenType.KEYWORD, {"else"})):
                break
            stmt = self.statement()
            if stmt is not None:
                body.append(stmt)
                self.consume_statement_commas(body)
            else:
                break
        return body

    def consume_statement_commas(self, output: list[AST.Statement]) -> None:
        while self.match(TokenType.COMMA):
            if self.check(TokenType.NEWLINE):
                break
            stmt = self.statement()
            if stmt is None:
                break
            output.append(stmt)

    def skip_continuation_newlines(self) -> None:
        while self.check(TokenType.NEWLINE) and not self.is_at_end():
            self.advance()

    def skip_expression_newlines(self) -> None:
        if not self.allow_expression_newlines:
            return
        while self.check(TokenType.NEWLINE) and not self.is_at_end():
            self.advance()

    def expression_without_newlines(self) -> AST.Expression:
        allow_newlines = self.allow_expression_newlines
        self.allow_expression_newlines = False
        try:
            return self.expression()
        finally:
            self.allow_expression_newlines = allow_newlines

    def is_generic_call_suffix(self) -> bool:
        if not self.check(TokenType.COMPARE) or self.peek().value != "<":
            return False
        index = self.current
        depth = 0
        while index < len(self.tokens):
            token = self.tokens[index]
            if token.type == TokenType.COMPARE and token.value == "<":
                depth += 1
            elif token.type == TokenType.COMPARE and token.value == ">":
                depth -= 1
                if depth == 0:
                    index += 1
                    break
            else:
                if depth > 0 and token.type == TokenType.NEWLINE:
                    return False
            index += 1
        if depth != 0:
            return False
        if index >= len(self.tokens):
            return False
        return self.tokens[index].type == TokenType.LPAREN

    def consume_generic_call_suffix(self) -> None:
        depth = 0
        while not self.is_at_end():
            token = self.peek()
            if token.type == TokenType.COMPARE and token.value == "<":
                depth += 1
                self.advance()
                continue
            if token.type == TokenType.COMPARE and token.value == ">":
                depth -= 1
                self.advance()
                if depth == 0:
                    return
                continue
            self.advance()

    def consume_type_generic_suffix(self) -> str:
        if not (self.check(TokenType.COMPARE) and self.peek().value == "<"):
            return ""
        depth = 0
        parts: list[str] = []
        while not self.is_at_end():
            token = self.peek()
            if token.type == TokenType.NEWLINE:
                break
            if token.type == TokenType.COMPARE and token.value == "<":
                depth += 1
            elif token.type == TokenType.COMPARE and token.value == ">":
                depth -= 1
            parts.append(token.value)
            self.advance()
            if depth == 0 and token.type == TokenType.COMPARE and token.value == ">":
                return "".join(parts)
        raise ValueError(f"Unterminated generic type annotation at line {self.peek().line}")

    def try_target_assignment_statement(self) -> AST.TargetAssignmentStatement | None:
        checkpoint = self.current
        try:
            target = self.parse_assignment_target()
            if target is None:
                self.current = checkpoint
                return None
            if not (self.check(TokenType.ASSIGN) or self.check(TokenType.COMPOUND_ASSIGN)):
                self.current = checkpoint
                return None
            assign_token = self.advance()
            if isinstance(target, AST.Identifier) and assign_token.value == "=":
                self.current = checkpoint
                return None
            value = self.expression()
            return AST.TargetAssignmentStatement(
                target=target,
                operator=assign_token.value,
                value=value,
                line=assign_token.line,
                column=assign_token.column,
            )
        except Exception:
            self.current = checkpoint
            return None

    def parse_assignment_target(self) -> AST.Expression | None:
        if not self.check_name():
            return None
        name_token = self.advance()
        target: AST.Expression = AST.Identifier(name=name_token.value, line=name_token.line, column=name_token.column)
        while True:
            if self.match(TokenType.DOT):
                if not (self.check_name()):
                    raise ValueError(f"Expected property name at line {self.peek().line}")
                property_token = self.advance()
                prop = AST.Identifier(name=property_token.value, line=property_token.line, column=property_token.column)
                target = AST.MemberExpression(object=target, property=prop, line=target.line, column=target.column)
                continue
            if self.check(TokenType.LBRACKET):
                self.advance()
                index = self.expression()
                self.consume(TokenType.RBRACKET, 'Expected "]" after index expression')
                target = AST.IndexExpression(object=target, index=index, line=target.line, column=target.column)
                continue
            break
        return target

    def find_next_non_newline_index(self) -> int:
        index = self.current + 1
        while index < len(self.tokens):
            if self.tokens[index].type != TokenType.NEWLINE:
                return index
            index += 1
        return -1

    def peek_n(self, offset: int) -> Token | None:
        index = self.current + offset
        if index < 0 or index >= len(self.tokens):
            return None
        return self.tokens[index]

    def is_import_statement_start(self) -> bool:
        token = self.peek()
        return token.type == TokenType.KEYWORD and token.value == "import"

    def is_type_declaration_start(self) -> bool:
        token = self.peek()
        if token.type != TokenType.KEYWORD or token.value not in {"type", "enum"}:
            return False
        name_token = self.peek_n(1)
        if name_token is None or not self.is_name_token(name_token):
            return False
        next_token = self.peek_n(2)
        if next_token is None:
            return True
        return next_token.type == TokenType.NEWLINE

    def is_method_declaration_start(self) -> bool:
        token = self.peek()
        if token.type != TokenType.KEYWORD or token.value != "method":
            return False
        name_token = self.peek_n(1)
        if name_token is None or not self.is_name_token(name_token):
            return False
        lparen_token = self.peek_n(2)
        return lparen_token is not None and lparen_token.type == TokenType.LPAREN

    def is_name_token(self, token: Token) -> bool:
        if token.type == TokenType.IDENTIFIER:
            return True
        if token.type == TokenType.KEYWORD and token.value not in NON_NAME_KEYWORDS:
            return True
        return False

    def check_name(self) -> bool:
        if self.is_at_end():
            return False
        return self.is_name_token(self.peek())

    def consume_name(self, message: str) -> Token:
        if self.check_name():
            return self.advance()
        raise ValueError(f"{message} at line {self.peek().line}")

    def match(self, *types: TokenType | tuple[TokenType, set[str]]) -> bool:
        for token_type in types:
            if self.check(token_type):
                self.advance()
                return True
        return False

    def check(self, token_type: TokenType | tuple[TokenType, set[str]]) -> bool:
        if self.is_at_end():
            return False
        if isinstance(token_type, tuple):
            kind, values = token_type
            token = self.peek()
            return token.type == kind and token.value in values
        return self.peek().type == token_type

    def advance(self) -> Token:
        if not self.is_at_end():
            self.current += 1
        return self.previous()

    def is_at_end(self) -> bool:
        return self.peek().type == TokenType.EOF

    def peek(self) -> Token:
        return self.tokens[self.current]

    def peek_next(self) -> Token | None:
        if self.current + 1 >= len(self.tokens):
            return None
        return self.tokens[self.current + 1]

    def previous(self) -> Token:
        return self.tokens[self.current - 1]

    def consume(self, token_type: TokenType, message: str) -> Token:
        if self.check(token_type):
            return self.advance()
        raise ValueError(f"{message} at line {self.peek().line}")

    def synchronize(self) -> None:
        if not self.is_at_end():
            self.advance()
        while not self.is_at_end():
            if self.previous().type == TokenType.NEWLINE:
                return
            if self.peek().type == TokenType.KEYWORD and self.peek().value in {"if", "for", "while", "switch", "type", "enum", "method", "import", "var", "varip", "const"}:
                return
            self.advance()
