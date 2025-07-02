from .utils import extract_function_info


def transform_tasks_py(tasks_py_ast):
    """
    Transform a tasks.py AST into a more readable format focusing on taskiq tasks.
    """
    output = {"tasks": []}
    declarations = tasks_py_ast.get("ast_declarations", [])

    for dec in declarations:
        # Look for both regular and async function definitions
        if dec.get("_nodetype") in ["FunctionDef", "AsyncFunctionDef"]:
            # Check if it's decorated with @broker.task
            for decorator in dec.get("decorator_list", []):
                if (
                    decorator.get("_nodetype") == "Attribute"
                    and decorator.get("attr") == "task"
                    and decorator.get("value", {}).get("_nodetype") == "Name"
                    and decorator.get("value", {}).get("id") == "broker"
                ):
                    # This is a taskiq task, extract its information
                    task_info = extract_function_info(dec)
                    output["tasks"].append(task_info)
                    break  # No need to check other decorators

    return output
