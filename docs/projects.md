# Projects

Openbase Coder discovers projects automatically from agent working directories when threads synchronize. Directories within a Multi workspace appear under that workspace's root. Discovery registers existing folders; it does not create folders or scaffold an app.

To create and immediately register a project on the current computer:

```bash
openbase-coder projects create "$HOME/Projects/my-app"
```

The command creates missing parent directories and prints the absolute project root to use as the worker's working directory. Existing folder contents are preserved. It does not initialize Git or publish a repository. If the folder is inside a Multi workspace, it prints and registers that workspace root.

Use `openbase-coder projects add PATH` to register an existing directory and `openbase-coder projects list` to print registered and automatically discovered projects as JSON. Explicit registration also restores projects previously hidden from the list. Use a dedicated project folder, rather than your home root or a system directory. Commands operate on the computer running the CLI.
