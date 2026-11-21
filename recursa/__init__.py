"""Recursa: agents that get better every run.

v1 proves one claim: on one reference agent, Recursa proposes a system-prompt
change that a human approves and that improves a pre-declared primary metric on a
sealed evaluation set, with no guardrail regression.
"""
from . import clients, evals, trace
from .adapter import Agent, Context, wrap
from .evals import EvalSuite
from .project import Config, Project, load_config
from .stats import GateConfig
from .trace import SandboxViolation, tool
from .types import Example, Result

__all__ = ["Agent", "Config", "Context", "EvalSuite", "Example", "GateConfig", "Project", "Result",
           "SandboxViolation", "clients", "evals", "load_config", "tool", "trace", "wrap", "Learner"]
__version__ = "0.1.0"


def __getattr__(name: str):
    if name == "Learner":  # imported lazily so the optimizer worker stays light
        from .learner import Learner

        return Learner
    raise AttributeError(name)
