"""DashScope Wanx image generation plugin.

Uses the Wanx2.1-T2I-Turbo model via the DashScope asynchronous task API.
Requires DASHSCOPE_API_KEY in ~/.hermes/.env.

Aspect ratios map to Wanx canvas sizes:
  landscape → 1280*720
  portrait  → 720*1280
  square    → 1024*1024
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    success_response,
)

logger = logging.getLogger(__name__)

_SUBMIT_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
    "text2image/image-synthesis"
)
_TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"

_DEFAULT_MODEL = "wanx2.1-t2i-turbo"

_ASPECT_TO_SIZE = {
    "landscape": "1280*720",
    "portrait": "720*1280",
    "square": "1024*1024",
}

_MODELS = [
    {
        "id": "wanx2.1-t2i-turbo",
        "display": "Wanx 2.1 Turbo",
        "speed": "~10s",
        "strengths": "Fast, general purpose, Chinese & English prompts",
        "price": "DashScope credits",
    },
    {
        "id": "wanx2.1-t2i-plus",
        "display": "Wanx 2.1 Plus",
        "speed": "~20s",
        "strengths": "Higher quality, detail-rich scenes",
        "price": "DashScope credits",
    },
    {
        "id": "wanx-v2",
        "display": "Wanx v2",
        "speed": "~15s",
        "strengths": "Stable, widely tested",
        "price": "DashScope credits",
    },
]


class WanxImageGenProvider(ImageGenProvider):
    """DashScope Wanx image generation backend."""

    @property
    def name(self) -> str:
        return "wanx"

    @property
    def display_name(self) -> str:
        return "Wanx (DashScope)"

    def is_available(self) -> bool:
        return bool(os.environ.get("DASHSCOPE_API_KEY", "").strip())

    def list_models(self) -> List[Dict[str, Any]]:
        return _MODELS

    def default_model(self) -> Optional[str]:
        return _DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Wanx (DashScope)",
            "badge": "paid",
            "tag": "通义万象 — Chinese & English text-to-image",
            "env_vars": [
                {
                    "key": "DASHSCOPE_API_KEY",
                    "prompt": "DashScope API key",
                    "url": "https://dashscope.aliyuncs.com",
                },
            ],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        try:
            import httpx
        except ImportError:
            return error_response(error="httpx is required for Wanx image generation", provider=self.name)

        api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not api_key:
            return error_response(error="DASHSCOPE_API_KEY is not set", provider=self.name)

        aspect = resolve_aspect_ratio(aspect_ratio)
        size = _ASPECT_TO_SIZE.get(aspect, "1024*1024")
        model = kwargs.get("model") or _DEFAULT_MODEL
        n = int(kwargs.get("num_images", 1))

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Asynchronous mode — DashScope returns a task_id immediately.
            "X-DashScope-Async": "enable",
        }
        payload = {
            "model": model,
            "input": {"prompt": prompt},
            "parameters": {"size": size, "n": n},
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(_SUBMIT_URL, json=payload, headers=headers)
                resp.raise_for_status()
                task_data = resp.json()

            task_id = task_data.get("output", {}).get("task_id")
            if not task_id:
                return error_response(
                    error=f"Wanx: no task_id in response: {task_data}", provider=self.name
                )

            # Poll for task completion (max ~90s).
            image_url = self._poll_task(task_id, api_key)
            if not image_url:
                return error_response(error="Wanx: task timed out or failed", provider=self.name)

            return success_response(
                image=image_url,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
                provider=self.name,
            )

        except httpx.HTTPStatusError as exc:
            return error_response(
                error=f"Wanx API error {exc.response.status_code}: {exc.response.text[:200]}",
                provider=self.name,
                prompt=prompt,
            )
        except Exception as exc:
            logger.exception("Wanx generate error")
            return error_response(error=str(exc), provider=self.name, prompt=prompt)

    def _poll_task(self, task_id: str, api_key: str, *, max_wait: int = 90) -> Optional[str]:
        """Poll the DashScope task endpoint until the image is ready."""
        try:
            import httpx
        except ImportError:
            return None

        url = _TASK_URL.format(task_id=task_id)
        headers = {"Authorization": f"Bearer {api_key}"}
        deadline = time.monotonic() + max_wait

        with httpx.Client(timeout=15.0) as client:
            while time.monotonic() < deadline:
                try:
                    resp = client.get(url, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    output = data.get("output", {})
                    status = output.get("task_status", "")

                    if status == "SUCCEEDED":
                        results = output.get("results", [])
                        if results:
                            return results[0].get("url")
                        return None

                    if status in ("FAILED", "CANCELED"):
                        logger.warning("Wanx task %s ended with status %s", task_id, status)
                        return None

                    time.sleep(3)
                except Exception as exc:
                    logger.debug("Wanx poll error: %s", exc)
                    time.sleep(3)

        return None


def register(ctx) -> None:
    """Register the Wanx image generation provider with the plugin system."""
    ctx.register_image_gen_provider(WanxImageGenProvider())
