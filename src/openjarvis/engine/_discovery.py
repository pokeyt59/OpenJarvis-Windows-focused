"""Engine discovery — probe running engines and aggregate available models."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Tuple

from openjarvis.core.config import JarvisConfig
from openjarvis.core.registry import EngineRegistry
from openjarvis.engine._base import InferenceEngine

logger = logging.getLogger(__name__)

# Map registry keys to config host attribute (None = no host arg)
_HOST_MAP: Dict[str, str | None] = {
    "ollama": "ollama_host",
    "vllm": "vllm_host",
    "llamacpp": "llamacpp_host",
    "sglang": "sglang_host",
    "mlx": "mlx_host",
    "lmstudio": "lmstudio_host",
    "exo": "exo_host",
    "nexa": "nexa_host",
    "uzu": "uzu_host",
    "apple_fm": "apple_fm_host",
    "lemonade": "lemonade_host",
    "cloud": None,
    "litellm": None,
    "gemma_cpp": None,
}


def _make_engine(key: str, config: JarvisConfig) -> InferenceEngine:
    """Instantiate a registered engine with the appropriate config host."""
    cls = EngineRegistry.get(key)

    # gemma_cpp: pass config fields instead of host
    if key == "gemma_cpp":
        cfg = config.engine.gemma_cpp
        return cls(
            model_path=cfg.model_path or None,
            tokenizer_path=cfg.tokenizer_path or None,
            model_type=cfg.model_type or None,
            num_threads=cfg.num_threads,
        )

    host_attr = _HOST_MAP.get(key)
    if host_attr is not None:
        host = getattr(config.engine, host_attr, None)
        if host:
            return cls(host=host)
    return cls()


def _maybe_register_mining_sidecar_engine() -> None:
    """If a mining sidecar exists with a ``vllm_endpoint``, register a derived
    vLLM engine class pointing at it.  Idempotent.  Quiet on error.

    The trigger is the *shape* of the sidecar (presence of ``vllm_endpoint``),
    not the value of its ``provider`` field — this leaves room for future
    non-engine-replacing providers (e.g., a hypothetical cpu-pearl) whose
    sidecars don't include ``vllm_endpoint``.
    """
    try:
        from openjarvis.mining import Sidecar
        from openjarvis.mining._constants import SIDECAR_PATH
    except ImportError:
        return

    if EngineRegistry.contains("vllm-pearl-mining"):
        return  # idempotent

    payload = Sidecar.read(SIDECAR_PATH)
    if payload is None:
        return

    endpoint = payload.get("vllm_endpoint")
    model = payload.get("model")
    if not endpoint or not model:
        return  # data-driven gate: no vllm_endpoint → don't register

    from openjarvis.engine._openai_compat import _OpenAICompatibleEngine

    # Strip a trailing "/v1" path segment so _default_host is the bare
    # base URL and _api_prefix="/v1" combines correctly in request paths.
    api_prefix = "/v1"
    base_url = endpoint.rstrip("/")
    if base_url.endswith(api_prefix):
        base_url = base_url[: -len(api_prefix)]

    _cls = type(
        "VllmPearlMiningEngine",
        (_OpenAICompatibleEngine,),
        {
            "engine_id": "vllm-pearl-mining",
            "_default_host": base_url,
            "_api_prefix": api_prefix,
        },
    )
    EngineRegistry.register_value("vllm-pearl-mining", _cls)


def _probe_engine(
    key: str, config: JarvisConfig
) -> Tuple[str, InferenceEngine] | None:
    """Instantiate + health-check one engine; return ``(key, engine)`` if
    healthy, else ``None``. The unit of work for concurrent discovery."""
    try:
        engine = _make_engine(key, config)
        if engine.health():
            return (key, engine)
    except Exception as exc:
        logger.debug("Engine %r failed during discovery: %s", key, exc)
    return None


def discover_engines(config: JarvisConfig) -> List[Tuple[str, InferenceEngine]]:
    """Probe registered engines and return ``[(key, instance)]`` for healthy ones.

    Probes run **concurrently**. Each engine's ``health()`` is a network
    round-trip to a (usually-dead) localhost port that blocks ~2-4s on the
    connection timeout; run sequentially across the ~14 registered engines this
    summed to ~37s of pure waiting at every server boot. A thread pool collapses
    that to the slowest single probe (~4s) without changing which engines or
    models are discovered. Keep this concurrent — reverting to a sequential loop
    reintroduces the multi-second boot stall (see _boot_profile findings).

    Results are sorted with the config default engine first.
    """
    _maybe_register_mining_sidecar_engine()

    keys = list(EngineRegistry.keys())
    healthy: List[Tuple[str, InferenceEngine]] = []
    if keys:
        with ThreadPoolExecutor(max_workers=min(len(keys), 16)) as pool:
            for result in pool.map(lambda k: _probe_engine(k, config), keys):
                if result is not None:
                    healthy.append(result)

    default_key = config.engine.default

    def sort_key(item: Tuple[str, Any]) -> Tuple[int, str]:
        return (0 if item[0] == default_key else 1, item[0])

    healthy.sort(key=sort_key)
    return healthy


def _list_models_one(
    item: Tuple[str, InferenceEngine],
) -> Tuple[str, List[str]]:
    """Call ``list_models()`` on one engine; return ``(key, models)`` (empty on
    error). The unit of work for concurrent model discovery."""
    key, engine = item
    try:
        return (key, engine.list_models())
    except Exception as exc:
        logger.debug("Failed to list models for engine %r: %s", key, exc)
        return (key, [])


def discover_models(
    engines: List[Tuple[str, InferenceEngine]],
) -> Dict[str, List[str]]:
    """Call ``list_models()`` on each engine and return a dict.

    Probes run concurrently (same rationale as ``discover_engines``): each
    ``list_models()`` is a network call, so a thread pool keeps wall-clock at
    the slowest single engine rather than the sum across all healthy engines.
    """
    result: Dict[str, List[str]] = {}
    if not engines:
        return result
    with ThreadPoolExecutor(max_workers=min(len(engines), 16)) as pool:
        for key, models in pool.map(_list_models_one, engines):
            result[key] = models
    return result


def get_engine(
    config: JarvisConfig, engine_key: str | None = None
) -> Tuple[str, InferenceEngine] | None:
    """Get a specific engine by key, or the default with fallback.

    Returns ``(key, engine_instance)`` or ``None`` if no engine is available.
    """
    # Build an ordered list of keys to try, then fall back to full discovery.
    keys_to_try: list[str] = []
    if engine_key:
        keys_to_try.append(engine_key)

    default_key = config.engine.default
    if default_key and default_key not in keys_to_try:
        keys_to_try.append(default_key)

    for key in keys_to_try:
        if not EngineRegistry.contains(key):
            continue
        try:
            engine = _make_engine(key, config)
            if engine.health():
                return (key, engine)
        except Exception as exc:
            logger.debug("Engine %r health check failed: %s", key, exc)

    # Fallback to any healthy engine
    healthy = discover_engines(config)
    return healthy[0] if healthy else None


__all__ = ["discover_engines", "discover_models", "get_engine"]
