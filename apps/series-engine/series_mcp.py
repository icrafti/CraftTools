"""Expose local Series Engine dubbing tools over MCP stdio."""

import argparse
import json
from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field
from typing_extensions import TypedDict


class Edit(TypedDict):
    cue: str
    text: Annotated[str, Field(min_length=1, max_length=2000)]


def make_server(port: int) -> FastMCP:
    server = FastMCP("Series Engine")
    opener = build_opener(ProxyHandler({}))

    def call(endpoint, *, query=None, payload=None, timeout=120):
        address = f"http://127.0.0.1:{port}{endpoint}"
        if query is not None:
            address += "?" + urlencode(query, doseq=True)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(address, data=body, headers={"Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Series Engine HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError) as error:
            raise RuntimeError(f"Series Engine request failed on 127.0.0.1:{port}: {error}") from error

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def check_dub_timing(
        path: str,
        master_language: str,
        languages: Annotated[list[str], Field(min_length=1)],
        chapter: str | None = None,
        cue_start: str | None = None,
        cue_end: str | None = None,
    ) -> dict:
        """Find overlong blocks, letter budgets, cue text and revision. Path is relative to the engine workspace."""
        query = {"path": path, "master_language": master_language, "language": languages}
        query.update({key: value for key, value in
                      (("chapter", chapter), ("cue_start", cue_start), ("cue_end", cue_end))
                      if value is not None})
        return call("/api/project/sync", query=query)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
    def batch_redub_microsoft(
        path: str,
        master_language: str,
        language: str,
        revision: str,
        changes: Annotated[list[Edit], Field(min_length=1, max_length=100)],
    ) -> dict:
        """Replace changed cues using installed Windows voices. Use the revision from check_dub_timing.

        The engine commits the batch only when every affected block fits at its original speaking rate.
        """
        return call("/api/project/sync/adapt", timeout=1800, payload={
            "path": path, "master_language": master_language, "language": language,
            "revision": revision, "provider": "microsoft", "changes": changes,
        })

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, help="Port of the running Series Engine")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    make_server(args.port).run(transport="stdio")
