"""Four-agent verified auto-fix: triage -> repair -> independent verification -> supervisor.
See pipeline.py for the coordinator and each agentN_*.py for one agent's job."""
from .pipeline import Deps, run, try_claim, PROMPT_TEXT  # noqa: F401
from .agent4_supervisor import anthropic_review  # noqa: F401
