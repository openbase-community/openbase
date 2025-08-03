from dataclasses import dataclass, fields
from pathlib import Path

from rest_framework.fields import ValidationError

from .parsing import SourceMappedString


@dataclass
class SourceMappedDataclass:
    """Base class for dataclasses that tracks initial values and detects changes."""

    path: Path

    def __post_init__(self):
        """Store initial values after dataclass initialization."""
        self._initial_values = {}
        for field in fields(self):
            self._initial_values[field.name] = getattr(self, field.name)

    def _extract_text_from_range(
        self, lineno: int, col_offset: int, end_lineno: int, end_col_offset: int
    ) -> str:
        """Extract text from file at the given line/column range."""
        file_path = Path(self.path)
        source_lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)

        if lineno == end_lineno:
            # Single line range
            line = source_lines[lineno - 1]  # Convert to 0-based indexing
            return line[col_offset:end_col_offset]
        else:
            # Multi-line range
            extracted_text = ""

            # First line: from col_offset to end of line
            first_line = source_lines[lineno - 1]
            extracted_text += first_line[col_offset:]

            # Middle lines: complete lines
            for line_idx in range(lineno, end_lineno - 1):
                extracted_text += source_lines[line_idx]

            # Last line: from start to end_col_offset
            last_line = source_lines[end_lineno - 1]
            extracted_text += last_line[:end_col_offset]

            return extracted_text

    def _replace_text_in_range(
        self,
        lineno: int,
        col_offset: int,
        end_lineno: int,
        end_col_offset: int,
        new_text: str,
    ):
        """Replace text in file at the given line/column range with new text."""
        file_path = Path(self.path)
        source_lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)

        if lineno == end_lineno:
            # Single line replacement
            line = source_lines[lineno - 1]  # Convert to 0-based indexing
            new_line = line[:col_offset] + new_text + line[end_col_offset:]
            source_lines[lineno - 1] = new_line
        else:
            # Multi-line replacement
            first_line = source_lines[lineno - 1]
            last_line = source_lines[end_lineno - 1]

            # Create the new line that combines first line prefix + new text + last line suffix
            new_line = first_line[:col_offset] + new_text + last_line[end_col_offset:]

            # Replace the range with a single line
            source_lines[lineno - 1 : end_lineno] = [new_line]

        # Write the modified content back to file
        file_path.write_text("".join(source_lines), encoding="utf-8")

    def save(self):
        """Detect changes and write them back to the source file."""
        changed_fields = []
        for field in fields(self):
            current_value = getattr(self, field.name)
            initial_value = self._initial_values.get(field.name)
            if current_value != initial_value:
                changed_fields.append(
                    {
                        "field": field.name,
                        "initial": initial_value,
                        "current": current_value,
                    }
                )

        if changed_fields:
            print(f"Changes detected in {self.__class__.__name__}:")

            # Apply changes in reverse line order to avoid coordinate shifts
            changes_to_apply = []

            for change in changed_fields:
                # If the initial value is a SourceMappedString, verify and modify the file
                if (
                    isinstance(change["initial"], SourceMappedString)
                    and hasattr(change["initial"], "ast_node")
                    and change["initial"].ast_node
                ):
                    node = change["initial"].ast_node
                    lineno = getattr(node, "lineno")
                    col_offset = getattr(node, "col_offset")
                    end_lineno = getattr(node, "end_lineno")
                    end_col_offset = getattr(node, "end_col_offset")
                    location_info = f"line {lineno}, col {col_offset}"
                    location_info += f" to line {end_lineno}, col {end_col_offset}"

                    print(
                        f"  {change['field']}: {change['initial']} -> {change['current']} (from {location_info})"
                    )

                    # Extract current text from file at the specified range
                    current_file_text = self._extract_text_from_range(
                        lineno, col_offset, end_lineno, end_col_offset
                    )
                    new_text = str(change["current"])
                    initial_text = str(change["initial"])

                    # If the initial value starts with a quote, preserve the quote style
                    if initial_text and current_file_text[0] in ('"', "'"):
                        quote_char = current_file_text[0]
                        # Wrap the new text in the same type of quotes
                        new_text = f"{quote_char}{new_text}{quote_char}"
                        initial_text = f"{quote_char}{initial_text}{quote_char}"

                    # Verify the file text matches the initial value
                    if current_file_text != initial_text:
                        raise RuntimeError(
                            f"Source file mismatch for field '{change['field']}': "
                            f"Expected '{initial_text}' but found '{current_file_text}' "
                            f"at {location_info}. File may have been modified externally."
                        )

                    # Store change info for application (we'll apply in reverse order)
                    changes_to_apply.append(
                        {
                            "lineno": lineno,
                            "col_offset": col_offset,
                            "end_lineno": end_lineno,
                            "end_col_offset": end_col_offset,
                            "new_text": new_text,
                            "field": change["field"],
                        }
                    )
                else:
                    raise ValidationError(
                        "Cannot change this attribute as it is not mapped to source."
                    )

            # Apply changes in reverse line order to avoid coordinate invalidation
            # Sort by line number (descending), then by column offset (descending)
            changes_to_apply.sort(
                key=lambda x: (x["lineno"], x["col_offset"]), reverse=True
            )

            for change_info in changes_to_apply:
                self._replace_text_in_range(
                    change_info["lineno"],
                    change_info["col_offset"],
                    change_info["end_lineno"],
                    change_info["end_col_offset"],
                    change_info["new_text"],
                )
                print(
                    f"  Applied change to field '{change_info['field']}' in source file"
                )

            print(f"All changes written to {self.path}")
        else:
            print(f"No changes detected in {self.__class__.__name__}")
