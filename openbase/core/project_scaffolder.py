from __future__ import annotations

import json
import logging
import os
import secrets
from typing import TYPE_CHECKING

from vscode_multi.sync import sync

from openbase.core.default_env import make_default_env
from openbase.core.git_helpers import (
    create_github_repo,
    create_initial_commit,
    get_github_user,
    init_git_repo,
)
from openbase.core.template_manager import TemplateManager

if TYPE_CHECKING:
    from openbase.core.paths import ProjectPaths
    from openbase.core.project_config import ProjectConfig


logger = logging.getLogger(__name__)

setup_script_contents = """
#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${{BASH_SOURCE[0]}}" )" && pwd )"
ROOT_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"

# Clone all repos
pushd "$ROOT_DIR"
multi sync
popd

# Set up Python workspace dependencies
cat > ${{ROOT_DIR}}/web/workspace_requirements.txt << EOF
-e ../{project_name_kebab}-api
EOF

# Call the web setup script
pushd ${{ROOT_DIR}}/web
./scripts/setup
popd

# Call the React install scripts
pushd ${{ROOT_DIR}}/{project_name_kebab}-react
npm install
popd
pushd ${{ROOT_DIR}}/react-shared
npm install
popd

# Link the react-shared package
pushd ${{ROOT_DIR}}/react-shared
npm link
popd
pushd ${{ROOT_DIR}}/{project_name_kebab}-react
npm link openbase-react-shared
popd

echo "Setup complete! Please restart your IDE, then you can run your project with the VS Code run button."
""".strip()

gitignore_contents = """
.env
data/
.claude/settings.local.json
.DS_Store

# Generated files
.vscode/launch.json
.vscode/settings.json
.vscode/tasks.json
.vscode/extensions.json
ruff.toml
CLAUDE.md
"""


class ProjectScaffolder:
    def __init__(
        self,
        paths: ProjectPaths,
        config: ProjectConfig,
        *,
        with_frontend: bool = True,
        with_github: bool = False,
    ):
        self.paths = paths
        self.config = config

        self.template_manager = TemplateManager(
            paths=paths,
            config=config,
        )
        self.with_frontend = with_frontend
        self.with_github = with_github

    def create_multi_json(self):
        multi_json_path = self.paths.root_dir / "multi.json"
        github_user = get_github_user()
        multi_config = {
            "repos": [
                {"url": "https://github.com/openbase-community/web"},
                {
                    "url": f"https://github.com/{github_user}/{self.config.api_package_name_snake}"
                },
            ]
        }

        if self.with_frontend:
            multi_config["repos"] += [
                {
                    "url": f"https://github.com/{github_user}/{self.config.project_name_kebab}-react"
                },
                {"url": "https://github.com/openbase-community/react-shared"},
            ]
            logger.debug(multi_config)

        with multi_json_path.open("w") as f:
            json.dump(multi_config, f, indent=2)

        logger.info(f"Created multi.json at {multi_json_path}")

    def create_setup_script(self):
        setup_script_path = self.paths.root_dir / "scripts" / "setup.sh"
        setup_script_path.parent.mkdir(parents=True, exist_ok=True)
        with setup_script_path.open("w") as f:
            f.write(
                setup_script_contents.format(
                    project_name_snake=self.config.project_name_snake,
                    project_name_kebab=self.config.project_name_kebab,
                )
            )

        # chmod +x
        setup_script_path.chmod(0o755)

    def create_settings_shared_json(self):
        settings_shared_json_path = (
            self.paths.root_dir / ".vscode" / "settings.shared.json"
        )
        settings_shared_json_path.parent.mkdir(exist_ok=True)

        settings_shared_json_contents = {
            "reloadFlags": f"--reload-dir ${{workspaceFolder}}/{self.config.api_package_name_snake}"
        }

        with settings_shared_json_path.open("w") as f:
            json.dump(settings_shared_json_contents, f, indent=2)

    def create_gitignore(self):
        gitignore_path = self.paths.root_dir / ".gitignore"
        with gitignore_path.open("w") as f:
            f.write(gitignore_contents)

        logger.info(f"Created .gitignore at {gitignore_path}")

    def init_with_boilersync_and_git(self):
        logger.info("Initializing Openbase project...")

        self.template_manager.update_and_init_all()

        # Create multi.json file
        self.create_multi_json()

        # Create setup script
        self.create_setup_script()

        # Create the GitHub repo if it doesn't exist
        if self.with_github:
            logger.info(
                f"Creating GitHub repository {self.config.project_name_kebab} if not exists..."
            )
            create_github_repo(self.config.project_name_kebab)

        # Create various root files
        self.create_settings_shared_json()
        self.create_gitignore()

        # Run multi sync
        logger.info("Syncing multi-repository workspace...")
        sync(root_dir=self.paths.root_dir, ensure_on_same_branch=False)

        # Create .env file if env variable is set
        dot_env_symlink_source = os.getenv("DOT_ENV_SYMLINK_SOURCE")
        if dot_env_symlink_source:
            dot_env_symlink_target = self.paths.root_dir / "web" / ".env"
            dot_env_symlink_target.symlink_to(dot_env_symlink_source)
        else:
            openbase_secret_key = secrets.token_hex(64)
            openbase_api_token = secrets.token_hex(64)

            dot_env_contents = make_default_env(
                package_name_snake=self.config.project_name_snake,
                package_name_url_prefix=self.config.api_prefix,
                openbase_secret_key=openbase_secret_key,
                openbase_api_token=openbase_api_token,
            )
            dot_env_target = self.paths.root_dir / "web" / ".env"
            dot_env_target.write_text(dot_env_contents)

        # Initialize root git repository
        logger.info("Initializing git repository...")
        init_git_repo(self.paths.root_dir)

        # Create an initial git commit after syncing
        logger.info("Creating initial git commit...")
        create_initial_commit(self.paths.root_dir)

        logger.info("Openbase project initialized successfully!")
