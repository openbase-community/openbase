import re
from typing import Dict, List, Optional, Union


def parse_field_definition(
    field_value: str,
) -> Dict[str, Union[str, bool, int, str, None]]:
    """
    Parse a Django model field definition string into its components.
    Example: models.CharField(max_length=100, unique=True) ->
    {
        'field_type': 'CharField',
        'max_length': 100,
        'unique': True
    }
    """
    # Extract the field type
    field_type_match = re.match(r"models\.(\w+)\(", field_value)
    if not field_type_match:
        return {"field_type": "Unknown", "raw_value": field_value}

    field_type = field_type_match.group(1)

    # Extract the arguments
    args_str = field_value[field_value.find("(") + 1 : field_value.rfind(")")]

    # Initialize the result with the field type
    result = {"field_type": field_type}

    if not args_str:
        return result

    # Handle empty parentheses
    if args_str.strip() == "":
        return result

    # Split by commas, but respect nested structures
    depth = 0
    current = []
    args = []

    for char in args_str:
        if char == "(" or char == "[" or char == "{":
            depth += 1
        elif char == ")" or char == "]" or char == "}":
            depth -= 1
        elif char == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
            continue
        current.append(char)

    if current:
        args.append("".join(current).strip())

    # Process each argument
    for arg in args:
        if "=" in arg:
            key, value = arg.split("=", 1)
            key = key.strip()
            value = value.strip()

            # Convert values to appropriate types
            if value == "True":
                value = True
            elif value == "False":
                value = False
            elif value == "None":
                value = None
            elif value.isdigit():
                value = int(value)
            elif value.startswith("[") and value.endswith("]"):
                # Handle lists/choices - keep as string for now
                value = value
            elif value.startswith("'") or value.startswith('"'):
                # Strip quotes from strings
                value = value[1:-1]

            result[key] = value
        else:
            # Handle positional arguments if needed
            result[f"arg_{len(result)}"] = arg.strip()

    return result


def transform_model_data(
    raw_model_data: Dict,
) -> Optional[Dict[str, Union[str, List, Dict]]]:
    """
    Transform raw AST model data into a more structured format.
    Returns None for Meta classes and Manager classes.
    """
    # Skip Meta classes
    if raw_model_data["name"] == "Meta":
        return None

    transformed = {
        "name": raw_model_data["name"],
        "docstring": raw_model_data["docstring"] or "",
        "base_classes": raw_model_data["bases"],
        "fields": {},
        "methods": {},
        "properties": {},
        "managers": {},
        "meta": {},
    }

    # Process attributes (fields and class attributes)
    for attr in raw_model_data["attributes"]:
        name = attr["name"]
        value = attr["value"]

        # Check if it's a Django model field
        if "models." in value:
            transformed["fields"][name] = parse_field_definition(value)
        else:
            # Class-level attribute
            transformed["meta"][name] = value

    # Process methods
    for method in raw_model_data["methods"]:
        name = method["name"]
        args = method["args"]
        docstring = method["docstring"] or ""

        # Special handling for properties (no args except self)
        if len(args) == 1 and args[0] == "self":
            transformed["properties"][name] = {"docstring": docstring}
        else:
            transformed["methods"][name] = {"args": args, "docstring": docstring}

    # Detect if this is a manager class
    if any(base == "models.Manager" for base in transformed["base_classes"]):
        return None  # Skip manager classes as they're typically internal

    return transformed


def transform_models_file(raw_ast_data: Dict) -> Dict[str, Union[str, Dict[str, Dict]]]:
    """
    Transform an entire models.py file AST data into a structured format.
    """
    models = {}

    for item in raw_ast_data["items"]:
        transformed = transform_model_data(item)
        if transformed:
            models[transformed["name"]] = transformed

    return {"file_path": raw_ast_data["file_path"], "models": models}
