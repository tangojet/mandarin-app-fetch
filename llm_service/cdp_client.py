"""CDP client for Doubao — executes chat via a logged-in Docker Chrome session.

Runs doubao-cdp-chat.js inside the Docker container via `docker exec`.
The browser's Argus SDK handles a_bogus/msToken signing automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import Optional

logger = logging.getLogger("llm_service.cdp_client")

CDP_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "doubao-cdp-chat.js")


def _find_docker() -> Optional[str]:
    """Find the docker binary path."""
    docker = shutil.which("docker")
    if docker:
        return docker
    for p in ["/Applications/Docker.app/Contents/Resources/bin/docker", "/usr/local/bin/docker"]:
        if os.path.isfile(p):
            return p
    return None


class DoubaoCDPClient:
    """Runs chat completions through a Docker browser's page context."""

    def __init__(self, container: str, timeout: int = 60):
        self.container = container
        self.timeout = timeout
        self._docker_bin = _find_docker()
        self._script_copied = False

        if not self._docker_bin:
            logger.warning("Docker binary not found — CDP chat will not work")

    async def _ensure_script(self) -> None:
        """Copy the CDP chat script into the Docker container."""
        if self._script_copied or not self._docker_bin:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                self._docker_bin, "cp", CDP_SCRIPT,
                f"{self.container}:/tmp/doubao-cdp-chat.js",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            if proc.returncode == 0:
                self._script_copied = True
                logger.info("CDP chat script copied to %s", self.container)
            else:
                stderr = (await proc.stderr.read()).decode().strip()
                logger.warning("Failed to copy CDP script: %s", stderr)
        except Exception as e:
            logger.warning("Failed to copy CDP script: %s", e)

    async def chat_completion(self, text: str, bot_id: str) -> dict:
        """Send a chat message and return the response.

        Returns {"error": ..., "content": ..., "conversation_id": ...}
        """
        if not self._docker_bin:
            return {"error": "Docker not available", "content": "", "conversation_id": None}

        await self._ensure_script()
        if not self._script_copied:
            return {"error": "CDP script not available in container", "content": "", "conversation_id": None}

        input_json = json.dumps({"text": text, "bot_id": bot_id}, ensure_ascii=False)

        try:
            proc = await asyncio.create_subprocess_exec(
                self._docker_bin, "exec", "-i", self.container,
                "node", "/tmp/doubao-cdp-chat.js",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_json.encode()),
                timeout=self.timeout,
            )

            if stderr:
                logger.debug("CDP stderr: %s", stderr.decode().strip())

            if not stdout:
                return {"error": f"CDP script returned no output (exit {proc.returncode})", "content": "", "conversation_id": None}

            result = json.loads(stdout.decode())
            if result.get("error"):
                logger.warning("CDP chat error: %s", result["error"])
            else:
                logger.info("CDP chat OK: %d chars", len(result.get("content", "")))

            return result

        except asyncio.TimeoutError:
            return {"error": "CDP chat timed out", "content": "", "conversation_id": None}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON from CDP script: {e}", "content": "", "conversation_id": None}
        except Exception as e:
            return {"error": str(e), "content": "", "conversation_id": None}

    async def check_browser(self) -> bool:
        """Check if the Docker container has a doubao.com tab open."""
        if not self._docker_bin:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                self._docker_bin, "exec", self.container,
                "node", "-e",
                'const h=require("http");h.get("http://127.0.0.1:9222/json",r=>{let d="";r.on("data",c=>d+=c);r.on("end",()=>{const t=JSON.parse(d);const ok=t.some(p=>p.url.includes("doubao.com/chat")&&!p.url.includes("worker"));console.log(ok?"ok":"no_tab")})}).on("error",e=>console.log("no_chrome"))',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            result = stdout.decode().strip()
            return result == "ok"
        except Exception:
            return False

    async def close(self) -> None:
        pass
