"""OllamaService - the local inference layer.

Wraps the four Ollama HTTP endpoints the app needs, built on the configurable
base URL (:func:`prism_core.config.ollama_url`) rather than a single hard-coded
global:

* ``/api/tags`` - is Ollama up, and which models are installed (+ digests)?
* ``/api/show`` - details for one model.
* ``/api/generate`` - the single-item inference primitive (ported verbatim from
  the research ``generate_with_ollama``: ``stream=False``, ``temperature`` and
  ``num_predict`` options, ``raise_for_status``, and a hard failure if the reply
  is not JSON or lacks a ``response`` field).
* ``/api/pull`` - download a model, streaming NDJSON progress events.

Pure Python + ``requests``; no Qt. Callers that want progress pass a plain
callback - the GUI worker adapts it to Qt signals.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests

from prism_core import config


class OllamaError(RuntimeError):
    """Any failure talking to Ollama (transport, non-2xx, or malformed reply)."""


class ModelNotFoundError(OllamaError):
    """The registry rejected the pull because ``model_name`` itself is bad -
    unknown model, unknown tag/variant, or malformed reference - as opposed
    to a transport/timeout failure. Callers can catch this specifically to
    show "that model doesn't exist" instead of a generic pull-failed
    message. Kept a subclass of :class:`OllamaError` so existing ``except
    OllamaError`` call sites still catch it too."""


class PullCancelled(Exception):
    """Raised from a pull's ``progress_callback`` to stop the download
    mid-stream (see :func:`pull`). Deliberately not an :class:`OllamaError`
    subclass - a cancellation isn't a failure talking to Ollama."""


@dataclass(frozen=True)
class OllamaModel:
    """One installed model as reported by ``/api/tags``."""

    name: str
    digest: str
    size: Optional[int] = None
    modified_at: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PullProgress:
    """One streamed ``/api/pull`` progress event."""

    status: str
    digest: Optional[str] = None
    total: Optional[int] = None
    completed: Optional[int] = None

    @property
    def percent(self) -> Optional[float]:
        if self.total and self.completed is not None and self.total > 0:
            return 100.0 * self.completed / self.total
        return None


def _url(path: str, base_url: Optional[str]) -> str:
    return config.ollama_url(base_url, path)


def is_available(*, base_url: Optional[str] = None, timeout: float = 5.0) -> bool:
    """True if Ollama answers ``/api/tags`` (used to gate the run-setup UI)."""
    try:
        response = requests.get(_url(config.OLLAMA_TAGS_PATH, base_url), timeout=timeout)
    except requests.RequestException:
        return False
    return response.ok


def list_models(*, base_url: Optional[str] = None, timeout: float = 10.0) -> list[OllamaModel]:
    """Return every installed model with its digest."""
    try:
        response = requests.get(_url(config.OLLAMA_TAGS_PATH, base_url), timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise OllamaError(f"Could not list Ollama models: {exc}") from exc
    except ValueError as exc:
        raise OllamaError("Ollama /api/tags did not return valid JSON.") from exc

    models: list[OllamaModel] = []
    for entry in data.get("models", []):
        models.append(
            OllamaModel(
                name=str(entry.get("name") or entry.get("model") or ""),
                digest=str(entry.get("digest") or ""),
                size=entry.get("size"),
                modified_at=entry.get("modified_at"),
                details=entry.get("details") or {},
            )
        )
    return models


def find_model(
    model_name: str, *, base_url: Optional[str] = None, timeout: float = 10.0
) -> Optional[OllamaModel]:
    """Find an installed model by tag, tolerating an implicit ``:latest``."""
    by_name = {model.name: model for model in list_models(base_url=base_url, timeout=timeout)}
    if model_name in by_name:
        return by_name[model_name]
    if ":" not in model_name and f"{model_name}:latest" in by_name:
        return by_name[f"{model_name}:latest"]
    return None


def model_digest(
    model_name: str, *, base_url: Optional[str] = None, timeout: float = 10.0
) -> Optional[str]:
    """The content digest for an installed model, or None if not installed.

    The digest - not the mutable ``mistral:7b`` tag - is what makes "same model"
    precise in :func:`prism_core.fingerprint.config_fingerprint`.
    """
    model = find_model(model_name, base_url=base_url, timeout=timeout)
    return model.digest if model else None


def show(
    model_name: str, *, base_url: Optional[str] = None, timeout: float = 30.0
) -> dict[str, Any]:
    """Return ``/api/show`` details for a model."""
    try:
        response = requests.post(
            _url(config.OLLAMA_SHOW_PATH, base_url),
            json={"name": model_name},
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise OllamaError(f"Could not show model {model_name!r}: {exc}") from exc
    except ValueError as exc:
        raise OllamaError("Ollama /api/show did not return valid JSON.") from exc


def generate(
    *,
    model_name: str,
    prompt_text: str,
    base_url: Optional[str] = None,
    temperature: float = config.TEMPERATURE,
    num_predict: int = config.NUM_PREDICT,
    timeout: float = config.REQUEST_TIMEOUT,
) -> tuple[str, float]:
    """Run one non-streaming generation. Returns ``(response_text, latency_s)``.

    Verbatim port of the research ``generate_with_ollama`` contract: the request
    body, the option keys, ``raise_for_status``, and the hard failures on a
    non-JSON body or a missing ``response`` field are all preserved so inference
    behaves identically to the validated pipeline.
    """
    payload = {
        "model": model_name,
        "prompt": prompt_text,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": num_predict},
    }

    started = time.perf_counter()
    response = requests.post(
        _url(config.OLLAMA_GENERATE_PATH, base_url),
        json=payload,
        timeout=timeout,
    )
    latency = time.perf_counter() - started
    response.raise_for_status()

    try:
        body = response.json()
    except ValueError as exc:
        raise OllamaError("Ollama /api/generate did not return valid JSON.") from exc

    raw = body.get("response")
    if raw is None:
        raise OllamaError(
            f"Ollama /api/generate response has no 'response' field: {body!r}"
        )
    return str(raw), latency


# How long we'll wait for *any* byte (including NDJSON keep-alive lines)
# before treating the connection as stalled. Ollama normally emits a status
# line well within this window (even a bare "pulling manifest" step reports
# back in a second or two) - a real stall (server wedged, network dropped
# mid-download, etc.) previously hung the request forever because no
# timeout was set at all, which is also what made Stop/Cancel appear to do
# nothing: the code never got back to a point where it could check for a
# cancellation. Now a stall raises requests.exceptions.Timeout instead of
# blocking indefinitely, which either aborts (if the user cancelled) or is
# retried automatically (see below) - the request resumes from wherever
# Ollama's own on-disk partial download left off, so a retry after a stall
# never restarts the download from zero.
_STALL_TIMEOUT_S = 30.0
# A stalled pull is retried this many times (with a short backoff) before
# giving up and surfacing an error, rather than looking "stuck" forever.
_MAX_STALL_RETRIES = 5
# _STALL_TIMEOUT_S only fires when literally zero bytes arrive. A wedged
# server/proxy can instead keep the connection alive by trickling the same
# status line (e.g. repeating "pulling manifest" with no digest/total/
# completed change) forever, which resets that timer every time without
# any real progress ever happening - previously an unbounded hang with no
# way out short of force-quitting the app. Track the last *meaningful*
# change and treat "no change" for this long as a stall too, even though
# bytes are technically still arriving.
_NO_PROGRESS_TIMEOUT_S = 45.0
# "pulling manifest" is a single small HTTP round-trip to the registry -
# normally under a couple of seconds - so it never legitimately needs
# anywhere near the full _NO_PROGRESS_TIMEOUT_S window used once real
# byte-progress is flowing. Previously a wedged/unreachable registry sat
# on this exact step for the *entire* 45s x 5-retry cycle (minutes) before
# ever surfacing an error, which is indistinguishable from the app itself
# being hung. Give this specific step its own much shorter patience so a
# genuinely unreachable/slow registry fails fast and starts retrying
# almost immediately instead of leaving the person staring at a frozen
# "pulling manifest" for minutes wondering if anything is happening.
_MANIFEST_NO_PROGRESS_TIMEOUT_S = 8.0
# Post-download finalization steps - hashing and writing a multi-gigabyte
# file to disk - report no byte-progress fields either (same shape as the
# initial manifest step) but can legitimately run well past the general
# _NO_PROGRESS_TIMEOUT_S default on a slow disk. Give these their own
# more generous ceiling so a genuinely slow (but still working) local
# hash/write pass never gets mistaken for a wedged connection.
_FINALIZE_NO_PROGRESS_TIMEOUT_S = 120.0
_FINALIZE_STEP_PREFIXES = ("verifying sha256 digest", "writing manifest")
# Absolute ceiling on one pull() call regardless of how many stall-retries
# succeed at reconnecting - a final backstop so a model that's genuinely
# too large/slow for this connection fails with a clear message instead of
# retrying forever.
_MAX_TOTAL_PULL_S = 30 * 60.0


def pull(
    model_name: str,
    *,
    base_url: Optional[str] = None,
    progress_callback: Optional[Callable[[PullProgress], None]] = None,
    timeout: Optional[float] = _STALL_TIMEOUT_S,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """Download a model, invoking ``progress_callback`` for each NDJSON event.

    Raises :class:`OllamaError` if the stream reports an error, if the
    connection stalls (no data at all for ``timeout`` seconds) more than a
    few times in a row, if the same status is reported with no real
    progress for too long even while technically still connected, or if
    the pull as a whole runs past an absolute time ceiling. ``progress_
    callback`` may raise :class:`PullCancelled` to abort the download
    between NDJSON events (e.g. in response to a user-requested cancel);
    ``cancel_event``, if given, is *also* checked right after each
    stall-timeout, so a cancel requested while the connection is stalled
    (no events arriving at all) still takes effect within ``timeout``
    seconds instead of waiting forever for a line that may never come.
    """
    pull_started = time.monotonic()
    attempts = 0
    last_key: Optional[tuple] = None
    last_change = pull_started
    while True:
        if time.monotonic() - pull_started > _MAX_TOTAL_PULL_S:
            raise OllamaError(
                f"Could not pull model {model_name!r}: the pull didn't finish "
                f"within {int(_MAX_TOTAL_PULL_S // 60)} minutes even after "
                "retrying. This model may be too large for this connection, "
                "or Ollama's registry may be unreachable right now - try "
                "again later."
            )
        try:
            with requests.post(
                _url(config.OLLAMA_PULL_PATH, base_url),
                json={"name": model_name, "stream": True},
                stream=True,
                timeout=(10.0, timeout) if timeout else None,
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines(decode_unicode=True):
                    if cancel_event is not None and cancel_event.is_set():
                        raise PullCancelled()
                    if not line:
                        continue
                    attempts = 0  # any data at all resets the *connection* stall counter
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if "error" in event:
                        registry_msg = str(event["error"])
                        # The registry reports a bad model reference (unknown
                        # model, unknown tag/variant, malformed name) as a
                        # plain "error" event rather than an HTTP status, and
                        # its wording varies by Ollama version - match the
                        # phrases actually seen in the wild rather than one
                        # exact string. Anything not matching one of these
                        # falls through to the generic OllamaError below
                        # (e.g. a private/unauthorized model, or a registry-
                        # side problem) so it isn't mislabeled as "doesn't
                        # exist" when the model reference was actually fine.
                        not_found_phrases = (
                            "file does not exist",
                            "not found",
                            "no such",
                            "manifest unknown",
                            "invalid reference format",
                            "unable to find",
                        )
                        if any(p in registry_msg.lower() for p in not_found_phrases):
                            raise ModelNotFoundError(
                                f"Model {model_name!r} not found: {registry_msg}"
                            )
                        raise OllamaError(f"Pull failed for {model_name!r}: {registry_msg}")

                    key = (event.get("status"), event.get("digest"), event.get("completed"))
                    now = time.monotonic()
                    if key != last_key:
                        last_key = key
                        last_change = now
                    else:
                        status_text = str(event.get("status") or "")
                        # Only the *initial* manifest lookup (a quick
                        # registry round-trip before any bytes are known)
                        # gets the short patience window. Ollama also uses
                        # the word "manifest" in later, legitimately slower
                        # finalization steps once a download is fully
                        # complete - "verifying sha256 digest" and "writing
                        # manifest" both involve hashing/writing a
                        # multi-gigabyte file to disk and can genuinely take
                        # well over 8 seconds on a slow disk. A prior version
                        # of this check matched any status containing
                        # "manifest" at all, which force-restarted the whole
                        # pull right as it reached 100% and moved into
                        # finalizing - the download would look like it
                        # snapped back to "pulling manifest" from scratch
                        # instead of just finishing. Match the exact initial
                        # phrase only.
                        is_manifest_step = status_text.strip().lower() == "pulling manifest"
                        is_finalize_step = status_text.strip().lower().startswith(_FINALIZE_STEP_PREFIXES)
                        if is_manifest_step:
                            limit = _MANIFEST_NO_PROGRESS_TIMEOUT_S
                        elif is_finalize_step:
                            limit = _FINALIZE_NO_PROGRESS_TIMEOUT_S
                        else:
                            limit = _NO_PROGRESS_TIMEOUT_S
                        if now - last_change > limit:
                            # Bytes are arriving (we're inside iter_lines, not a
                            # socket timeout) but nothing about the reported
                            # status/digest/completed has actually moved - the
                            # server is wedged behind a heartbeat. Force a
                            # reconnect via the same retry path below instead of
                            # trusting this connection any further.
                            raise requests.exceptions.ConnectionError(
                                f"no progress on {status_text or 'pull'!r} "
                                f"for over {int(limit)}s"
                            )

                    if progress_callback is not None:
                        progress_callback(
                            PullProgress(
                                status=str(event.get("status", "")),
                                digest=event.get("digest"),
                                total=event.get("total"),
                                completed=event.get("completed"),
                            )
                        )
                # Stream ended normally (Ollama closed the connection after
                # the final "success" event) - the pull is complete.
                return
        except PullCancelled:
            raise
        except (requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError) as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise PullCancelled()
            attempts += 1
            if attempts > _MAX_STALL_RETRIES:
                raise OllamaError(
                    f"Could not pull model {model_name!r}: repeatedly stuck "
                    "on 'pulling manifest' / stalled with no progress. This "
                    "almost always means Ollama's registry (registry.ollama"
                    ".ai) isn't reachable from this network - check your "
                    "internet connection, any VPN/proxy/firewall that might "
                    "block it, then try again. If it's reachable elsewhere, "
                    "restarting the Ollama server can also clear a wedged "
                    f"local daemon. ({exc})"
                ) from exc
            # Reset the no-progress tracker for the reconnected attempt so a
            # fresh "pulling manifest" isn't immediately judged against the
            # previous connection's stale clock.
            last_key = None
            last_change = time.monotonic()
            if progress_callback is not None:
                # Otherwise the UI is left showing whatever status the dead
                # connection last reported (e.g. a frozen "pulling
                # manifest") with zero indication anything is happening -
                # this makes the retry itself visible instead of looking
                # like the app silently hung.
                progress_callback(
                    PullProgress(status=f"Connection stalled - reconnecting ({attempts}/{_MAX_STALL_RETRIES})\u2026")
                )
            time.sleep(min(2.0 * attempts, 10.0))
            continue
        except requests.RequestException as exc:
            raise OllamaError(f"Could not pull model {model_name!r}: {exc}") from exc