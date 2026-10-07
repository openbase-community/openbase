# Laptop Tools for Agents on Another Machine

Some tools only make sense on the machine in front of you. Computer control acts on the screen you are looking at; a browser extension's MCP server lives where the browser runs. When your agents run on another of your machines (an always-on Mac mini or a Cloud DevSpace, the *hub*), the **MCP gateway** makes those laptop-bound MCP servers available to them.

The agent stays in charge. Offering a server only adds it to the hub agent's tool list under an explicit name, such as `computer-laptop`. Nothing routes calls through the gateway automatically, wraps commands, or prefers the laptop over the hub. The agent decides when a laptop tool is the right one, and tells you where something ran.

## How it works

- **On the laptop**, you choose which MCP servers other devices may open. Each one is a local stdio MCP server listed in `~/.openbase/mcp-gateway.json`. Nothing is served until you add it.
- **On the hub**, you *offer* one of them to the hub's agents. Openbase adds an MCP server named `<name>-laptop` to its Claude Code and Codex session profiles. When an agent calls it, the hub opens an authenticated WebSocket to the laptop's Openbase runtime over your Openbase network, and the laptop starts that server's process for the session. The process stops when the session ends.
- Only devices signed in to **your** Openbase account can open a served server. The laptop accepts the same owner sign-in it accepts for every other request between your devices, and refuses any name that is not in its served list.
- If the laptop is asleep, offline, or does not serve that name, the hub's connection fails within a few seconds and the agent sees the server as unavailable instead of waiting.

## Set it up

On the laptop, serve the built-in computer-control server (or any stdio MCP command after `--`):

```bash
openbase-coder mcp-gateway serve add computer
openbase-coder mcp-gateway serve add browser -- npx some-browser-mcp
openbase-coder mcp-gateway serve list
```

On the hub, offer it to agents, naming the laptop as it appears in your device list:

```bash
openbase-coder mcp-gateway offer computer --peer my-laptop
openbase-coder mcp-gateway offered
```

New agent sessions on the hub now list `computer-laptop`. Sessions that were already running keep their old tool list until they restart.

## Commands

| Command | Where | What it does |
|---|---|---|
| `mcp-gateway serve add <name> [-- command...]` | laptop | Serve a built-in server (`computer`) or any stdio MCP command under `<name>`. Put `--description` before `<name>`. |
| `mcp-gateway serve remove <name>` | laptop | Stop serving `<name>`. Open sessions end when they disconnect. |
| `mcp-gateway serve list` | laptop | Served servers, plus built-ins that are not served. |
| `mcp-gateway offer <name> --peer <device>` | hub | Add `<name>-laptop` to this machine's agent profiles. |
| `mcp-gateway withdraw <name>` | hub | Remove `<name>-laptop` from the profiles. |
| `mcp-gateway offered` | hub | Offered servers, their device, and the profiles that list them. |
| `mcp-gateway connect <name> --peer <device>` | hub | The stdio MCP server the profiles run. Agents start it; you normally don't. Exits with code 69 when the device is unreachable. |

`offer` edits the Claude Code profile (`~/.openbase/profiles/claude/mcp.json`) and the Openbase Codex profiles that already exist. It never replaces an MCP server of your own that happens to use the same name, and `withdraw` removes only entries that `offer` created.

## Other laptop tools

When your machines run the hub/edge sync, hub agents can also reach the laptop with the `edge` command: `edge run` for display-bound commands such as `open` or `pbcopy`, `edge where` to check whether a file is present on both machines, and `edge forward` to reach a laptop port such as Chrome's DevTools port. The bundled `openbase-laptop-tools` skill teaches agents these options. As with the gateway, the agent uses them only when it decides to.

## Troubleshooting

- **The agent says `computer-laptop` is unavailable.** Check that the laptop is awake and online, that `openbase-coder mcp-gateway serve list` on the laptop shows the server, and that both machines are signed in to the same Openbase account. Running `openbase-coder mcp-gateway connect computer --peer my-laptop` on the hub prints the reason on stderr.
- **The device name is not found.** Use the name shown for the laptop in your device list (or its Openbase network host name).
- **Computer control on the laptop asks for permissions.** macOS Screen Recording and Accessibility permissions are granted on the laptop, not the hub.
