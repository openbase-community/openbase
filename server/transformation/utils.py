def extract_function_info(func_def_node):
    """
    Extract comprehensive information about a function definition from its AST node.

    Args:
        func_def_node: AST node of type FunctionDef

    Returns:
        dict: Contains detailed function information including:
            - name: function name
            - is_async: boolean indicating if it's an async function
            - docstring: function's docstring if any
            - body_source: source code of function body
            - args: detailed argument information including:
                - positional_only: list of position-only arguments
                - regular_args: list of regular arguments
                - keyword_only: list of keyword-only arguments
                - defaults: mapping of arguments to their default values
                - vararg: name of *args parameter if present
                - kwarg: name of **kwargs parameter if present
    """
    if func_def_node.get("_nodetype") not in ["FunctionDef", "AsyncFunctionDef"]:
        raise ValueError("Node must be a FunctionDef")

    info = {
        "name": func_def_node.get("name"),
        "is_async": func_def_node.get("_nodetype") == "AsyncFunctionDef",
        "docstring": "",
        "body_source": func_def_node.get("body_source", "").strip(),
        "args": {
            "positional_only": [],
            "regular_args": [],
            "keyword_only": [],
            "defaults": {},
            "vararg": None,
            "kwarg": None,
        },
    }

    # Extract docstring if present
    if (
        func_def_node.get("body")
        and func_def_node["body"][0].get("_nodetype") == "Expr"
        and func_def_node["body"][0].get("value", {}).get("_nodetype") == "Constant"
    ):
        info["docstring"] = func_def_node["body"][0]["value"].get("value", "").strip()

    args_node = func_def_node.get("args", {})

    # Handle position-only arguments
    for arg in args_node.get("posonlyargs", []):
        if isinstance(arg, dict) and arg.get("arg"):
            info["args"]["positional_only"].append(arg["arg"])

    # Handle regular arguments
    for arg in args_node.get("args", []):
        if isinstance(arg, dict) and arg.get("arg"):
            info["args"]["regular_args"].append(arg["arg"])

    # Handle keyword-only arguments
    for arg in args_node.get("kwonlyargs", []):
        if isinstance(arg, dict) and arg.get("arg"):
            info["args"]["keyword_only"].append(arg["arg"])

    # Handle *args
    vararg = args_node.get("vararg")
    if isinstance(vararg, dict) and vararg.get("arg"):
        info["args"]["vararg"] = vararg["arg"]

    # Handle **kwargs
    kwarg = args_node.get("kwarg")
    if isinstance(kwarg, dict) and kwarg.get("arg"):
        info["args"]["kwarg"] = kwarg["arg"]

    # Handle defaults for regular args
    defaults = args_node.get("defaults", [])
    if defaults:
        regular_args = info["args"]["regular_args"]
        for i, default in enumerate(defaults):
            # Defaults are matched from the end of regular args
            arg_index = len(regular_args) - len(defaults) + i
            if arg_index >= 0 and arg_index < len(regular_args):
                if default.get("_nodetype") == "Constant":
                    info["args"]["defaults"][regular_args[arg_index]] = default.get(
                        "value"
                    )
                elif default.get("_nodetype") == "Name":
                    info["args"]["defaults"][regular_args[arg_index]] = default.get(
                        "id"
                    )

    # Handle defaults for keyword-only args
    kw_defaults = args_node.get("kw_defaults", [])
    for i, default in enumerate(kw_defaults):
        if i < len(info["args"]["keyword_only"]) and default is not None:
            if default.get("_nodetype") == "Constant":
                info["args"]["defaults"][info["args"]["keyword_only"][i]] = default.get(
                    "value"
                )
            elif default.get("_nodetype") == "Name":
                info["args"]["defaults"][info["args"]["keyword_only"][i]] = default.get(
                    "id"
                )

    return info
