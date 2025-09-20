from __future__ import annotations


class GenerationCommand:
    def __init__(self, name: str, description: str, command: Callable):
        self.name = name
        self.description = description
        self.command = command

    def execute(self):
        self.command()
