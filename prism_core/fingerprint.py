"""Reproducibility identity.

Two distinct concepts (CLAUDE.md §21, §25–26):

* :func:`config_fingerprint` - a deterministic hash answering *"are these two
  results equivalent?"* It is stable across machines and runs: same inputs →
  same hash. It intentionally binds the **Ollama model digest** (not just the
  ``mistral:7b`` tag), the dataset + ``DATASET_VERSION``, the template identities
  (per-template SHA-256), the prompt conditions, the inference parameters, the
  seed, the question count, and the protocol version.
* :func:`new_benchmark_run_id` - a unique id answering *"which run produced
  this?"* One per execution.

The fingerprint is computed **per (model, dataset)** - the natural unit for the
future "an existing PRISM benchmark is available for this model on this dataset"
lookup.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Mapping, Sequence

from prism_core import config


def new_benchmark_run_id() -> str:
    """A unique identifier for one benchmark execution."""
    return uuid.uuid4().hex


def template_hashes_from_prompts(prompts_doc: Mapping[str, Any]) -> dict[str, str]:
    """Extract ``{prompt_id: sha256}`` from a loaded ``*_prompts.json`` artifact."""
    return {
        str(entry["prompt_id"]): str(entry["sha256"])
        for entry in prompts_doc["templates"]
    }


def config_fingerprint(
    *,
    model_tag: str,
    model_digest: str | None,
    dataset: str,
    question_count: int,
    template_sha256: Mapping[str, str],
    dataset_version: str = config.DATASET_VERSION,
    template_version: str = config.TEMPLATE_VERSION,
    prompt_conditions: Sequence[str] = config.PROMPT_CONDITIONS,
    temperature: float = config.TEMPERATURE,
    num_predict: int = config.NUM_PREDICT,
    random_seed: int = config.RANDOM_SEED,
    protocol_version: str = config.PROTOCOL_VERSION,
) -> str:
    """Return the canonical SHA-256 equivalence key for one (model, dataset).

    The payload is serialized with ``sort_keys=True`` and compact separators so
    the digest depends only on values, never on argument or dict order.
    """
    payload: dict[str, Any] = {
        "protocol_version": protocol_version,
        "model_tag": model_tag,
        "model_digest": model_digest,
        "dataset": dataset,
        "dataset_version": dataset_version,
        "template_version": template_version,
        # Sort template hashes for order-independence.
        "template_sha256": {key: template_sha256[key] for key in sorted(template_sha256)},
        # Prompt conditions are semantically ordered - keep as a list.
        "prompt_conditions": list(prompt_conditions),
        "temperature": temperature,
        "num_predict": num_predict,
        "random_seed": random_seed,
        "question_count": question_count,
    }

    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()