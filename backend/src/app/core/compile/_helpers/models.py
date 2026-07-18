"""Model constructors — `OpenAIChat(...)` / `Claude(...)` / `Ollama(...)` / `Gemini(...)`.

The generator writes Python that depends ONLY on `agno`. Each model
provider maps to a constructor in `agno.models.*`. If the node carries
a `presetId`, the key is read from `os.environ[...]` so the user
controls secrets via environment variables rather than embedding them
in the exported source.
"""
from __future__ import annotations

from app.core.compile.errors import CompileError as GeneratorError

def provider_env_var(provider: str) -> str | None:
    """Return the `os.environ[...]` key the preset-mode generator reads."""
    return {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "google": "GOOGLE_API_KEY",
        "ollama": None,
    }.get(provider)

def model_expr(model: dict | None) -> str:
    """Render `OpenAIChat(id='gpt-4o', api_key='...')` etc.

    Behavior:
      - `model` is None / missing `provider` → `GeneratorError` (caller is
        responsible for surfacing a useful message).
      - `presetId` set → key comes from `os.environ[<PROVIDER>_API_KEY]`,
        NOT the inline `apiKey` field. The preset is the user's chosen
        default; the inline apiKey is irrelevant.
      - Inline mode → key is inlined into the constructor (legacy path).

    Unknown providers fall back to a generic `OpenAIChat(...)` plus a
    `# TODO` comment so the export still succeeds — the user can edit.
    """
    if not model:
        raise GeneratorError("agent missing model config")
    provider = (model.get("provider") or "openai").lower()
    model_id = model.get("modelId") or ""
    api_key = model.get("apiKey") or ""
    base_url = model.get("baseUrl")
    preset_id = model.get("presetId") or ""

    env_var = provider_env_var(provider)
    if preset_id and env_var:
        return emit_model_with_env(provider, model_id, env_var, base_url)

    if provider == "openai":
        kw = [f"id={model_id!r}"]
        if api_key:
            kw.append(f"api_key={api_key!r}")
        if base_url:
            kw.append(f"base_url={base_url!r}")
        return f"OpenAIChat({', '.join(kw)})"
    if provider == "anthropic":
        kw = [f"id={model_id!r}"]
        if api_key:
            kw.append(f"api_key={api_key!r}")
        if base_url:
            kw.append(f"client_params={{'base_url': {base_url!r}}}")
        return f"Claude({', '.join(kw)})"
    if provider == "ollama":
        kw = [f"id={model_id!r}"]
        if base_url:
            kw.append(f"host={base_url!r}")
        return f"Ollama({', '.join(kw)})"
    if provider == "google":
        kw = [f"id={model_id!r}"]
        if api_key:
            kw.append(f"api_key={api_key!r}")
        return f"Gemini({', '.join(kw)})"
    return f"OpenAIChat(id={model_id!r})  # TODO: provider '{provider}' not wired yet"

def emit_model_with_env(
    provider: str, model_id: str, env_var: str, base_url: str | None
) -> str:
    """Build a constructor that reads the key from `os.environ` at runtime."""
    if provider == "openai":
        kw = [f"id={model_id!r}", f"api_key=os.environ[{env_var!r}]"]
        if base_url:
            kw.append(f"base_url={base_url!r}")
        return f"OpenAIChat({', '.join(kw)})"
    if provider == "anthropic":
        base = (
            "os.environ.get('ANTHROPIC_BASE_URL') or 'https://api.anthropic.com'"
        )
        kw = [
            f"id={model_id!r}",
            f"api_key=os.environ[{env_var!r}]",
            f"client_params={{'base_url': {base}}}",
        ]
        if base_url:
            # user override wins over the env-driven default
            kw[-1] = f"client_params={{'base_url': {base_url!r}}}"
        return f"Claude({', '.join(kw)})"
    if provider == "google":
        return f"Gemini(id={model_id!r}, api_key=os.environ[{env_var!r}])"
    return f"OpenAIChat(id={model_id!r})  # TODO preset for {provider}"