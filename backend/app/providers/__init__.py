"""Provider adapters (Meta, GitHub, Modal, Baseten) with lazy configuration."""

from __future__ import annotations

from typing import Optional

import httpx

from . import registry
from .baseten import BasetenAdapter
from .github import GitHubAdapter
from .meta import MetaAdapter
from .modal import ModalAdapter

__all__ = ["PROVIDER_NAMES", "get_adapter", "registry"]

PROVIDER_NAMES = ("meta", "github", "modal", "baseten")


def get_adapter(name: str, transport: Optional[httpx.BaseTransport] = None):
    """Build the named adapter from current environment configuration.

    ``transport`` is test-only (httpx.MockTransport); production calls pass
    nothing and reach the live providers.
    """
    from .. import config

    if name == "meta":
        return MetaAdapter(
            api_key=config.get_meta_api_key(),
            base_url=config.get_meta_base_url(),
            transport=transport,
        )
    if name == "github":
        return GitHubAdapter(
            token=config.get_github_token(),
            repo=config.get_github_repo(),
            transport=transport,
        )
    if name == "modal":
        return ModalAdapter(
            token_id=config.get_modal_token_id(),
            token_secret=config.get_modal_token_secret(),
        )
    if name == "baseten":
        return BasetenAdapter(api_key=config.get_baseten_api_key(), transport=transport)
    return None
