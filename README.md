# mealie-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that exposes
[Mealie](https://mealie.io) — a self-hosted recipe manager — as a set of
LLM-callable tools. It lets Claude Desktop / Claude Code (or any MCP client)
search recipes, fetch details, manage the meal plan, and edit shopping lists on
your own Mealie instance.

## Tools

| Tool | Purpose |
| --- | --- |
| `search_recipes(query?, tags?, limit?)` | Search recipes; returns slug, name, description, tags, categories |
| `get_recipe(slug)` | Full recipe JSON for a slug |
| `list_meal_plan(start_date, end_date)` | Meal plan entries between two ISO dates |
| `list_shopping_lists()` | IDs and names of all shopping lists |
| `add_shopping_list_items(list_id, items[])` | Add free-text items to a list |
| `create_recipe(name)` | Create a blank recipe; returns generated slug |
| `create_meal_plan_entry(date, entry_type, recipe_slug?, title?)` | Add a meal plan entry |

## Requirements

- Python 3.11+
- A running Mealie instance (self-hosted; tested against Mealie v2)
- A long-lived Mealie API token (User Profile → API Tokens in the Mealie UI)

## Configuration

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `MEALIE_URL` | yes | — | Base URL of your Mealie instance (e.g. `http://localhost:9011` or `http://mealie:9000`) |
| `MEALIE_API_TOKEN` | yes | — | Long-lived bearer token from Mealie |
| `MCP_TRANSPORT` | no | `sse` | `stdio` for local subprocess, `sse` for HTTP streaming |
| `MCP_HOST` | no | `0.0.0.0` | Bind address for SSE mode |
| `MCP_PORT` | no | `8000` | Bind port for SSE mode |

## Install & run locally

```bash
pip install -e .
# stdio mode (for Claude Desktop):
MCP_TRANSPORT=stdio mealie-mcp
# sse/http mode (for docker-compose / remote clients):
MCP_TRANSPORT=sse MCP_PORT=8000 mealie-mcp
```

The SSE endpoint is served at `http://<host>:<port>/sse`.

## Claude Desktop configuration

Edit Claude Desktop's config file (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "mealie": {
      "command": "mealie-mcp",
      "env": {
        "MEALIE_URL": "http://localhost:9011",
        "MEALIE_API_TOKEN": "paste-your-token-here",
        "MCP_TRANSPORT": "stdio"
      }
    }
  }
}
```

If `mealie-mcp` is not on your `PATH`, point `command` at the full binary path
(e.g. `/Users/you/.venvs/mealie-mcp/bin/mealie-mcp`) or invoke via Python:

```json
{
  "mcpServers": {
    "mealie": {
      "command": "python",
      "args": ["-m", "mealie_mcp"],
      "env": {
        "MEALIE_URL": "http://localhost:9011",
        "MEALIE_API_TOKEN": "paste-your-token-here",
        "MCP_TRANSPORT": "stdio"
      }
    }
  }
}
```

Restart Claude Desktop after editing the config.

## Docker / docker-compose

A `Dockerfile` is included. To run standalone:

```bash
docker build -t mealie-mcp .
docker run --rm -p 8765:8000 \
  -e MEALIE_URL=http://host.docker.internal:9011 \
  -e MEALIE_API_TOKEN=your-token \
  mealie-mcp
```

To add the server to an existing Mealie stack, see
[`docker-compose.snippet.yml`](./docker-compose.snippet.yml):

```yaml
services:
  mealie-mcp:
    build:
      context: ./mealie-mcp
    depends_on:
      - mealie
    environment:
      MEALIE_URL: "http://mealie:9000"
      MEALIE_API_TOKEN: "${MEALIE_API_TOKEN}"
      MCP_TRANSPORT: "sse"
      MCP_HOST: "0.0.0.0"
      MCP_PORT: "8000"
    ports:
      - "8765:8000"
```

Point your MCP client at `http://<docker-host>:8765/sse`.

## Testing the server manually

With the server running in SSE mode, confirm it is reachable:

```bash
curl -N http://localhost:8000/sse
```

You should see an SSE event stream open. For stdio mode, Claude Desktop handles
the handshake — there is no HTTP endpoint.

## Project layout

```
mealie-mcp/
├── pyproject.toml
├── Dockerfile
├── docker-compose.snippet.yml
├── .env.example
├── README.md
└── src/mealie_mcp/
    ├── __init__.py
    ├── __main__.py        # CLI entry point (loads .env, dispatches transport)
    ├── server.py          # FastMCP server + tool definitions
    └── client.py          # Async httpx wrapper for the Mealie REST API
```

## License

MIT
