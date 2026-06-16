"""Human-facing landing page served at ``/`` for browsers.

Agents/curl get JSON (content-negotiated in asgi.py); humans get copy-paste
instructions for pointing an MCP-capable agent (Claude Code, Claude Desktop,
Cursor, OpenClaw, …) at the remote MCP endpoint, plus the no-install grep path.
"""

from __future__ import annotations

from . import __version__, config


def landing_html(base_url: str, edition: str | None = None) -> str:
    base = base_url.rstrip("/")
    mcp_url = f"{base}/mcp/"
    edition = edition or config.CURRENT_EDITION_NAME
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vibe Camp Expansion — point your agent here</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 2rem 1.25rem 4rem;
    font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0e1014; color: #e7e9ee; max-width: 820px; margin-inline: auto;
  }}
  h1 {{ font-size: 1.9rem; margin: 0 0 .25rem; }}
  h2 {{ font-size: 1.2rem; margin: 2.2rem 0 .6rem; color: #ffd35c; }}
  p.lead {{ color: #aab1c0; margin-top: .25rem; }}
  code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  pre {{
    background: #181b22; border: 1px solid #262b35; border-radius: 10px;
    padding: .9rem 1rem; overflow-x: auto; position: relative; font-size: .9rem;
  }}
  code.inline {{ background: #181b22; padding: .1rem .35rem; border-radius: 5px; font-size: .9em; }}
  .step {{ color: #7fd1ff; font-weight: 600; }}
  .ask {{ border-left: 3px solid #ffd35c; padding: .15rem 0 .15rem .9rem; margin: .5rem 0; color: #d6dae3; }}
  a {{ color: #7fd1ff; }}
  .pill {{ display:inline-block; background:#1d2530; border:1px solid #2c3645; border-radius:999px;
          padding:.15rem .6rem; font-size:.8rem; color:#9fb0c6; margin-right:.4rem; }}
  footer {{ margin-top: 3rem; color: #6b7280; font-size: .85rem; }}
</style>
</head>
<body>
  <h1>🎪 Vibe Camp Expansion</h1>
  <p class="lead">An agent-friendly view of the Vibe Camp schedule. Point your AI at the URL
  below and just <em>talk to it</em> — "what's on at the Pool Saturday night?", "I'm into
  consciousness and AI, what should I go to?"</p>
  <p>
    <span class="pill">{edition}</span>
    <span class="pill">read-only</span>
    <span class="pill">refreshed every ~5&nbsp;min</span>
  </p>

  <h2>Claude Code — one command</h2>
  <pre>claude mcp add --transport http vibecamp {mcp_url}</pre>
  <p>Then start a chat and ask about events. That's it.</p>

  <h2>Claude Desktop · Cursor · OpenClaw · any MCP client</h2>
  <p>Add this to your MCP servers config:</p>
  <pre>{{
  "mcpServers": {{
    "vibecamp": {{ "url": "{mcp_url}" }}
  }}
}}</pre>

  <h2>No install? Just fetch the data</h2>
  <p>The whole schedule is one file your agent (or you) can grep:</p>
  <pre>curl -s {base}/data/events.ndjson        # one event per line (JSON)
curl -s {base}/data/schedule.md          # human-readable, by day
curl -s {base}/data/llms.txt             # field guide for agents</pre>

  <h2>Then just ask</h2>
  <div class="ask">"What's happening at the Pool on Saturday night?"</div>
  <div class="ask">"I'm into consciousness and the future of humanity — what should I go to?"</div>
  <div class="ask">"Find the sea shanties and add them to my plan."</div>
  <div class="ask">"What are the most popular events on Friday?"</div>

  <h2>Want to star / RSVP?</h2>
  <p>Every event includes a <code class="inline">url</code> that opens it directly in the official
  <a href="https://my.vibe.camp">my.vibe.camp</a> app — log in there and star it natively.
  This mirror never touches your account.</p>

  <footer>
    <a href="{base}/docs">REST API docs</a> ·
    <a href="{base}/data/index.json">data index</a> ·
    <a href="{base}/stats">stats</a> ·
    <a href="https://github.com/BenevolentFutures/vibecamp-expansion">source</a>
    <br>vibecamp-expansion v{__version__} · not affiliated with Vibe Camp; data mirrored from the public API.
  </footer>
</body>
</html>"""
