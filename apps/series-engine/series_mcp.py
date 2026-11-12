"""Expose local Series Engine tools and production prompts over MCP stdio."""

import argparse
import json
from typing import Annotated
from pathlib import Path
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
    guide = Path(__file__).with_name("AGENT_GUIDE.md")
    server = FastMCP("Series Engine", instructions=guide.read_text(encoding="utf-8"))
    opener = build_opener(ProxyHandler({}))

    @server.resource("series-engine://agent-guide", mime_type="text/markdown")
    def agent_guide() -> str:
        """Current Series Engine workflow and maintenance instructions."""
        return guide.read_text(encoding="utf-8")

    @server.prompt()
    def plan_scene_audio(path: str) -> str:
        """Prepare English Stable Audio 3 prompts for a project's storyboard."""
        return f"Prepare scene audio for {path}.\n\n" + guide.read_text(encoding="utf-8").split(
            "## Stable Audio 3 по сценам", 1
        )[1].split("\n## ", 1)[0]

    @server.prompt()
    def review_3d_scenes(path: str, render: str) -> str:
        """Review each shot for clipping, foot contact and motion continuity across cuts."""
        checklist = guide.read_text(encoding="utf-8").split(
            "## Приёмка 3D-сцен", 1
        )[1].split("\n## ", 1)[0].strip()
        return f"Проект: {path}\nРендер: {render}\n\n{checklist}"

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

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
    def build_scene_audio(
        path: str,
        language: str,
        cue_start: str,
        cue_end: str,
        resolution: str = "360p",
        dry_run: bool = True,
    ) -> dict:
        """Queue a fragment with Stable Audio 3. Set dry_run=False to generate sound and render."""
        return call("/api/jobs", payload={
            "path": path, "languages": [language], "cue_start": cue_start, "cue_end": cue_end,
            "resolution": resolution, "mix": True, "stable_audio": True, "dry_run": dry_run,
        })

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def check_build(job_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]) -> dict:
        """Read status, log and output paths of an engine job."""
        return call(f"/api/jobs/{job_id}")

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, help="Port of the running Series Engine")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    make_server(args.port).run(transport="stdio")
