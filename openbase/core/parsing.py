import ast
from pathlib import Path
from typing import Any


class SourceMappedDict(dict):
    """A dictionary that keeps track of the AST node it was created from."""

    def __init__(self, *args, ast_node=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ast_node = ast_node


class SourceMappedString(str):
    """A string that keeps track of the AST node it was created from."""

    def __new__(cls, value, ast_node=None):
        instance = str.__new__(cls, value)
        instance.ast_node = ast_node
        return instance


def parse_python_file_ast(
    file_path: str | Path, functions_to_stringify: list[str] | None = None
) -> dict[str, Any]:
    path = file_path if isinstance(file_path, Path) else Path(file_path)
    source_code = path.read_text(encoding="utf-8")
    tree = ast.parse(source_code, filename=str(path))

    # Convert the AST to dict and extract just the body list
    ast_dict = _ast_to_dict(
        tree,
        source_code,
        parent_node=None,
        functions_to_stringify=functions_to_stringify,
    )

    # Since tree is a Module node, ast_dict must be a dictionary with a 'body' key
    declarations = ast_dict["body"] if isinstance(ast_dict, dict) else []

    return declarations


def _ast_to_dict(
    node: Any,
    source_code: str,
    parent_node: ast.AST | None = None,
    functions_to_stringify: list[str] | None = None,
) -> dict[str, Any] | list[Any] | None | str | int | float | bool:
    """
    Recursively converts an AST node to a nested dictionary structure.
    For FunctionDef or AsyncFunctionDef nodes, if the function name is in
    functions_to_stringify, its body is parsed as AST nodes.
    Otherwise, the function's body is extracted as a raw source string under 'body_source'.
    Location information (lineno, col_offset, etc.) is not included in the output.
    Docstrings are extracted for all functions.
    """
    if isinstance(node, ast.AST):
        result = SourceMappedDict({"_nodetype": node.__class__.__name__}, ast_node=node)

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Always try to extract docstring first, as it's part of the body
            docstring = ast.get_docstring(node, clean=True)
            if docstring is not None:
                result["docstring"] = SourceMappedString(docstring, ast_node=node)

            # Process other fields (name, args, decorators, etc.)
            for field, value in ast.iter_fields(node):
                if field == "body":
                    if functions_to_stringify and node.name in functions_to_stringify:
                        # Preserve AST structure for the body of specified functions
                        # Skip the docstring node if it was already processed
                        body_nodes = node.body
                        if (
                            docstring is not None
                            and body_nodes
                            and isinstance(body_nodes[0], ast.Expr)
                            and isinstance(body_nodes[0].value, ast.Constant)
                            and isinstance(body_nodes[0].value.value, str)
                        ):
                            body_nodes = body_nodes[1:]  # Skip the docstring node
                        result[field] = _ast_to_dict(
                            body_nodes, source_code, node, functions_to_stringify
                        )
                    else:
                        # Convert body to source string for other functions
                        body_source_str = ""  # Default to empty string
                        if node.body:  # If there are statements in the body
                            # Exclude docstring from body_source if it was already extracted
                            start_node_index = 0
                            if (
                                docstring is not None
                                and node.body
                                and isinstance(node.body[0], ast.Expr)
                                and isinstance(node.body[0].value, ast.Constant)
                                and isinstance(node.body[0].value.value, str)
                            ):
                                start_node_index = (
                                    1  # Skip the docstring node for source extraction
                                )

                            if start_node_index < len(node.body):
                                first_stmt_for_source = node.body[start_node_index]
                                last_stmt_for_source = node.body[-1]

                                body_start_lineno = getattr(
                                    first_stmt_for_source, "lineno", None
                                )
                                body_end_lineno = getattr(
                                    last_stmt_for_source,
                                    "end_lineno",
                                    getattr(last_stmt_for_source, "lineno", None),
                                )

                                if (
                                    body_start_lineno is not None
                                    and body_end_lineno is not None
                                ):
                                    all_source_lines = source_code.splitlines(True)
                                    start_idx = body_start_lineno - 1
                                    end_idx = body_end_lineno

                                    if 0 <= start_idx < len(
                                        all_source_lines
                                    ) and start_idx < end_idx <= len(all_source_lines):
                                        body_lines_extracted = all_source_lines[
                                            start_idx:end_idx
                                        ]
                                        body_source_str = "".join(
                                            body_lines_extracted
                                        ).strip()
                        result["body_source"] = SourceMappedString(
                            body_source_str, ast_node=node
                        )
                elif (
                    field == "type_comment"
                ):  # avoid processing type_comment twice, it's part of args for python < 3.8
                    if (
                        hasattr(node, "args")
                        and hasattr(node.args, field)
                        and getattr(node.args, field) is value
                    ):
                        pass  # Will be handled when processing node.args
                    else:
                        result[field] = _ast_to_dict(
                            value, source_code, node, functions_to_stringify
                        )
                else:  # Other fields (name, args, decorators, type_comment, returns, etc.)
                    result[field] = _ast_to_dict(
                        value, source_code, node, functions_to_stringify
                    )
        else:  # Not a FunctionDef, process all fields normally
            # For ClassDef nodes, preserve line number information
            if isinstance(node, ast.ClassDef):
                result["lineno"] = getattr(node, "lineno", None)
                result["end_lineno"] = getattr(node, "end_lineno", None)

            for field, value in ast.iter_fields(node):
                if field not in (
                    "lineno",
                    "col_offset",
                    "end_lineno",
                    "end_col_offset",
                    "ctx",
                ):
                    result[field] = _ast_to_dict(
                        value, source_code, node, functions_to_stringify
                    )

        return result
    elif isinstance(node, list):
        return [
            _ast_to_dict(item, source_code, parent_node, functions_to_stringify)
            for item in node
        ]
    elif isinstance(node, str):
        return SourceMappedString(node, ast_node=parent_node)
    elif isinstance(node, (str, int, float, bool)) or node is None:
        return node
    else:
        return SourceMappedString(str(node), ast_node=parent_node)
