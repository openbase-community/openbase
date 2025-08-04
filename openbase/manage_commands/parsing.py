from pathlib import Path

from openbase.core.parsing import parse_python_file_ast
from openbase.manage_commands.models import ManageCommand


def parse_add_argument_call(call_node):
    """
    Parses an ast.Call node representing a call to parser.add_argument().
    Extracts argument names/flags and keyword arguments like help, type, default.
    """
    arg_details = {"names": [], "kwargs": {}}

    # Extract positional arguments (names/flags)
    for arg in call_node.get("args", []):
        if arg.get("_nodetype") == "Constant":
            arg_details["names"].append(arg.get("value"))
        elif (
            arg.get("_nodetype") == "List"
        ):  # For multiple flags like ['-f', '--force']
            for elt in arg.get("elts", []):
                if elt.get("_nodetype") == "Constant":
                    arg_details["names"].append(elt.get("value"))

    # Extract keyword arguments
    for kwarg in call_node.get("keywords", []):
        kwarg_name = kwarg.get("arg")
        value_node = kwarg.get("value")
        kwarg_value = None

        if value_node:
            if value_node.get("_nodetype") == "Constant":
                kwarg_value = value_node.get("value")
            elif value_node.get("_nodetype") == "Name":
                # Could be a variable reference, e.g., type=str
                kwarg_value = value_node.get("id")
            elif value_node.get("_nodetype") == "Attribute":
                # e.g. action=argparse.SUPPRESS
                if value_node.get("value", {}).get("id") and value_node.get("attr"):
                    kwarg_value = (
                        f"{value_node.get('value').get('id')}.{value_node.get('attr')}"
                    )
                else:  # just the attr if no value id (e.g. from some other import)
                    kwarg_value = value_node.get("attr")

        if kwarg_name and kwarg_value is not None:
            arg_details["kwargs"][kwarg_name] = kwarg_value

    return arg_details


def parse_manage_command_file(path: Path) -> "ManageCommand":
    """
    Parse a manage command file into a ManageCommand instance.
    """
    declarations = parse_python_file_ast(path)
    for dec in declarations:
        if dec.get("_nodetype") == "ClassDef":
            is_command_class = False
            for base in dec.get("bases", []):
                # Checking for direct inheritance from BaseCommand or similar
                if base.get("_nodetype") == "Name" and base.get("id") == "BaseCommand":
                    is_command_class = True
                    break
                # Could also be an attribute like 'management.BaseCommand'
                # This part might need to be more robust depending on import styles
                elif (
                    base.get("_nodetype") == "Attribute"
                    and base.get("attr") == "BaseCommand"
                ):
                    is_command_class = True
                    break

            if is_command_class:
                command_info = {
                    "help": "",
                    "arguments": [],
                    "handle_body_source": "",  # New field for handle method's body
                }

                for item in dec.get("body", []):
                    # Extract help text
                    if item.get("_nodetype") == "Assign":
                        for target in item.get("targets", []):
                            if (
                                target.get("_nodetype") == "Name"
                                and target.get("id") == "help"
                            ):
                                value_node = item.get("value")
                                if (
                                    value_node
                                    and value_node.get("_nodetype") == "Constant"
                                ):
                                    command_info["help"] = value_node.get(
                                        "value", ""
                                    )  # TODO: Strip
                                break

                    # Extract arguments from add_arguments method
                    elif (
                        item.get("_nodetype") == "FunctionDef"
                        and item.get("name") == "add_arguments"
                    ):
                        # The first argument to add_arguments is typically 'self', the second is 'parser'
                        parser_name = None
                        if len(item.get("args", {}).get("args", [])) > 1:
                            parser_arg_node = item["args"]["args"][1]
                            if parser_arg_node.get("_nodetype") == "arg":
                                parser_name = parser_arg_node.get("arg")

                        if parser_name:
                            for node in item.get(
                                "body", []
                            ):  # Iterate through statements in add_arguments
                                if (
                                    node.get("_nodetype") == "Expr"
                                ):  # e.g. parser.add_argument(...)
                                    call_node = node.get("value")
                                    # Check if it's a call to parser.add_argument
                                    if (
                                        call_node
                                        and call_node.get("_nodetype") == "Call"
                                        and call_node.get("func", {}).get("_nodetype")
                                        == "Attribute"
                                        and call_node["func"].get("attr")
                                        == "add_argument"
                                        and call_node["func"]
                                        .get("value", {})
                                        .get("_nodetype")
                                        == "Name"
                                        and call_node["func"]["value"].get("id")
                                        == parser_name
                                    ):
                                        arg_data = parse_add_argument_call(call_node)
                                        command_info["arguments"].append(arg_data)
                    # Extract body_source from handle method
                    elif (
                        item.get("_nodetype") == "FunctionDef"
                        and item.get("name") == "handle"
                    ):
                        command_info["handle_body_source"] = item.get(
                            "body_source", ""
                        ).strip()

                return ManageCommand(
                    path=path,
                    arguments=command_info["arguments"],
                    handle_body_source=command_info["handle_body_source"],
                    help=command_info["help"],
                )

    # Return a basic ManageCommand if no command class found
    return None
