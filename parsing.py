import ast
from pathlib import Path
from typing import Any, Dict, List, Union


def parse_django_file_ast(file_path: Union[str, Path]) -> Dict[str, Any]:
    path = file_path if isinstance(file_path, Path) else Path(file_path)
    if not path.exists():
        return {"error": f"File not found: {path}", "ast": None}

    try:
        source_code = path.read_text(encoding="utf-8")
        tree = ast.parse(source_code, filename=str(path))
        # Convert the AST to dict and extract just the body list
        ast_dict = _ast_to_dict(tree, source_code)
        # Since we know tree is a Module node, ast_dict must be a dictionary with a 'body' key
        declarations = ast_dict["body"] if isinstance(ast_dict, dict) else []
        return {"file_path": str(path), "ast_declarations": declarations}
    except Exception as e:
        return {"error": f"Failed to parse {path}: {e}", "ast": None}


def _ast_to_dict(
    node: Any, source_code: str
) -> Union[Dict[str, Any], List[Any], None, str, int, float, bool]:
    """
    Recursively converts an AST node to a nested dictionary structure.
    For FunctionDef or AsyncFunctionDef nodes, the function signature is parsed,
    and the function's body is extracted as a raw source string under the 'body_source' key.
    Location information (lineno, col_offset, etc.) is not included in the output.
    """
    if isinstance(node, ast.AST):
        result: Dict[str, Any] = {"_nodetype": node.__class__.__name__}

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Process most fields, but customize 'body'
            for field, value in ast.iter_fields(node):
                if field == "body":
                    body_source_str = ""  # Default to empty string
                    if node.body:  # If there are statements in the body
                        first_stmt = node.body[0]
                        last_stmt = node.body[-1]

                        body_start_lineno = getattr(first_stmt, "lineno", None)
                        # Use end_lineno of the last statement, or its lineno if end_lineno is not available
                        body_end_lineno = getattr(
                            last_stmt, "end_lineno", getattr(last_stmt, "lineno", None)
                        )

                        if (
                            body_start_lineno is not None
                            and body_end_lineno is not None
                        ):
                            all_source_lines = source_code.splitlines(True)
                            # AST lineno is 1-indexed for list slicing
                            start_idx = body_start_lineno - 1
                            # end_lineno refers to the line number, so slice up to that line number
                            end_idx = body_end_lineno

                            # Ensure indices are valid and in correct order
                            if 0 <= start_idx < len(
                                all_source_lines
                            ) and start_idx < end_idx <= len(all_source_lines):
                                body_lines_extracted = all_source_lines[
                                    start_idx:end_idx
                                ]
                                body_source_str = "".join(body_lines_extracted)

                    result["body_source"] = body_source_str
                    result[
                        field
                    ] = []  # Original 'body' (AST list) is now represented by 'body_source'
                else:  # Other fields of FunctionDef (name, args, decorators, type_comment, returns, etc.)
                    result[field] = _ast_to_dict(value, source_code)
        else:  # Not a FunctionDef, process all fields normally for other AST types
            for field, value in ast.iter_fields(node):
                # Skip location-related fields
                if field not in (
                    "lineno",
                    "col_offset",
                    "end_lineno",
                    "end_col_offset",
                    "ctx",
                ):
                    result[field] = _ast_to_dict(value, source_code)

        return result
    elif isinstance(node, list):
        return [_ast_to_dict(item, source_code) for item in node]
    elif isinstance(node, (str, int, float, bool)) or node is None:
        return node
    else:
        return str(node)
