"""
Transforms the AST of a Django urls.py file into a structured format,
identifying router registrations and urlpatterns details.
"""


def get_node_value(node):
    """Helper to extract a simple value from an AST node."""
    if not node:
        return None
    nodetype = node.get("_nodetype")
    if nodetype == "Constant":
        return node.get("value")
    elif nodetype == "Name":
        return node.get("id")
    elif nodetype == "Attribute":
        value_part = get_node_value(node.get("value"))
        attr_part = node.get("attr")
        if value_part and attr_part:
            return f"{value_part}.{attr_part}"
        elif attr_part:
            return attr_part
    # Add other types if necessary, e.g., for f-strings or other complex nodes
    return None


def get_keyword_arg_value(keywords_list, arg_name):
    """Helper to find and extract the value of a specific keyword argument."""
    for kw in keywords_list:
        if kw.get("arg") == arg_name:
            return get_node_value(kw.get("value"))
    return None


def transform_urls_py(urls_py_ast):
    """Transforms a urls.py AST into a structured dictionary."""
    output = {
        "router_registrations": [],
        "urlpatterns": [],
    }

    declarations = urls_py_ast.get("ast_declarations", [])

    for dec in declarations:
        # Handle urlpatterns assignment
        if dec.get("_nodetype") == "Assign":
            target_node = dec.get("targets", [{}])[0]
            value_node = dec.get("value")

            if (
                target_node.get("_nodetype") == "Name"
                and target_node.get("id") == "urlpatterns"
            ):
                urlpatterns_list_node = value_node
                if (
                    urlpatterns_list_node
                    and urlpatterns_list_node.get("_nodetype") == "List"
                ):
                    for item_node in urlpatterns_list_node.get("elts", []):
                        if item_node.get("_nodetype") == "Call" and item_node.get(
                            "func", {}
                        ).get("id") in ["path", "re_path"]:
                            route_pattern = (
                                get_node_value(item_node.get("args", [])[0])
                                if len(item_node.get("args", [])) > 0
                                else None
                            )
                            view_or_include_node = (
                                item_node.get("args", [])[1]
                                if len(item_node.get("args", [])) > 1
                                else None
                            )
                            name_kwarg = get_keyword_arg_value(
                                item_node.get("keywords", []), "name"
                            )

                            path_detail = {
                                "route": route_pattern,
                                "name": name_kwarg,
                                "view_type": "unknown",
                                "view_name": None,
                            }

                            if view_or_include_node:
                                func_call_name = get_node_value(
                                    view_or_include_node.get("func")
                                )
                                if (
                                    view_or_include_node.get("_nodetype") == "Call"
                                    and func_call_name == "include"
                                ):
                                    include_arg_node = (
                                        view_or_include_node.get("args", [])[0]
                                        if len(view_or_include_node.get("args", [])) > 0
                                        else None
                                    )
                                    include_target = get_node_value(include_arg_node)

                                    # Skip router.urls includes
                                    if include_target == "router.urls":
                                        continue

                                    path_detail["view_type"] = "module_include"
                                    path_detail["include_target"] = include_target
                                elif (
                                    view_or_include_node.get("_nodetype") == "Call"
                                    and isinstance(func_call_name, str)
                                    and func_call_name.endswith(".as_view")
                                ):
                                    path_detail["view_type"] = "class_based_view"
                                    view_class_full_name = get_node_value(
                                        view_or_include_node.get("func").get("value")
                                    )
                                    path_detail["view_name"] = view_class_full_name
                                elif view_or_include_node.get("_nodetype") == "Name":
                                    path_detail["view_type"] = "function_based_view"
                                    path_detail["view_name"] = get_node_value(
                                        view_or_include_node
                                    )

                            output["urlpatterns"].append(path_detail)

        # Handle Router Registrations
        if dec.get("_nodetype") == "Expr":
            call_node = dec.get("value")
            if call_node and call_node.get("_nodetype") == "Call":
                func_attribute_node = call_node.get("func")
                if (
                    func_attribute_node
                    and func_attribute_node.get("_nodetype") == "Attribute"
                    and func_attribute_node.get("attr") == "register"
                ):
                    object_name = get_node_value(func_attribute_node.get("value"))
                    if object_name == "router":
                        prefix = (
                            get_node_value(call_node.get("args", [])[0])
                            if len(call_node.get("args", [])) > 0
                            else None
                        )
                        viewset = (
                            get_node_value(call_node.get("args", [])[1])
                            if len(call_node.get("args", [])) > 1
                            else None
                        )
                        output["router_registrations"].append(
                            {"prefix": prefix, "viewset": viewset}
                        )

    return output
