from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .data_loader import KEYWORDS


class TokenType(str, Enum):
    NUMBER = "NUMBER"
    STRING = "STRING"
    BOOL = "BOOL"
    COLOR = "COLOR"
    IDENTIFIER = "IDENTIFIER"
    KEYWORD = "KEYWORD"
    ASSIGN = "ASSIGN"
    COMPOUND_ASSIGN = "COMPOUND_ASSIGN"
    PLUS = "PLUS"
    MINUS = "MINUS"
    MULTIPLY = "MULTIPLY"
    DIVIDE = "DIVIDE"
    MODULO = "MODULO"
    COMPARE = "COMPARE"
    TERNARY = "TERNARY"
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    LBRACKET = "LBRACKET"
    RBRACKET = "RBRACKET"
    COMMA = "COMMA"
    DOT = "DOT"
    COLON = "COLON"
    ARROW = "ARROW"
    NEWLINE = "NEWLINE"
    COMMENT = "COMMENT"
    ANNOTATION = "ANNOTATION"
    EOF = "EOF"
    WHITESPACE = "WHITESPACE"
    ERROR = "ERROR"


@dataclass(slots=True)
class Token:
    type: TokenType
    value: str
    line: int
    column: int
    length: int
    indent: int = 0
    end_line: int | None = None
    end_column: int | None = None


@dataclass(slots=True)
class LexerError:
    message: str
    line: int
    column: int
    length: int


SINGLE_CHAR_TOKENS = {
    "(": TokenType.LPAREN,
    ")": TokenType.RPAREN,
    "[": TokenType.LBRACKET,
    "]": TokenType.RBRACKET,
    ",": TokenType.COMMA,
    ".": TokenType.DOT,
    "?": TokenType.TERNARY,
}


class Lexer:
    def __init__(self, source: str, *, tab_width: int = 4) -> None:
        self.source = source
        self.pos = 0
        self.line = 1
        self.column = 1
        self.tokens: list[Token] = []
        self.errors: list[LexerError] = []
        self.current_indent = 0
        self.at_line_start = True
        self.tab_width = tab_width

    def tokenize(self) -> list[Token]:
        while self.pos < len(self.source):
            self._scan_token()
        self._add_token(TokenType.EOF, "", 0)
        return self.tokens

    def get_errors(self) -> list[LexerError]:
        return self.errors

    def _report_error(self, message: str, length: int = 1) -> None:
        self.errors.append(
            LexerError(
                message=message,
                line=self.line,
                column=max(1, self.column - length),
                length=length,
            )
        )

    def _scan_token(self) -> None:
        char = self._advance()

        if char == "." and self._is_digit(self._peek()):
            self._scan_number(started_with_dot=True)
            return

        single_char = SINGLE_CHAR_TOKENS.get(char)
        if single_char is not None:
            self._add_token(single_char, char, 1)
            return

        if char in {" ", "\t"}:
            if self.at_line_start:
                self.current_indent += self.tab_width if char == "\t" else 1
            return

        if char == "\r":
            return

        if char == "\n":
            self._add_token(TokenType.NEWLINE, "\n", 1)
            self.line += 1
            self.column = 1
            self.at_line_start = True
            self.current_indent = 0
            return

        if char == "/":
            if self._peek() == "/":
                self._scan_comment()
            elif self._peek() == "*":
                self._scan_block_comment()
            elif self._peek() == "=":
                self._advance()
                self._add_token(TokenType.COMPOUND_ASSIGN, "/=", 2)
            else:
                self._add_token(TokenType.DIVIDE, "/", 1)
            return

        if char == "+":
            if self._peek() == "=":
                self._advance()
                self._add_token(TokenType.COMPOUND_ASSIGN, "+=", 2)
            else:
                self._add_token(TokenType.PLUS, "+", 1)
            return

        if char == "-":
            if self._peek() == "=":
                self._advance()
                self._add_token(TokenType.COMPOUND_ASSIGN, "-=", 2)
            else:
                self._add_token(TokenType.MINUS, "-", 1)
            return

        if char == "*":
            if self._peek() == "=":
                self._advance()
                self._add_token(TokenType.COMPOUND_ASSIGN, "*=", 2)
            else:
                self._add_token(TokenType.MULTIPLY, "*", 1)
            return

        if char == "%":
            if self._peek() == "=":
                self._advance()
                self._add_token(TokenType.COMPOUND_ASSIGN, "%=", 2)
            else:
                self._add_token(TokenType.MODULO, "%", 1)
            return

        if char == ":":
            if self._peek() == "=":
                self._advance()
                self._add_token(TokenType.ASSIGN, ":=", 2)
            else:
                self._add_token(TokenType.COLON, ":", 1)
            return

        if char == "=":
            if self._peek() == "=":
                self._advance()
                self._add_token(TokenType.COMPARE, "==", 2)
            elif self._peek() == ">":
                self._advance()
                self._add_token(TokenType.ARROW, "=>", 2)
            else:
                self._add_token(TokenType.ASSIGN, "=", 1)
            return

        if char == "!":
            if self._peek() == "=":
                self._advance()
                self._add_token(TokenType.COMPARE, "!=", 2)
            else:
                self._report_error("Unexpected character '!'. Pine Script uses 'not' instead.", 1)
                self._add_token(TokenType.ERROR, "!", 1)
            return

        if char == "<":
            if self._peek() == "=":
                self._advance()
                self._add_token(TokenType.COMPARE, "<=", 2)
            else:
                self._add_token(TokenType.COMPARE, "<", 1)
            return

        if char == ">":
            if self._peek() == "=":
                self._advance()
                self._add_token(TokenType.COMPARE, ">=", 2)
            else:
                self._add_token(TokenType.COMPARE, ">", 1)
            return

        if char == "#":
            self._scan_hex_color()
            return

        if char in {'"', "'"}:
            self._scan_string(char)
            return

        if self._is_digit(char):
            self._scan_number()
            return

        if self._is_alpha(char):
            self._scan_identifier()
            return

        self._report_error(f"Unexpected character '{char}'", 1)
        self._add_token(TokenType.ERROR, char, 1)

    def _scan_comment(self) -> None:
        start = self.pos - 1
        self._advance()
        if self._peek() == "@":
            while self._peek() != "\n" and not self._is_at_end():
                self._advance()
            value = self.source[start:self.pos]
            self._add_token(TokenType.ANNOTATION, value, len(value))
            return

        while self._peek() != "\n" and not self._is_at_end():
            self._advance()
        value = self.source[start:self.pos]
        self._add_token(TokenType.COMMENT, value, len(value))

    def _scan_block_comment(self) -> None:
        start = self.pos - 1
        start_line = self.line
        start_column = self.column - 1
        terminated = False
        self._advance()
        while not self._is_at_end():
            if self._peek() == "*" and self._peek_next() == "/":
                self._advance()
                self._advance()
                terminated = True
                break
            if self._peek() == "\n":
                self.line += 1
                self.column = 1
            self._advance()
        self.errors.append(
            LexerError(
                message="Pine Script does not support multiline comments. Use '//' comments instead.",
                line=start_line,
                column=start_column,
                length=2,
            )
        )
        if not terminated:
            self.errors.append(
                LexerError(
                    message="Unterminated multiline comment",
                    line=start_line,
                    column=start_column,
                    length=max(2, self.pos - start),
                )
            )
        value = self.source[start:self.pos]
        self._add_token_with_position(
            TokenType.COMMENT,
            value,
            start_line,
            start_column,
            self.line,
            self.column,
        )

    def _scan_string(self, quote: str) -> None:
        start = self.pos - 1
        start_line = self.line
        start_column = self.column - 1
        terminated = False
        while not self._is_at_end():
            char = self._peek()
            if char == "\\":
                self._advance()
                if not self._is_at_end():
                    self._advance()
            elif char == quote:
                terminated = True
                break
            else:
                if char == "\n":
                    self.line += 1
                    self.column = 1
                self._advance()
        if terminated:
            self._advance()
        else:
            self._report_error("Unterminated string literal", max(1, self.pos - start))
        value = self.source[start:self.pos]
        self._add_token_with_position(
            TokenType.STRING,
            value,
            start_line,
            start_column,
            self.line,
            self.column,
        )

    def _scan_number(self, *, started_with_dot: bool = False) -> None:
        start = self.pos - 1
        while self._is_digit(self._peek()):
            self._advance()
        if not started_with_dot and self._peek() == ".":
            self._advance()
            while self._is_digit(self._peek()):
                self._advance()
        if self._peek() in {"e", "E"}:
            self._advance()
            if self._peek() in {"+", "-"}:
                self._advance()
            while self._is_digit(self._peek()):
                self._advance()
        value = self.source[start:self.pos]
        self._add_token(TokenType.NUMBER, value, len(value))

    def _scan_hex_color(self) -> None:
        start = self.pos - 1
        hex_count = 0
        while self._is_hex_digit(self._peek()) and hex_count < 8:
            self._advance()
            hex_count += 1
        if hex_count in {6, 8}:
            value = self.source[start:self.pos]
            self._add_token(TokenType.COLOR, value, len(value))
            return
        while self._is_alpha_numeric(self._peek()):
            self._advance()
        value = self.source[start:self.pos]
        self._add_token(TokenType.IDENTIFIER, value, len(value))

    def _scan_identifier(self) -> None:
        start = self.pos - 1
        while self._is_alpha_numeric(self._peek()) or self._peek() == "_":
            self._advance()
        value = self.source[start:self.pos]
        if value in {"true", "false"}:
            token_type = TokenType.BOOL
        elif value in KEYWORDS:
            token_type = TokenType.KEYWORD
        else:
            token_type = TokenType.IDENTIFIER
        self._add_token(token_type, value, len(value))

    def _advance(self) -> str:
        char = self.source[self.pos]
        self.pos += 1
        self.column += 1
        return char

    def _peek(self) -> str:
        if self._is_at_end():
            return "\0"
        return self.source[self.pos]

    def _peek_next(self) -> str:
        if self.pos + 1 >= len(self.source):
            return "\0"
        return self.source[self.pos + 1]

    def _is_at_end(self) -> bool:
        return self.pos >= len(self.source)

    @staticmethod
    def _is_digit(char: str) -> bool:
        return "0" <= char <= "9"

    @staticmethod
    def _is_hex_digit(char: str) -> bool:
        return ("0" <= char <= "9") or ("a" <= char <= "f") or ("A" <= char <= "F")

    @staticmethod
    def _is_alpha(char: str) -> bool:
        return ("a" <= char <= "z") or ("A" <= char <= "Z") or char == "_"

    def _is_alpha_numeric(self, char: str) -> bool:
        return self._is_alpha(char) or self._is_digit(char)

    def _add_token(self, token_type: TokenType, value: str, length: int) -> None:
        if token_type not in {TokenType.NEWLINE, TokenType.WHITESPACE} and self.at_line_start:
            self.at_line_start = False
        start_column = self.column - length
        self.tokens.append(
            Token(
                type=token_type,
                value=value,
                line=self.line,
                column=start_column,
                length=length,
                indent=self.current_indent,
                end_line=self.line,
                end_column=self.column,
            )
        )

    def _add_token_with_position(
        self,
        token_type: TokenType,
        value: str,
        start_line: int,
        start_column: int,
        end_line: int,
        end_column: int,
    ) -> None:
        if token_type not in {TokenType.NEWLINE, TokenType.WHITESPACE} and self.at_line_start:
            self.at_line_start = False
        self.tokens.append(
            Token(
                type=token_type,
                value=value,
                line=start_line,
                column=start_column,
                length=len(value),
                indent=self.current_indent,
                end_line=end_line,
                end_column=end_column,
            )
        )
