"""YAML configuration loading and inheritance."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from pricing_scraper.clients.base import ConfigurationError


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def default_config_path() -> Path:
    """Prefer the ignored private config when it exists."""
    private = Path("config.local.yaml")
    return private if private.exists() else Path("config.yaml")


def load_config(path: Path, _seen: set[Path] | None = None) -> dict[str, Any]:
    """Load YAML configuration, including an optional inherited base file."""
    resolved = path.resolve()
    seen = set(_seen or ())
    if resolved in seen:
        raise ConfigurationError(
            f"Recursive config inheritance detected: {resolved}"
        )
    seen.add(resolved)
    try:
        payload = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Config file not found: {resolved}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in {resolved}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("The config root must be a YAML object.")

    extends = payload.pop("extends", None)
    if extends:
        parent_path = (resolved.parent / str(extends)).resolve()
        payload = _deep_merge(load_config(parent_path, seen), payload)

    for required in ("request", "nykaa", "output"):
        if not isinstance(payload.get(required), dict):
            raise ConfigurationError(f"Config section {required!r} is required.")

    payload.setdefault("tira", {})
    payload.setdefault("amazon", {})
    for site in ("nykaa", "tira"):
        curl_file = str(payload[site].get("curl_file") or "").strip()
        if curl_file:
            curl_path = Path(curl_file).expanduser()
            if not curl_path.is_absolute():
                curl_path = resolved.parent / curl_path
            payload[site]["curl_file"] = str(curl_path.resolve())
    return payload


def apply_environment_overrides(config: dict[str, Any]) -> None:
    """Let hosted secrets replace the credentials committed in the YAML."""
    nykaa_command = os.getenv("NYKAA_CURL_COMMAND", "").strip()
    nykaa_file = os.getenv("NYKAA_CURL_FILE", "").strip()
    if nykaa_command:
        config["nykaa"]["curl_command"] = nykaa_command
        config["nykaa"]["curl_file"] = ""
    elif nykaa_file:
        config["nykaa"]["curl_file"] = nykaa_file
    tira_id = os.getenv("TIRA_APPLICATION_ID", "").strip()
    tira_token = os.getenv("TIRA_APPLICATION_TOKEN", "").strip()
    if tira_id:
        config["tira"]["application_id"] = tira_id
    if tira_token:
        config["tira"]["application_token"] = tira_token
