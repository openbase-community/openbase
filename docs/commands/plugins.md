# plugins

Manage Openbase plugins installed into the local CLI runtime.

In the apps: plugin-provided console pages appear in the
[desktop app](../desktop-app.md) and [console](../console.md) sidebar once
installed; plugin-provided skills show up on the Skills page.

## Usage

```bash
openbase-coder plugins COMMAND [ARGS]
```

## Subcommands

| Subcommand | Description |
|---|---|
| `add SOURCE [--ref REF]` | Install a plugin from local repo path or GitHub URL |
| `list` | List installed plugins |
| `show PLUGIN_ID` | Show a plugin's declared capabilities |
| `remove PLUGIN_ID` | Uninstall a plugin |
| `update [PLUGIN_ID] [--ref REF]` | Update one plugin or all plugins |
| `bootstrappers` | List all discovered bootstrapper names |

## Source Types

### Local repo path

```bash
openbase-coder plugins add ~/code/my-openbase-plugin
```

- Installed editable (`-e`) into the CLI Python environment
- Useful for active plugin development

### GitHub URL

```bash
openbase-coder plugins add https://github.com/org/openbase-plugin
openbase-coder plugins add https://github.com/org/openbase-plugin --ref main
```

- Cloned under `~/.openbase/plugins/sources/`
- Installed pinned to resolved commit SHA

## Where Plugin Packages Install

In development installs, plugin Python packages install into the workspace CLI
venv, which persists on its own.

In standalone installs, the versioned runtime package is replaced wholesale on
upgrade, so plugin packages install into the stable plugin site directory
`~/.openbase/plugins/site` instead. Every Openbase Coder process adds that
directory to `sys.path` at startup, so desktop package upgrades do not lose
installed plugins.

## What Happens on Add/Update/Remove

Mutating plugin commands will:

1. Update plugin registry and requirements under `~/.openbase/plugins/`
2. Sync plugin-declared Claude skills into `~/.claude/skills`
3. Copy each console page's `asset_dir` into `~/.openbase/plugins/console-assets/`
   and regenerate the runtime console page registry
4. Restart managed launchd services

## Plugin Declaration Model

Plugins are Python packages discovered via entry points in:

```toml
[project.entry-points."openbase_coder.plugins"]
my_plugin = "my_plugin.spec:get_plugin_spec"
```

The entry point returns a plugin spec dict containing declarations such as:

- `bootstrappers`
- `stacks`
- `console_pages`
- `skills`
- `django_url_modules`

### Console pages

Plugin console pages are **iframe-only**: a page declares a directory of
prebuilt static assets, and the console renders its entrypoint in an iframe.
This works identically in development and standalone installs, requires no
console rebuild, and needs no Node/npm at install time.

```python
{
    "console_pages": [
        {
            "key": "dashboard",          # required, unique per plugin
            "title": "Dashboard",        # optional, defaults to key
            "route": "/dashboard/plugins/<plugin>/<key>",  # optional default
            "sidebar": True,             # optional, defaults to True
            "asset_dir": "web",          # required: prebuilt static assets
            "entrypoint": "index.html",  # optional, defaults to index.html
        }
    ]
}
```

The CLI copies `asset_dir` into `~/.openbase/plugins/console-assets/` and serves
it under `/openbase-plugin-assets/<plugin>/<page>/`. The console and desktop
apps discover pages at runtime via `/api/plugins/console-registry/` and add
them to the sidebar without rebuilding.

Routes must start with `/dashboard`.

### Removed capabilities

React component console pages (`render: "component"`, `import_module`,
`export`), `project_views`, and `console_npm_packages` are no longer
supported. Plugins that declare them fail validation with clear errors;
expose plugin UI as iframe `console_pages` instead.

## Collision Rules

Install/update will fail if a plugin conflicts with existing plugins on:

- bootstrapper name
- console page key
- console page route (including built-in console routes)
