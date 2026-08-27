"""The interface is used by someone who cannot read a stack trace.

Every string that can reach the screen has to be a sentence with a next step in
it. These tests guard the translation layer and the plain-language labels.
"""

import httpx
import pytest

from prospector.webui import (
    CLOUD_COST_HINT, CLOUD_MODELS, _first_free_port, friendly_error,
)


JARGON = ("gguf", "huggingface", "traceback", "errno", "nonetype", "http://",
          "https://", "getaddrinfo", "0x", "winerror", "llama", "vulkan",
          "openrouter/", "exception")


@pytest.mark.parametrize("exc", [
    PermissionError(13, "Permission denied", r"C:\Users\a\leads_20260821.xlsx"),
    RuntimeError("HTTPStatusError: Client error '404 Not Found' for url "
                 "'https://huggingface.co/Qwen/x.gguf?download=true'"),
    OSError(28, "No space left on device"),
    httpx.ConnectError("getaddrinfo failed"),
    KeyError("choices"),
    AttributeError("'NoneType' object has no attribute 'text'"),
    ValueError("invalid literal for int() with base 10: ''"),
])
def test_no_technical_detail_ever_reaches_the_screen(exc):
    text = friendly_error(exc, "researching")
    for jargon in JARGON:
        assert jargon not in text.lower(), f"{jargon!r} leaked: {text}"
    assert text.endswith((".", "!", "?"))
    assert text[:1].isupper()


def test_the_excel_is_open_case_is_named_because_it_is_the_common_one():
    text = friendly_error(
        PermissionError(13, "Permission denied", r"C:\Users\a\leads.xlsx"),
        "building the spreadsheet")
    assert "Excel" in text and "Close it" in text


def test_messages_we_wrote_ourselves_are_passed_through_unchanged():
    written = ("Web search has stopped responding - wait ten minutes and press "
               "Continue.")
    assert friendly_error(RuntimeError(written)) == written


def test_every_error_says_what_to_do_next():
    for exc in (OSError(28, "No space left on device"),
                httpx.ConnectError("getaddrinfo failed"),
                KeyError("choices")):
        text = friendly_error(exc, "researching").lower()
        assert any(cue in text for cue in
                   ("press", "check", "close", "add", "pick", "free", "wait"))


def test_model_choices_are_described_by_what_they_do():
    """A picker listing "meta-llama/llama-3.3-70b-instruct" asks the user to
    make a choice they have no way to make."""
    for model_id, label in CLOUD_MODELS:
        assert "/" not in label
        assert label[0].isupper()
        assert model_id in CLOUD_COST_HINT, f"{model_id} has no cost hint"


def test_the_status_line_names_no_model(monkeypatch):
    from prospector.llm import describe

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    text = describe()
    assert "gemini" not in text.lower()
    assert "/" not in text


def test_a_busy_port_is_stepped_over_rather_than_reported(monkeypatch):
    """It used to print "run: prospector serve --port 8741" -- a command line
    instruction to someone who by definition cannot use one."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        busy = taken.getsockname()[1]
        taken.listen(1)
        assert _first_free_port(busy) != busy
