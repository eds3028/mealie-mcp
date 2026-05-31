# mealie-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that exposes
[Mealie](https://mealie.io) — a self-hosted recipe manager — as a set of
LLM-callable tools. It lets Claude Desktop / Claude Code (or any MCP client)
search recipes, create and edit them, manage the meal plan, work with shopping
lists, and organize cookbooks on your own Mealie instance.

## Tools

### Recipes

| Tool | Purpose |
| --- | --- |
| `search_recipes(query?, tags?, limit?)` | Search recipes; returns slug, name, description, tags, categories |
| `get_recipe(slug)` | Full recipe JSON for a slug |
| `create_recipe(name, description?, recipe_yield?, recipe_servings?, prep_time?, cook_time?, total_time?, ingredients?, instructions?, notes?, tags?, categories?, tools?)` | Create a recipe fully populated in one call. Lines starting with `#`/`##`/`###` in `ingredients`/`instructions` become section headers. Tags, categories, and tools are auto-created if missing. |
| `update_recipe(slug, ...same fields as create_recipe)` | Patch an existing recipe; only provided fields change |
| `import_recipe_from_url(url)` | Scrape and import a recipe from an external URL |
| `delete_recipe(slug)` | **Destructive.** Permanently delete a recipe |
| `set_recipe_image_from_url(slug, url)` | Upload a recipe image by downloading it from a URL |
| `set_recipe_image_from_base64(slug, image_data, content_type?)` | Upload a base64-encoded image (e.g. AI-generated). Accepts `data:<mime>;base64,...` URIs |

### Tags, categories, tools

| Tool | Purpose |
| --- | --- |
| `list_tags()` | List all tags |
| `set_recipe_tags(slug, tags[])` | Replace a recipe's tags (auto-creates missing tags; pass `[]` to clear) |
| `list_categories()` | List all recipe categories |
| `set_recipe_categories(slug, categories[])` | Replace a recipe's categories (auto-creates; pass `[]` to clear) |
| `list_recipe_tools()` | List all recipe tools/equipment |
| `set_recipe_tools(slug, tools[])` | Replace a recipe's tools/equipment (auto-creates; pass `[]` to clear) |
| `list_foods(query?, limit?)` | List foods/ingredients known to Mealie |

### Meal plan

| Tool | Purpose |
| --- | --- |
| `list_meal_plan(start_date, end_date)` | Meal plan entries between two ISO dates (inclusive) |
| `get_todays_meal_plan()` | Today's meal plan entries |
| `create_meal_plan_entry(date, entry_type, recipe_slug?, title?)` | Add a meal plan entry. `entry_type` is one of `breakfast`, `lunch`, `dinner`, `side`. Provide `recipe_slug` to link a recipe or `title` for a free-text entry. |
| `delete_meal_plan_entry(entry_id)` | **Destructive.** Delete a meal plan entry |

### Shopping lists

| Tool | Purpose |
| --- | --- |
| `list_shopping_lists()` | IDs and names of all shopping lists |
| `create_shopping_list(name)` | Create a new shopping list |
| `list_shopping_list_items(list_id)` | All items in a shopping list |
| `add_shopping_list_items(list_id, items[])` | Add free-text items to a list |
| `check_off_shopping_item(item_id, checked?)` | Mark a shopping list item checked or unchecked |
| `delete_shopping_list_item(item_id)` | **Destructive.** Delete a shopping list item |

### Cookbooks

| Tool | Purpose |
| --- | --- |
| `list_cookbooks()` | List all cookbooks in the household |
| `create_cookbook(name, description?, public?)` | Create a new cookbook |

## Requirements

- Python 3.11+
- A running Mealie instance (self-hosted; tested against Mealie v2)
- A long-lived Mealie API token (User Profile → API Tokens in the Mealie UI)
- (Optional) An OIDC provider such as Authentik if you want to put OAuth in
  front of the HTTP transports

## Configuration

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

### Core variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `MEALIE_URL` | yes | — | Base URL of your Mealie instance (e.g. `http://localhost:9011` or `http://mealie:9000`) |
| `MEALIE_API_TOKEN` | yes | — | Long-lived bearer token from Mealie |
| `MCP_TRANSPORT` | no | `sse` | `stdio`, `sse`, `http`, or `streamable-http` |
| `MCP_HOST` | no | `0.0.0.0` | Bind address for HTTP/SSE modes |
| `MCP_PORT` | no | `8000` | Bind port for HTTP/SSE modes |

### Transport security (HTTP/SSE only)

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `MCP_ALLOWED_HOSTS` | no | — | Comma-separated extra hostnames allowed for DNS-rebinding protection |
| `MCP_ALLOWED_ORIGINS` | no | — | Comma-separated extra origins allowed by CORS |

### OAuth2 / OIDC (optional, HTTP/SSE only)

The MCP server acts purely as an OAuth-protected resource: it advertises its
authorization server at `/.well-known/oauth-protected-resource` and validates
incoming Bearer JWTs against that issuer's JWKS. The MCP client (e.g. ChatGPT)
runs the authorization-code + PKCE flow directly with your provider — the MCP
server does not host an `/oauth/authorize`, `/oauth/callback`, or `/oauth/token`
endpoint.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `OAUTH_ISSUER_URL` | no | — | Application-specific issuer URL, e.g. `https://auth.example.com/application/o/mealie-mcp/`. Must match the `iss` claim minted into your tokens. |
| `OAUTH_CLIENT_ID` | no | — | OAuth client ID from your provider. |
| `OAUTH_CLIENT_SECRET` | no | — | Optional. Leave blank for Public/PKCE clients (recommended for ChatGPT). |
| `OAUTH_SERVER_URL` | no | — | Public URL of this MCP server including `/mcp`, e.g. `https://mealiemcp.example.com/mcp`. |

OAuth is enabled when `OAUTH_ISSUER_URL`, `OAUTH_CLIENT_ID`, and
`OAUTH_SERVER_URL` are all set. With OAuth enabled, requests to `/mcp`,
`/sse`, and `/messages` without a valid Bearer JWT receive `401` plus a
`WWW-Authenticate: Bearer resource_metadata="…"` challenge that triggers the
MCP client's discovery flow.

#### Authentik recipe

1. Create an OAuth2/OpenID Provider in Authentik.
2. Set the **Client type** to **Public**. This is what causes Authentik's
   discovery doc to advertise `none` in `token_endpoint_auth_methods_supported`,
   which ChatGPT requires for PKCE.
3. Add the redirect URI shown by your MCP client (ChatGPT's custom-connector
   setup screen will display one) to **Redirect URIs / Origins**.
4. Bind the provider to an Application; the Application slug becomes the last
   path segment of `OAUTH_ISSUER_URL`, e.g.
   `https://auth.example.com/application/o/<app-slug>/`.
5. Copy the Client ID into `OAUTH_CLIENT_ID` (and into ChatGPT's connector
   form). Leave `OAUTH_CLIENT_SECRET` blank.

To verify before pointing ChatGPT at it:

```bash
curl https://<your-mcp>/.well-known/oauth-protected-resource/mcp
# expect: {"resource":"<OAUTH_SERVER_URL>","authorization_servers":["<OAUTH_ISSUER_URL>"]}

# The root metadata URL is also served for clients that discover there:
curl https://<your-mcp>/.well-known/oauth-protected-resource

curl <OAUTH_ISSUER_URL>/.well-known/openid-configuration | jq '.issuer, .token_endpoint_auth_methods_supported'
# expect: issuer matches OAUTH_ISSUER_URL exactly, and "none" is in the methods list.
```

If `issuer` doesn't match `OAUTH_ISSUER_URL`, you've used the wrong URL (most
likely the bare host instead of the app-specific path). If `none` is missing,
your Authentik provider is still set to Confidential — change it to Public.

## Install & run locally

```bash
pip install -e .
# stdio mode (for Claude Desktop):
MCP_TRANSPORT=stdio mealie-mcp
# sse mode (for docker-compose / remote clients):
MCP_TRANSPORT=sse MCP_PORT=8000 mealie-mcp
# streamable-http mode:
MCP_TRANSPORT=streamable-http MCP_PORT=8000 mealie-mcp
```

The SSE endpoint is served at `http://<host>:<port>/sse`. A simple health
check is available at `http://<host>:<port>/health` and reports whether OAuth
is enabled.

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

With the server running in HTTP/SSE mode, confirm it is reachable:

```bash
curl -s http://localhost:8000/health
# {"status":"ok","oauth_enabled":false}

curl -N http://localhost:8000/sse
```

The first command returns a small JSON status; the second opens an SSE event
stream. For stdio mode, Claude Desktop handles the handshake — there is no
HTTP endpoint.

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
    ├── server.py          # FastMCP server + tool definitions + Bearer auth middleware
    ├── auth.py            # OAuth2/OIDC token validation against issuer JWKS
    └── client.py          # Async httpx wrapper for the Mealie REST API
```

## License

MIT
