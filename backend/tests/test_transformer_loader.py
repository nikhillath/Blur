"""The detector loader must be safe under concurrent startup and must not remember a failure forever."""
import threading
import time

import pytest

from app.core.config import get_settings
from app.ml import transformer


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(get_settings(), "ner_model", "fake/model")
    transformer.reset_transformer_detector()
    yield
    transformer.reset_transformer_detector()


def test_disabled_when_no_model_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "ner_model", "")
    assert transformer.get_transformer_detector() is None


def test_concurrent_first_use_builds_exactly_one_detector(monkeypatch):
    built = []

    class Slow:
        def __init__(self, *a, **k):
            time.sleep(0.3)  # a slow load keeps the other threads waiting on the lock
            built.append(self)

    monkeypatch.setattr(transformer, "TransformerDetector", Slow)
    results = []
    threads = [threading.Thread(target=lambda: results.append(transformer.get_transformer_detector())) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(built) == 1 and len({id(r) for r in results}) == 1 and results[0] is built[0]


def test_failure_is_retried_after_backoff_not_cached_forever(monkeypatch):
    calls = {"n": 0}

    class Flaky:
        def __init__(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ImportError("simulated import race")

    monkeypatch.setattr(transformer, "TransformerDetector", Flaky)
    assert transformer.get_transformer_detector() is None          # first attempt fails
    assert transformer.get_transformer_detector() is None          # inside the back-off window: no hammering
    assert calls["n"] == 1
    monkeypatch.setattr(transformer, "RETRY_AFTER_SECONDS", 0)     # window elapsed
    det = transformer.get_transformer_detector()
    assert det is not None and calls["n"] == 2                     # recovered without a restart
    assert transformer.get_transformer_detector() is det           # and is now cached


def test_pipeline_reports_ner_error_when_model_cannot_load(monkeypatch):
    from app.ml import pipeline

    monkeypatch.setattr(transformer, "TransformerDetector", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(pipeline, "get_transformer_detector", transformer.get_transformer_detector)
    out = pipeline.run_detection("Mail a@example.com", None)
    assert out.mode == "rules-only" and "ner_error" in out.config  # never silent
