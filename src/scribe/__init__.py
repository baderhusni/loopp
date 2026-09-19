"""Scribe -- record a computer-use run once, replay it deterministically forever.

An LLM drives a real application surface to work out how a task is done. The run
is distilled into a typed, versioned capability artifact. From then on the
artifact is replayed without a model in the decision loop, with guardrails,
declared business outcomes, and a path to a human when it gets stuck.
"""

__version__ = "0.1.0"
