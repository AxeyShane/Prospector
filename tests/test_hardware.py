"""Local AI is CPU-first with an opportunistic graphics card, so the questions
are memory, real cores, and whether the answer is "possible" or "useful"."""

from prospector.engine import MODELS, choose_model
from prospector.hardware import (
    Hardware, detect, estimate_minutes, recommend, seconds_per_call,
)


def hw(ram=16.0, cores=8, gpu="", vram=0.0):
    return Hardware(os_name="Windows", os_version="11", arch="AMD64",
                    cpu_cores=cores * 2 if cores > 1 else 1,
                    ram_gb=ram, gpu_name=gpu,
                    physical_cores=cores, gpu_vram_gb=vram)


def test_a_capable_pc_is_offered_local_ai():
    rec = recommend(hw(ram=32, cores=16))
    assert rec["can_run_local"] is True
    assert rec["label"]
    assert "free" in rec["reason"]


def test_the_model_steps_down_with_memory():
    labels = [recommend(hw(ram=r, cores=8))["label"] for r in (32, 16, 8)]
    assert labels == ["Larger model", "Larger model", "Standard model"]


def test_a_weak_pc_is_told_to_use_the_cloud():
    rec = recommend(hw(ram=4, cores=2))
    assert rec["can_run_local"] is False
    assert "cloud" in rec["reason"]


def test_a_single_core_machine_is_rejected_however_much_memory_it_has():
    rec = recommend(hw(ram=64, cores=1))
    assert rec["can_run_local"] is False
    # Grammar matters in text the user reads: "1 cores" is sloppy.
    assert "1 core," in rec["reason"]


def test_a_capable_gpu_is_offered_and_the_answer_is_hedged():
    """The card is opportunistic: offered, measured, kept only if it helps.

    The wording must not promise a speed-up the probe has not proved yet, or a
    machine whose driver silently falls back to the CPU has been lied to.
    """
    rec = recommend(hw(ram=32, cores=16, gpu="NVIDIA RTX 4090", vram=24.0))
    assert rec["can_run_local"] is True
    assert rec["gpu_usable"] is True
    assert "graphics card" in rec["reason"]
    assert "try to use it" in rec["reason"]


def test_a_gpu_with_too_little_memory_is_explained_rather_than_ignored():
    """The user will ask why their card is idle, so answer before they ask."""
    rec = recommend(hw(ram=32, cores=16, gpu="Intel UHD Graphics", vram=0.5))
    assert rec["gpu_usable"] is False
    assert "too little memory" in rec["reason"]


def test_the_time_estimate_counts_every_call_and_the_search_floor():
    """It used to promise "about 5 minutes" for a run that took several hours.

    Three local calls are made per company, not one, and roughly seven throttled
    web searches happen per company whatever the processor does.
    """
    fast = recommend(hw(ram=32, cores=16))
    assert fast["minutes_per_hundred"] >= 30
    assert estimate_minutes(100, hw(ram=32, cores=16)) > \
        estimate_minutes(100, hw(ram=32, cores=16), speedup=4.0)
    # Search alone puts a floor under it even with an infinitely fast processor.
    assert estimate_minutes(100, hw(ram=32, cores=16), speedup=1000, cloud_share=1.0) >= 15


def test_the_time_estimate_is_shown_and_scales_with_cores():
    fast = recommend(hw(ram=32, cores=16))
    slow = recommend(hw(ram=32, cores=4))
    assert fast["seconds_per_call"] < slow["seconds_per_call"]
    assert "minutes" in fast["reason"]
    assert seconds_per_call(16) < seconds_per_call(2)


def test_detect_never_raises_on_this_machine():
    result = detect()
    assert result.cpu_cores >= 1
    assert result.summary()


def test_every_catalogue_model_is_fully_specified():
    for model in MODELS:
        assert model["id"] and model["label"] and model["quality"]
        assert model["ram_gb"] > 0 and model["size_gb"] > 0
        assert model["urls"] and all(u.startswith("https://") for u in model["urls"])
        # More than one publisher, so a repository rename cannot brick setup.
        assert len(model["urls"]) >= 2
    # Ordered largest first, because choose_model takes the first that fits.
    assert [m["ram_gb"] for m in MODELS] == sorted((m["ram_gb"] for m in MODELS),
                                                   reverse=True)


def test_choose_model_always_returns_something():
    assert choose_model(0.5, 1)["id"] == MODELS[-1]["id"]
    assert choose_model(64, 32)["id"] == MODELS[0]["id"]


def test_relevance_never_rounds_a_hedged_answer_upward():
    """Walking the ladder High-first made every hedge resolve to High.

    "Medium-High" and even "Low, certainly not High" landed at the top of the
    call list. An uncertain answer should cost a scroll, not a phone call.
    """
    from prospector.stages.classify import RELEVANCE_LEVELS, classify_one

    class FakeLLM:
        def __init__(self, answer): self.answer = answer
        def ask_json(self, prompt, **kw):
            return {"relevance": self.answer, "category": "Crushing",
                    "products": "x", "entity_type": "Manufacturer",
                    "country": "India", "origin_country": "India",
                    "reasoning": "r"}

    import prospector.stages.classify as mod
    row = {"company_name": "Acme", "website": "", "pages_json": None}

    for answer, forbidden in (("Medium-High", "High"),
                              ("Low, certainly not High", "High"),
                              ("not relevant at all", "High")):
        mod.get_client = lambda stage="default", a=answer: FakeLLM(a)
        got = classify_one(row, {})["relevance"]
        assert got in RELEVANCE_LEVELS
        assert got != forbidden, f"{answer!r} resolved to {got}"
