"""
Transforms the AST of a Django views.py file (focused on DRF ViewSets)
into a structured format, identifying ViewSet classes, their attributes,
standard methods, and custom actions.
"""


def _get_node_value(node):
    """
    Recursively extracts a simple value or representation from an AST node.
    Handles Constants, Names, Attributes, and lists/tuples of these.
    """
    if not node:
        return None
    nodetype = node.get("_nodetype")

    if nodetype == "Constant":
        return node.get("value")
    elif nodetype == "Name":
        return node.get("id")
    elif nodetype == "Attribute":
        value_part = _get_node_value(node.get("value"))
        attr_part = node.get("attr")
        if value_part and attr_part:
            return f"{value_part}.{attr_part}"
        elif attr_part:
            return attr_part
    elif nodetype in ["List", "Tuple"]:
        return [_get_node_value(el) for el in node.get("elts", [])]
    elif nodetype == "Call":  # For queryset = Model.objects.filter() like constructs
        func_repr = _get_node_value(node.get("func"))
        args_repr = []
        if node.get("args"):
            args_repr.extend([_get_node_value(arg) for arg in node.get("args", [])])
        if node.get("keywords"):
            args_repr.extend(
                [
                    f"{kw.get('arg')}={_get_node_value(kw.get('value'))}"
                    for kw in node.get("keywords", [])
                ]
            )
        return f"{func_repr}({', '.join(filter(None, [str(a) for a in args_repr]))})"
    return None  # Or some string representation for unhandled complex types


def _parse_action_decorator(decorator_node):
    """Parses arguments from an @action decorator Call node."""
    args = {}
    if decorator_node.get("_nodetype") == "Call":
        # Positional arguments (less common for @action, but handle if necessary)
        # For now, focusing on keyword arguments as they are standard for @action
        for kw in decorator_node.get("keywords", []):
            arg_name = kw.get("arg")
            arg_value = _get_node_value(kw.get("value"))
            if arg_name:
                args[arg_name] = arg_value
    return args


def transform_views_py(views_py_ast):
    """Transforms a views.py AST (focused on ViewSets) into a structured dictionary."""
    output_viewsets = []
    ast_declarations = views_py_ast.get("ast_declarations", [])

    for declaration in ast_declarations:
        if declaration.get("_nodetype") == "ClassDef":
            class_name = declaration.get("name")
            base_classes = [
                _get_node_value(base) for base in declaration.get("bases", [])
            ]

            is_viewset_class = any(
                bc
                and (
                    "viewsets.ModelViewSet" in bc
                    or "viewsets.ReadOnlyModelViewSet" in bc
                )
                for bc in base_classes
            )

            if not is_viewset_class:
                continue

            viewset_info = {
                "name": class_name,
                "docstring": None,
                "serializer_class": None,
                "permission_classes": [],
                "lookup_field": None,
                "lookup_url_kwarg": None,
                "queryset_definition": None,
                "methods": [],
                "actions": [],
            }

            # Attempt to get class docstring (first Expr node if it's a Constant string)
            if (
                declaration.get("body")
                and declaration.get("body")[0].get("_nodetype") == "Expr"
            ):
                docstring_node_value = declaration.get("body")[0].get("value")
                if (
                    docstring_node_value
                    and docstring_node_value.get("_nodetype") == "Constant"
                ):
                    viewset_info["docstring"] = _get_node_value(docstring_node_value)

            class_body = declaration.get("body", [])
            for item in class_body:
                item_type = item.get("_nodetype")

                if item_type == "Assign":
                    target_name = item.get("targets", [{}])[0].get("id")
                    value_node = item.get("value")

                    if target_name == "serializer_class":
                        viewset_info["serializer_class"] = _get_node_value(value_node)
                    elif target_name == "permission_classes":
                        viewset_info["permission_classes"] = _get_node_value(value_node)
                    elif target_name == "lookup_field":
                        viewset_info["lookup_field"] = _get_node_value(value_node)
                    elif target_name == "lookup_url_kwarg":
                        viewset_info["lookup_url_kwarg"] = _get_node_value(value_node)
                    elif target_name == "queryset":
                        viewset_info["queryset_definition"] = _get_node_value(
                            value_node
                        )

                elif item_type == "FunctionDef":
                    func_name = item.get("name")
                    func_docstring = item.get("docstring")
                    func_body_source = item.get("body_source", "")
                    method_data = {
                        "name": func_name,
                        "docstring": func_docstring,
                        "body": func_body_source,
                    }

                    is_action = False
                    if item.get("decorator_list"):
                        for decorator in item.get("decorator_list", []):
                            # Check if decorator is @action or @<something>.action
                            decorator_call_node = (
                                decorator
                                if decorator.get("_nodetype") == "Call"
                                else None
                            )
                            decorator_name_node = (
                                decorator_call_node.get("func")
                                if decorator_call_node
                                else decorator
                            )

                            decorator_name = _get_node_value(decorator_name_node)

                            # Ensure decorator_name is a string before using string methods
                            if isinstance(decorator_name, str) and (
                                decorator_name == "action"
                                or decorator_name.endswith(".action")
                            ):
                                action_args = (
                                    _parse_action_decorator(decorator_call_node)
                                    if decorator_call_node
                                    else {}
                                )
                                method_data["decorator_args"] = action_args
                                viewset_info["actions"].append(method_data)
                                is_action = True
                                break

                    if not is_action and func_name in [
                        "get_queryset",
                        "perform_create",
                        "get_object",
                    ]:
                        viewset_info["methods"].append(method_data)

            output_viewsets.append(viewset_info)

    return {"viewsets": output_viewsets}
