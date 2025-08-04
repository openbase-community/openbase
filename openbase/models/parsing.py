from pathlib import Path

from openbase.core.parsing import parse_python_file_ast
from openbase.core.parsing_utils import extract_function_info


def get_field_arg_name(field_type, arg_index):
    """
    Get the kwarg name for a positional argument based on field type and position.

    Args:
        field_type: The type of the field (e.g. "models.CharField")
        arg_index: The position of the argument (0-based)

    Returns:
        The name of the kwarg this positional arg maps to
    """
    # Special cases for different field types - check these first
    if field_type == "models.ForeignKey" or field_type == "models.OneToOneField":
        if arg_index == 0:
            return "to"
        elif arg_index == 1:
            return "on_delete"
    elif field_type == "models.ManyToManyField":
        if arg_index == 0:
            return "to"
    elif field_type == "models.CharField" or field_type == "models.TextField":
        if arg_index == 0:
            return "verbose_name"
        elif arg_index == 1:
            return "max_length"
    elif field_type == "models.DecimalField":
        if arg_index == 0:
            return "verbose_name"
        elif arg_index == 1:
            return "max_digits"
        elif arg_index == 2:
            return "decimal_places"
    else:
        # Common first argument for most other fields is 'verbose_name'
        if arg_index == 0:
            return "verbose_name"

    # For unknown field types or positions, use a generic name
    return f"arg_{arg_index}"


def parse_model_field(
    value_node, target_name, class_level_vars, class_level_choices_vars
):
    """Parse a Django model field assignment and return the field information."""
    field_type = f"models.{value_node['func']['attr']}"
    field_kwargs = {}
    processed_choices = None

    # Convert positional args to kwargs
    for i, arg_node in enumerate(value_node.get("args", [])):
        arg_value = None
        if arg_node.get("_nodetype") == "Name":
            arg_value = arg_node.get("id")
        elif arg_node.get("_nodetype") == "Constant":
            arg_value = arg_node.get("value")

        if arg_value is not None:
            kwarg_name = get_field_arg_name(field_type, i)
            field_kwargs[kwarg_name] = arg_value

    # Handle explicit kwargs
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

        # Filter out 'self' from regular_args
        args = func_info["args"]["regular_args"]
        if args and args[0] == "self":
            args = args[1:]

        return (
            func_info["name"],
            {
                "args": args,
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


def parse_models_file(path: Path):
    declarations = parse_python_file_ast(path)

    models = []

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
            "lineno": dec.get("lineno"),
            "end_lineno": dec.get("end_lineno"),
            "docstring": None,
            "fields": [],
            "methods": [],
            "properties": [],
            "meta": {},
            "save_method": None,
            "str_method": None,
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
                method_name = item.get("name")

                # Handle special methods
                if method_name == "save":
                    model_info["save_method"] = {
                        "body": item.get("body_source", "").strip(),
                        "docstring": item.get("docstring", "").strip(),
                    }
                elif method_name == "__str__":
                    model_info["str_method"] = {
                        "body": item.get("body_source", "").strip(),
                    }
                else:
                    # Handle regular methods and properties
                    (
                        method_name,
                        method_args,
                        method_body,
                        is_property,
                        method_docstring,
                    ) = parse_method_or_property(item)
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
                            "args": method_args,
                        }
                        model_info["methods"].append(method_info)

            elif item.get("_nodetype") == "ClassDef" and item.get("name") == "Meta":
                model_info["meta"] = parse_meta_class(item)

        models.append(model_info)

    return models
