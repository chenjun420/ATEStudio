#!/usr/bin/env python3
"""Generate TypeScript type definitions from Python dataclass/enum definitions.

Reads the canonical YAML DSL schema from src/shared/dsl.py using AST parsing
and emits matching TypeScript interfaces/types to frontend/src/types/dsl.ts.

Usage:
    python scripts/generate_dsl_types.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Project root detection
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DSL_MODULE_PATH = PROJECT_ROOT / "src" / "shared" / "dsl.py"
OUTPUT_PATH = PROJECT_ROOT / "frontend" / "src" / "types" / "dsl.ts"

# Python type -> TypeScript type mapping
TYPE_MAP: dict[str, str] = {
    "str": "string",
    "int": "number",
    "float": "number",
    "bool": "boolean",
    "Any": "unknown",
    "NoneType": "null",
    "None": "null",
}

# Set of enum class names (populated during generation)
_ENUM_NAMES: set[str] = set()


def python_type_str_to_ts(type_str: str, known_types: set[str]) -> str:
    """Convert a Python type annotation string to a TypeScript type string."""
    type_str = type_str.strip()

    # Handle None
    if type_str == "None":
        return "null"

    # Handle simple mapped types
    if type_str in TYPE_MAP:
        return TYPE_MAP[type_str]

    # Handle known type references (class names)
    # For enum types, use the Value type (string literal union)
    if type_str in known_types and type_str in _ENUM_NAMES:
        return f"{type_str}Value"
    if type_str in known_types:
        return type_str

    # Handle Optional[X] = X | None
    if type_str.startswith("Optional[") and type_str.endswith("]"):
        inner = type_str[9:-1]
        inner_ts = python_type_str_to_ts(inner, known_types)
        return f"{inner_ts} | null"

    # Handle dict[K, V]
    if type_str.startswith("dict[") and type_str.endswith("]"):
        inner = type_str[5:-1]
        # Split on comma, respecting nested brackets
        parts = _split_type_args(inner)
        if len(parts) == 2:
            key_ts = python_type_str_to_ts(parts[0].strip(), known_types)
            val_ts = python_type_str_to_ts(parts[1].strip(), known_types)
            return f"Record<{key_ts}, {val_ts}>"
        return "Record<string, unknown>"

    # Handle list[T]
    if type_str.startswith("list[") and type_str.endswith("]"):
        inner = type_str[5:-1]
        inner_ts = python_type_str_to_ts(inner, known_types)
        return f"Array<{inner_ts}>"

    # Handle union types with | (Python 3.10+ syntax)
    if "|" in type_str:
        parts = _split_union(type_str)
        ts_parts = [python_type_str_to_ts(p.strip(), known_types) for p in parts]
        return " | ".join(ts_parts)

    # Fallback: return as-is (it's likely a class name)
    return type_str


def _split_type_args(s: str) -> list[str]:
    """Split type arguments on commas, respecting nested brackets."""
    result: list[str] = []
    depth = 0
    current = ""
    for ch in s:
        if ch in "[(":
            depth += 1
            current += ch
        elif ch in ")]":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            result.append(current)
            current = ""
        else:
            current += ch
    if current:
        result.append(current)
    return result


def _split_union(s: str) -> list[str]:
    """Split union type on |, respecting nested brackets."""
    result: list[str] = []
    depth = 0
    current = ""
    for ch in s:
        if ch in "[(":
            depth += 1
            current += ch
        elif ch in ")]":
            depth -= 1
            current += ch
        elif ch == "|" and depth == 0:
            result.append(current)
            current = ""
        else:
            current += ch
    if current:
        result.append(current)
    return result


def ast_annotation_to_str(node: ast.expr) -> str:
    """Convert an AST annotation node back to a string."""
    if isinstance(node, ast.Constant):
        if node.value is None:
            return "None"
        return str(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{ast_annotation_to_str(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        base = ast_annotation_to_str(node.value)
        slice_node = node.slice
        if isinstance(slice_node, ast.Tuple):
            args = ", ".join(ast_annotation_to_str(elt) for elt in slice_node.elts)
        else:
            args = ast_annotation_to_str(slice_node)
        return f"{base}[{args}]"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = ast_annotation_to_str(node.left)
        right = ast_annotation_to_str(node.right)
        return f"{left} | {right}"
    return ast.unparse(node)


class DSLVisitor(ast.NodeVisitor):
    """AST visitor that extracts enum and dataclass definitions."""

    def __init__(self) -> None:
        self.enums: list[tuple[str, list[tuple[str, str]]]] = []
        self.dataclasses: list[tuple[str, list[tuple[str, str, bool]]]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Check if it's a dataclass
        is_dataclass = any(
            isinstance(dec, ast.Name) and dec.id == "dataclass"
            or isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Name)
            and dec.func.id == "dataclass"
            for dec in node.decorator_list
        )

        # Check if it's an Enum subclass
        is_enum = any(
            isinstance(base, ast.Name) and base.id == "Enum"
            for base in node.bases
        )

        if is_enum:
            members: list[tuple[str, str]] = []
            for item in node.body:
                if isinstance(item, ast.Assign) and len(item.targets) == 1:
                    name = item.targets[0].id if isinstance(item.targets[0], ast.Name) else ""
                    if isinstance(item.value, ast.Constant):
                        members.append((name, str(item.value.value)))
                    elif isinstance(item.value, ast.Call):
                        # Enum(value) form
                        if item.value.args and isinstance(item.value.args[0], ast.Constant):
                            members.append((name, str(item.value.args[0].value.value)))
            self.enums.append((node.name, members))

        elif is_dataclass:
            fields_info: list[tuple[str, str, bool]] = []
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and item.target:
                    field_name = item.target.id if isinstance(item.target, ast.Name) else ""
                    type_str = ast_annotation_to_str(item.annotation)

                    # Check if field has a default (field() or direct value)
                    has_default = item.value is not None

                    # Check for field() call with default_factory
                    if isinstance(item.value, ast.Call):
                        func = item.value.func
                        if isinstance(func, ast.Name) and func.id == "field":
                            has_default = True

                    # Check if type includes None (Optional)
                    has_none = "None" in type_str

                    is_optional = has_default or has_none
                    fields_info.append((field_name, type_str, is_optional))

            self.dataclasses.append((node.name, fields_info))

        self.generic_visit(node)


def generate_enum_ts(name: str, members: list[tuple[str, str]]) -> str:
    """Generate a TypeScript const object + type union from extracted enum data.

    Uses 'as const' pattern instead of 'enum' to be compatible with
    TypeScript's erasableSyntaxOnly compiler option.
    """
    # Generate const object
    lines = [f"export const {name} = {{"]
    for member_name, member_value in members:
        lines.append(f'  {member_name}: "{member_value}",')
    lines.append("} as const")
    # Generate type union
    member_types = " | ".join(f'"{mv}"' for _, mv in members)
    lines.append(f"\nexport type {name}Value = {member_types}")
    return "\n".join(lines)


def generate_dataclass_ts(
    name: str,
    fields_info: list[tuple[str, str, bool]],
    known_types: set[str],
) -> str:
    """Generate a TypeScript interface from extracted dataclass data."""
    lines = [f"export interface {name} {{"]

    for field_name, type_str, is_optional in fields_info:
        ts_type = python_type_str_to_ts(type_str, known_types)
        optional_marker = "?" if is_optional else ""
        lines.append(f"  {field_name}{optional_marker}: {ts_type}")

    lines.append("}")
    return "\n".join(lines)


def generate() -> str:
    """Generate the full TypeScript file content from the DSL module."""
    source = DSL_MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    visitor = DSLVisitor()
    visitor.visit(tree)

    # Collect known type names for cross-referencing
    known_type_names = {name for name, _ in visitor.enums} | {name for name, _ in visitor.dataclasses}

    # Track enum names so we can use Value types for them in interfaces
    global _ENUM_NAMES
    _ENUM_NAMES = {name for name, _ in visitor.enums}

    # Build output
    header = (
        "// Auto-generated by scripts/generate_dsl_types.py\n"
        "// DO NOT EDIT MANUALLY — regenerate with: python scripts/generate_dsl_types.py\n"
    )

    sections: list[str] = [header]

    # Emit enums first (they're referenced by interfaces)
    for name, members in sorted(visitor.enums, key=lambda x: x[0]):
        sections.append(generate_enum_ts(name, members))

    # Emit interfaces
    for name, fields_info in sorted(visitor.dataclasses, key=lambda x: x[0]):
        sections.append(generate_dataclass_ts(name, fields_info, known_type_names))

    # Emit frontend convenience types that build on the canonical schema
    convenience_types = """\
/**
 * Frontend convenience types built on the canonical DSL schema.
 * YamlScope narrows YamlPlan.scope to the common { variables } pattern.
 * YamlSequence is the frontend's view of a YamlPlan for YAML serialization.
 */
export interface YamlScope {
  variables?: Record<string, unknown>
}

export interface YamlSequence {
  name: string
  version: string
  scope?: YamlScope
  max_concurrency?: number
  steps: Array<YamlStep | YamlLoop>
}"""
    sections.append(convenience_types)

    return "\n\n".join(sections) + "\n"


def main() -> None:
    """Main entry point."""
    # Ensure output directory exists
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    content = generate()
    OUTPUT_PATH.write_text(content, encoding="utf-8")
    print(f"Generated TypeScript types at: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
