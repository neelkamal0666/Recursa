"""Project configuration (``recursa.yaml``) and loading the user's agent bundle."""
from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import yaml

from .adapter import Agent
from .evals import EvalSuite
from .stats import GateConfig


@dataclass
class Project:
    """What a project factory returns: the agent and how to judge it."""

    agent: Agent
    suite: EvalSuite


@dataclass
class Config:
    root: str
    project: str
    factory: str                     # "module:function" returning Project
    home: str = ".recursa"
    gate: GateConfig = field(default_factory=GateConfig)
    optimizer: dict[str, Any] = field(default_factory=lambda: {"strategy": "heuristic", "candidates": 4,
                                                               "gate_top_k": 1, "model": "claude-opus-5-5"})
    reflect: dict[str, Any] = field(default_factory=dict)
    reviewers: str = "reviewers.yaml"
    expire_after_days: float = 7.0
    budget: dict[str, Any] = field(default_factory=lambda: {"max_cost_usd_per_cycle": 5.0})
    dev_samples: int = 1
    store: dict[str, Any] = field(default_factory=lambda: {"auto_migrate": True})

    @property
    def home_path(self) -> str:
        return os.path.join(self.root, self.home)

    @property
    def reviewers_path(self) -> str:
        return os.path.join(self.root, self.reviewers)


STORE_KEYS = {"auto_migrate", "url", "key_env", "sealed_key_env", "timeout"}


def _check_store(store: dict[str, Any]) -> dict[str, Any]:
    if {"key", "api_key", "token"} & set(store):
        raise ValueError("put API keys in an environment variable named by store.key_env, not in the config")
    unknown = set(store) - STORE_KEYS
    if unknown:
        raise ValueError(f"unknown store setting(s): {', '.join(sorted(unknown))}")
    url = store.get("url")
    if url is not None and not str(url).startswith(("http://", "https://")):
        raise ValueError(f"store.url must be an http(s) URL of Recursa Server, got {url!r}")
    return store


def load_config(path: str = "recursa.yaml") -> Config:
    path = os.path.abspath(path)
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    root = os.path.dirname(path)
    opt = Config.__dataclass_fields__["optimizer"].default_factory()
    opt.update(raw.get("optimizer", {}))
    budget = Config.__dataclass_fields__["budget"].default_factory()
    budget.update(raw.get("budget", {}))
    store = Config.__dataclass_fields__["store"].default_factory()
    store.update(_check_store(raw.get("store") or {}))
    return Config(
        root=root, project=raw["project"], factory=raw["factory"], home=raw.get("home", ".recursa"),
        gate=GateConfig.from_dict(raw.get("gate", {})), optimizer=opt, reflect=raw.get("reflect", {}),
        reviewers=raw.get("reviewers", "reviewers.yaml"),
        expire_after_days=float(raw.get("expire_after_days", 7)), budget=budget,
        dev_samples=int(raw.get("dev_samples", 1)), store=store,
    )


def load_project(factory: str, root: str) -> Project:
    if root not in sys.path:
        sys.path.insert(0, root)
    mod_name, _, attr = factory.partition(":")
    obj = getattr(importlib.import_module(mod_name), attr)
    proj = obj() if callable(obj) else obj
    if not isinstance(proj, Project):
        raise TypeError(f"{factory} must return recursa.Project")
    return proj
