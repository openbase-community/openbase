import ast


class IRNode:
    original_node: ast.AST

    def __init__(self, original_node: ast.AST):
        self.original_node = original_node
