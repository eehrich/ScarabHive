from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TypeAnnotation:
    name: str
    qualifier: str | None = None


@dataclass(slots=True)
class DestructuringVariable:
    name: str
    line: int
    column: int


@dataclass(slots=True)
class CallArgument:
    value: "Expression"
    name: str | None = None


@dataclass(slots=True)
class FunctionParam:
    name: str
    line: int
    column: int
    type_annotation: TypeAnnotation | None = None
    default_value: "Expression | None" = None


@dataclass(slots=True)
class Program:
    body: list["Statement"] = field(default_factory=list)
    line: int = 1
    column: int = 1


@dataclass(slots=True)
class VariableDeclaration:
    name: str
    name_line: int
    name_column: int
    var_type: str | None
    init: "Expression | None"
    line: int
    column: int
    type_annotation: TypeAnnotation | None = None


@dataclass(slots=True)
class DestructuringAssignment:
    names: list[str]
    variables: list[DestructuringVariable]
    init: "Expression"
    line: int
    column: int


@dataclass(slots=True)
class AssignmentStatement:
    name: str
    name_line: int
    name_column: int
    value: "Expression"
    line: int
    column: int


@dataclass(slots=True)
class CompoundAssignmentStatement:
    name: str
    name_line: int
    name_column: int
    operator: str
    value: "Expression"
    line: int
    column: int


@dataclass(slots=True)
class TargetAssignmentStatement:
    target: "Expression"
    operator: str
    value: "Expression"
    line: int
    column: int


@dataclass(slots=True)
class FunctionDeclaration:
    name: str
    params: list[FunctionParam]
    body: list["Statement"]
    line: int
    column: int
    return_type: TypeAnnotation | None = None
    is_export: bool = False


@dataclass(slots=True)
class TypeField:
    name: str
    line: int
    column: int
    type_annotation: TypeAnnotation | None = None
    default_value: "Expression | None" = None


@dataclass(slots=True)
class TypeDeclaration:
    name: str
    fields: list[TypeField]
    line: int
    column: int


@dataclass(slots=True)
class ImportStatement:
    line: int
    column: int
    alias: str | None = None
    module: str | None = None


@dataclass(slots=True)
class ExpressionStatement:
    expression: "Expression"
    line: int
    column: int


@dataclass(slots=True)
class IfStatement:
    condition: "Expression"
    consequent: list["Statement"]
    line: int
    column: int
    alternate: list["Statement"] | None = None


@dataclass(slots=True)
class ForStatement:
    iterator: str | None
    body: list["Statement"]
    line: int
    column: int
    from_expr: "Expression | None" = None
    to_expr: "Expression | None" = None
    step_expr: "Expression | None" = None
    iterable: "Expression | None" = None
    iterators: list[DestructuringVariable] = field(default_factory=list)


@dataclass(slots=True)
class WhileStatement:
    condition: "Expression"
    body: list["Statement"]
    line: int
    column: int


@dataclass(slots=True)
class SwitchCase:
    line: int
    column: int
    body: list["Statement"]
    condition: "Expression | None" = None


@dataclass(slots=True)
class SwitchStatement:
    expression: "Expression | None"
    cases: list[SwitchCase]
    line: int
    column: int


@dataclass(slots=True)
class SwitchExpressionCase:
    value: "Expression"
    line: int
    column: int
    condition: "Expression | None" = None
    body: list["Statement"] = field(default_factory=list)


@dataclass(slots=True)
class SwitchExpression:
    cases: list[SwitchExpressionCase]
    line: int
    column: int
    expression: "Expression | None" = None


@dataclass(slots=True)
class ReturnStatement:
    value: "Expression"
    line: int
    column: int


@dataclass(slots=True)
class Identifier:
    name: str
    line: int
    column: int


@dataclass(slots=True)
class Literal:
    value: str | float | bool
    raw: str
    line: int
    column: int


@dataclass(slots=True)
class CallExpression:
    callee: "Expression"
    arguments: list[CallArgument]
    line: int
    column: int


@dataclass(slots=True)
class MemberExpression:
    object: "Expression"
    property: Identifier
    line: int
    column: int


@dataclass(slots=True)
class BinaryExpression:
    operator: str
    left: "Expression"
    right: "Expression"
    line: int
    column: int


@dataclass(slots=True)
class UnaryExpression:
    operator: str
    argument: "Expression"
    line: int
    column: int


@dataclass(slots=True)
class TernaryExpression:
    condition: "Expression"
    consequent: "Expression"
    alternate: "Expression"
    line: int
    column: int


@dataclass(slots=True)
class IfExpression:
    condition: "Expression"
    consequent: "Expression"
    alternate: "Expression"
    line: int
    column: int


@dataclass(slots=True)
class ArrayExpression:
    elements: list["Expression"]
    line: int
    column: int


@dataclass(slots=True)
class IndexExpression:
    object: "Expression"
    index: "Expression"
    line: int
    column: int


Statement = (
    VariableDeclaration
    | DestructuringAssignment
    | AssignmentStatement
    | CompoundAssignmentStatement
    | TargetAssignmentStatement
    | FunctionDeclaration
    | TypeDeclaration
    | ImportStatement
    | ExpressionStatement
    | IfStatement
    | ForStatement
    | WhileStatement
    | SwitchStatement
    | ReturnStatement
)

Expression = (
    Identifier
    | Literal
    | CallExpression
    | MemberExpression
    | BinaryExpression
    | UnaryExpression
    | TernaryExpression
    | IfExpression
    | SwitchExpression
    | ArrayExpression
    | IndexExpression
)
