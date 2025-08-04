"""
Transforms the AST of a Django serializers.py file into a structured format,
identifying serializer classes, their associated models, fields, and custom field definitions.
"""

from pathlib import Path

from openbase.core.parsing import parse_python_file_ast

from .models import (
    DjangoSerializer,
    DjangoSerializerCreateMethod,
    DjangoSerializerField,
)


def _get_node_value(node):
    """
    Recursively extracts a simple value or representation from an AST node.
    Handles Constants, Names, and Attributes (e.g., obj.attr, module.class.attr).
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
        elif attr_part:  # Should ideally not happen if value_node is valid
            return attr_part
    # Add other simple types if necessary
    return None


def _parse_fields_attribute(value_node):
    """
    Parses the value of Meta.fields or Meta.read_only_fields.
    Can be a list/tuple of strings or the string "__all__".
    """
    if not value_node:
        return []

    if value_node.get("_nodetype") == "Constant" and isinstance(
        _get_node_value(value_node), str
    ):
        return _get_node_value(value_node)  # Handles "__all__"
    elif value_node.get("_nodetype") in ["List", "Tuple"]:
        field_nodes = value_node.get("elts", [])
        return [
            _get_node_value(f_node)
            for f_node in field_nodes
            if _get_node_value(f_node) is not None
        ]
    return []


def parse_serializers_file(
    file_path: Path, app_name: str, package_name: str
) -> list[DjangoSerializer]:
    """
    Parse a Django serializers.py file and extract serializer information.
    """
    ast_declarations = parse_python_file_ast(file_path)
    if not ast_declarations:
        return []

    output_serializers = []

    for declaration in ast_declarations:
        if declaration.get("_nodetype") == "ClassDef":
            class_name = declaration.get("name")
            base_classes = [
                _get_node_value(base) for base in declaration.get("bases", [])
            ]

            # Check if it's a DRF Serializer class
            is_serializer_class = any(
                bc
                and (
                    "serializers.ModelSerializer" in bc
                    or "serializers.Serializer" in bc
                    or "BaseModelSerializer" in bc
                )
                for bc in base_classes
            )

            if not is_serializer_class:
                continue

            serializer_info = {
                "name": class_name,
                "model": None,
                "fields": [],
                "read_only_fields": [],
                "custom_fields": [],
                "create_method": None,
                "path": file_path,
                "app_name": app_name,
                "package_name": package_name,
            }

            # Process serializer class body for Meta class and custom fields
            for item in declaration.get("body", []):
                if item.get("_nodetype") == "ClassDef" and item.get("name") == "Meta":
                    # Parse Meta class attributes
                    for meta_assign in item.get("body", []):
                        if meta_assign.get("_nodetype") == "Assign":
                            target_name = meta_assign.get("targets", [{}])[0].get("id")
                            value_node = meta_assign.get("value")

                            if target_name == "model":
                                serializer_info["model"] = _get_node_value(value_node)
                            elif target_name == "fields":
                                fields_value = _parse_fields_attribute(value_node)
                                if isinstance(fields_value, str):
                                    serializer_info["fields"] = [fields_value]
                                else:
                                    serializer_info["fields"] = fields_value
                            elif target_name == "read_only_fields":
                                serializer_info["read_only_fields"] = (
                                    _parse_fields_attribute(value_node)
                                )

                elif (
                    item.get("_nodetype") == "Assign"
                ):  # Custom/related serializer fields
                    target_node = item.get("targets", [{}])[0]
                    if target_node.get("_nodetype") == "Name":
                        field_name = target_node.get("id")
                        value_node = item.get("value")

                        if value_node.get("_nodetype") == "Call":
                            field_serializer_class = _get_node_value(
                                value_node.get("func")
                            )
                            arguments = {}
                            for kw in value_node.get("keywords", []):
                                arg_name = kw.get("arg")
                                arg_val_node = kw.get("value")
                                arg_value = _get_node_value(arg_val_node)
                                if (
                                    arg_name is not None
                                ):  # arg_value can be None (e.g. default=None)
                                    arguments[arg_name] = arg_value

                            serializer_info["custom_fields"].append(
                                DjangoSerializerField(
                                    name=field_name,
                                    serializer_class=field_serializer_class,
                                    arguments=arguments,
                                )
                            )

                elif (
                    item.get("_nodetype") == "FunctionDef"
                    and item.get("name") == "create"
                ):
                    # Extract both body source and docstring for create method
                    serializer_info["create_method"] = DjangoSerializerCreateMethod(
                        body=item.get("body_source", ""),
                        docstring=item.get("docstring"),
                    )

            output_serializers.append(DjangoSerializer(**serializer_info))

    return output_serializers
