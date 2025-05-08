from .utils import extract_function_info


def parse_model_field(
    value_node, target_name, class_level_vars, class_level_choices_vars
):
    """Parse a Django model field assignment and return the field information."""
    field_type = f"models.{value_node['func']['attr']}"
    field_args = []
    field_kwargs = {}
    processed_choices = None

    for arg_node in value_node.get("args", []):
        if arg_node.get("_nodetype") == "Name":
            field_args.append(arg_node.get("id"))
        elif arg_node.get("_nodetype") == "Constant":
            field_args.append(arg_node.get("value"))

    for kwarg_node in value_node.get("keywords", []):
        kwarg_name = kwarg_node.get("arg")
        kwarg_val_node = kwarg_node.get("value")
        kwarg_value = None
        if kwarg_val_node:
            if kwarg_val_node.get("_nodetype") == "Constant":
                kwarg_value = kwarg_val_node.get("value")
            elif kwarg_val_node.get("_nodetype") == "Name":
                kwarg_value = kwarg_val_node.get("id")
            elif kwarg_val_node.get("_nodetype") == "Attribute":
                kwarg_value = f"{kwarg_val_node.get('value', {}).get('id')}.{kwarg_val_node.get('attr')}"

        if kwarg_name == "choices" and kwarg_value in class_level_choices_vars:
            parsed_choices_tuples = class_level_choices_vars[kwarg_value]
            resolved_choices = []
            for const_name, human_readable in parsed_choices_tuples:
                if const_name in class_level_vars:
                    resolved_choices.append(
                        (class_level_vars[const_name], human_readable)
                    )
                else:
                    resolved_choices.append((const_name, human_readable))
            processed_choices = resolved_choices
        else:
            if kwarg_name and kwarg_value is not None:
                field_kwargs[kwarg_name] = kwarg_value

    field_data = {
        "name": target_name,
        "type": field_type,
        "args": field_args,
        "kwargs": field_kwargs,
    }
    if processed_choices is not None:
        field_data["choices"] = processed_choices

    return field_data


def parse_class_level_variable(
    target_name, value_node, class_level_vars, class_level_choices_vars
):
    """Parse class-level variable assignments like constants and choices."""
    if value_node.get("_nodetype") == "Constant" and isinstance(
        value_node.get("value"), str
    ):
        class_level_vars[target_name] = value_node.get("value")
    elif target_name.endswith("_CHOICES") and value_node.get("_nodetype") == "List":
        choices_list = []
        for elt_tuple in value_node.get("elts", []):
            if (
                elt_tuple.get("_nodetype") == "Tuple"
                and len(elt_tuple.get("elts", [])) == 2
            ):
                const_name_node = elt_tuple["elts"][0]
                human_readable_node = elt_tuple["elts"][1]
                if (
                    const_name_node.get("_nodetype") == "Name"
                    and human_readable_node.get("_nodetype") == "Constant"
                ):
                    choices_list.append(
                        (const_name_node.get("id"), human_readable_node.get("value"))
                    )
        class_level_choices_vars[target_name] = choices_list


def parse_method_or_property(item):
    """Parse a class method or property definition."""
    is_property = False
    for decorator in item.get("decorator_list", []):
        if decorator.get("_nodetype") == "Name" and decorator.get("id") == "property":
            is_property = True
            break

    if is_property:
        # For properties, we keep the simpler format since they don't have complex args
        method_name = item.get("name")
        method_body = item.get("body_source", "").strip()
        docstring = item.get("docstring", "").strip()
        return method_name, [], method_body, is_property, docstring
    else:
        # For methods, use the comprehensive function info extraction
        func_info = extract_function_info(item)
        return (
            func_info["name"],
            {
                "positional_only": func_info["args"]["positional_only"],
                "regular_args": func_info["args"]["regular_args"],
                "keyword_only": func_info["args"]["keyword_only"],
                "defaults": func_info["args"]["defaults"],
                "vararg": func_info["args"]["vararg"],
                "kwarg": func_info["args"]["kwarg"],
            },
            func_info["body_source"],
            is_property,
            func_info["docstring"],
        )


def parse_meta_class(item):
    """Parse the Meta inner class of a Django model."""
    meta_info = {}
    for meta_item in item.get("body", []):
        if meta_item.get("_nodetype") == "Assign":
            meta_attr_name = meta_item.get("targets", [{}])[0].get("id")
            meta_attr_value_node = meta_item.get("value")
            meta_attr_value = None

            if meta_attr_value_node:
                if meta_attr_value_node.get("_nodetype") == "List":
                    meta_attr_value = []
                    for elt in meta_attr_value_node.get("elts", []):
                        if elt.get("_nodetype") == "Constant":
                            meta_attr_value.append(elt.get("value"))
                elif meta_attr_value_node.get("_nodetype") == "Constant":
                    meta_attr_value = meta_attr_value_node.get("value")

            if meta_attr_name and meta_attr_value is not None:
                meta_info[meta_attr_name] = meta_attr_value
    return meta_info


def transform_models_py(models_py_ast):
    """Transform Django models.py AST into a structured format."""
    output = {"models": []}
    declarations = models_py_ast.get("ast_declarations", [])

    for dec in declarations:
        if dec.get("_nodetype") != "ClassDef":
            continue

        # Check if it's a Django model
        is_django_model = False
        for base in dec.get("bases", []):
            if (
                base.get("_nodetype") == "Attribute"
                and base.get("value", {}).get("id") == "models"
                and base.get("attr") == "Model"
            ):
                is_django_model = True
                break

        if not is_django_model:
            continue

        model_info = {
            "name": dec.get("name"),
            "docstring": None,
            "fields": [],
            "methods": [],
            "properties": [],
            "meta": {},
        }

        class_level_vars = {}
        class_level_choices_vars = {}

        # Extract docstring
        if dec.get("body") and dec["body"][0].get("_nodetype") == "Expr":
            docstring_node = dec["body"][0].get("value")
            if docstring_node and docstring_node.get("_nodetype") == "Constant":
                model_info["docstring"] = docstring_node.get("value", "").strip()

        for item in dec.get("body", []):
            if item.get("_nodetype") == "Assign":
                target_node = item.get("targets", [{}])[0]
                if target_node.get("_nodetype") == "Name":
                    target_name = target_node.get("id")
                    value_node = item.get("value")

                    # Check if it's a model field assignment
                    is_model_field = (
                        value_node
                        and value_node.get("_nodetype") == "Call"
                        and value_node.get("func", {}).get("_nodetype") == "Attribute"
                        and value_node.get("func", {}).get("value", {}).get("id")
                        == "models"
                    )

                    if is_model_field:
                        field_data = parse_model_field(
                            value_node,
                            target_name,
                            class_level_vars,
                            class_level_choices_vars,
                        )
                        model_info["fields"].append(field_data)
                    else:
                        parse_class_level_variable(
                            target_name,
                            value_node,
                            class_level_vars,
                            class_level_choices_vars,
                        )

            elif item.get("_nodetype") == "FunctionDef":
                method_name, method_args, method_body, is_property, method_docstring = (
                    parse_method_or_property(item)
                )
                if is_property:
                    model_info["properties"].append(
                        {
                            "name": method_name,
                            "body": method_body,
                            "docstring": method_docstring,
                        }
                    )
                else:
                    method_info = {
                        "name": method_name,
                        "body": method_body,
                        "docstring": method_docstring,
                    }

                    # Handle the new comprehensive argument structure
                    method_info["args"] = method_args
                    model_info["methods"].append(method_info)

            elif item.get("_nodetype") == "ClassDef" and item.get("name") == "Meta":
                model_info["meta"] = parse_meta_class(item)

        output["models"].append(model_info)

    return output
