"""The local engine installs itself on a machine whose owner cannot debug it.

Every decision here has to degrade rather than fail: a card that cannot be used
must cost seconds and a plain sentence, never a hang.
"""

import json

import pytest

from prospector import engine
from prospector.engine import (
    GPU_ASSET_EXCLUDE, MAX_GPU_FAILURES, MODELS, describe_engine,
    gpu_layers_for, load_state, resolve_server_asset, save_state,
)


THREE_B = next(m for m in MODELS if m["size_gb"] == 2.0)
SEVEN_B = next(m for m in MODELS if m["size_gb"] == 4.7)


# ---------------------------------------------------------------------------
# Layer budgeting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vram", [0.0, 1.0, 2.0, 3.0])
def test_a_small_card_is_not_used_at_all(vram):
    """Spilling across the bus is slower than the processor.

    "-ngl 99 and hope" does not fail loudly -- it produces a run that is slower
    than not trying, on a machine whose owner bought a graphics card.
    """
    assert gpu_layers_for(vram, THREE_B) == 0


def test_a_big_card_takes_the_whole_model():
    assert gpu_layers_for(12.0, THREE_B) == THREE_B["layers"]
    assert gpu_layers_for(24.0, SEVEN_B) == SEVEN_B["layers"]


def test_a_middling_card_takes_a_partial_offload():
    layers = gpu_layers_for(4.0, SEVEN_B)
    assert 0 < layers < SEVEN_B["layers"]


def test_the_budget_leaves_room_for_the_context_and_the_desktop():
    """All of VRAM is never claimed: the driver and the KV cache need theirs."""
    weights_per_layer = SEVEN_B["size_gb"] / SEVEN_B["layers"]
    claimed = gpu_layers_for(5.0, SEVEN_B) * weights_per_layer
    assert claimed < 5.0 - 0.8


# ---------------------------------------------------------------------------
# Asset selection
# ---------------------------------------------------------------------------

def _release(*names):
    return {"tag_name": "b9999",
            "assets": [{"name": n, "browser_download_url": f"https://x/{n}"}
                       for n in names]}


def test_the_graphics_build_never_falls_back_to_a_cuda_archive(monkeypatch):
    """The CPU path's "any x64 zip" rescue is wrong on the GPU path.

    CUDA ships its runtime as a *second* download matched to the driver, so
    silently accepting that archive produces a server that dies at launch with
    a missing-DLL error the user cannot act on.
    """
    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return _release("llama-b9999-bin-win-cuda-12.4-x64.zip",
                                        "llama-b9999-bin-win-cpu-x64.zip")

    monkeypatch.setattr(engine.httpx, "get", lambda *a, **k: Resp())
    with pytest.raises(RuntimeError, match="graphics-card build"):
        resolve_server_asset(gpu=True)


def test_the_graphics_build_is_found_when_it_is_there(monkeypatch):
    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return _release("llama-b9999-bin-win-cuda-12.4-x64.zip",
                                        "llama-b9999-bin-win-vulkan-x64.zip",
                                        "llama-b9999-bin-win-cpu-x64.zip")

    monkeypatch.setattr(engine.httpx, "get", lambda *a, **k: Resp())
    url, tag = resolve_server_asset(gpu=True)
    assert "vulkan" in url
    assert tag == "b9999"


def test_the_processor_build_never_picks_a_gpu_archive(monkeypatch):
    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return _release("llama-b9999-bin-win-vulkan-x64.zip",
                                        "llama-b9999-bin-win-cpu-x64.zip")

    monkeypatch.setattr(engine.httpx, "get", lambda *a, **k: Resp())
    url, _ = resolve_server_asset()
    assert "vulkan" not in url
    assert "cpu" in url


def test_cuda_and_rocm_are_excluded_from_the_graphics_build_too():
    """Vulkan is the only backend that needs no separate runtime install."""
    for name in ("cuda", "hip", "rocm", "sycl"):
        assert name in GPU_ASSET_EXCLUDE


# ---------------------------------------------------------------------------
# Remembering what worked
# ---------------------------------------------------------------------------

def test_engine_state_survives_a_restart():
    save_state(backend="vulkan", n_gpu_layers=28, speedup=3.4)
    assert load_state()["backend"] == "vulkan"
    save_state(gpu_failures=1)
    assert load_state()["n_gpu_layers"] == 28   # merged, not replaced


def test_a_card_that_keeps_failing_is_given_up_on():
    save_state(gpu_failures=MAX_GPU_FAILURES)
    assert load_state()["gpu_failures"] >= MAX_GPU_FAILURES


def test_unreadable_state_is_not_an_error():
    engine.state_path().parent.mkdir(parents=True, exist_ok=True)
    engine.state_path().write_text("{ not json")
    assert load_state() == {}


@pytest.mark.parametrize("state,expected", [
    ({}, ""),
    ({"backend": "cpu"}, "processor"),
    ({"backend": "cpu", "gpu_rejected_rate": 40.0}, "not faster"),
    ({"backend": "cpu", "gpu_failures": MAX_GPU_FAILURES}, "driver"),
    ({"backend": "vulkan", "speedup": 3.4}, "3.4x faster"),
])
def test_the_engine_explains_itself_without_naming_a_product(state, expected):
    """Nothing here may leak Vulkan, llama.cpp, GGUF, VRAM or a layer count."""
    engine.state_path().parent.mkdir(parents=True, exist_ok=True)
    engine.state_path().write_text(json.dumps(state))

    described = describe_engine()
    text = f"{described['where']} {described['detail']}"
    assert expected in text
    for jargon in ("vulkan", "llama", "gguf", "vram", "ngl", "cuda", "layer"):
        assert jargon not in text.lower()


def test_a_build_without_timings_is_still_measured(monkeypatch):
    """Returning 0.0 defeated the whole point of measuring.

    The caller treats a zero on either side as "no comparison available" and
    keeps the graphics card, so on any llama-server build that omits `timings`
    the card was adopted with no evidence at all -- which is exactly the
    situation the probe exists to prevent.
    """
    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "OK"}}]}

    monkeypatch.setattr(engine.httpx, "post", lambda *a, **k: Resp())
    assert engine.measure_prefill("http://127.0.0.1:1/v1", "m") > 0


def test_asking_for_a_specific_model_never_returns_a_different_one(tmp_path, monkeypatch):
    """The picker appeared to do nothing, and the error blamed the wrong model."""
    monkeypatch.setattr(engine, "models_dir", lambda: tmp_path)
    (tmp_path / "qwen2.5-7b-instruct-q4_k_m.gguf").write_bytes(b"GGUF" + b"0" * 100)

    assert engine.find_model("qwen2.5-1.5b-instruct-q4_k_m") == ""
    assert engine.find_model("qwen2.5-7b-instruct-q4_k_m").endswith("7b-instruct-q4_k_m.gguf")
    # With no id at all, any downloaded model is still fine.
    assert engine.find_model() != ""
