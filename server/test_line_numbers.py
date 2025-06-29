#!/usr/bin/env python3

import json

from parsing import parse_django_file_ast
from transformation.transform_models import transform_models_py


def test_line_numbers():
    """Test that line numbers are correctly extracted for model classes."""
    # Parse the test models file
    ast_result = parse_django_file_ast("test_models.py")
    transformed = transform_models_py(ast_result)

    print("Models with line numbers:")
    print("=" * 40)

    for model in transformed["models"]:
        print(f"Model: {model['name']}")
        print(f"  Start Line: {model['lineno']}")
        print(f"  End Line: {model['end_lineno']}")
        print(f"  Fields: {len(model['fields'])}")
        print()

    # Pretty print the full result for debugging
    print("Full transformation result:")
    print("=" * 40)
    print(json.dumps(transformed, indent=2))


if __name__ == "__main__":
    test_line_numbers()
