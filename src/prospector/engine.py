"""Local AI engine -- a CPU llama-server that Prospector installs itself.

The user is assumed to have nothing installed and to have no idea what any of
this is, so nothing in here surfaces a product name. The app says "local AI",
and the mechanics stay mechanics.

CPU only, on purpose. Dropping GPU support removes the whole backend-variant
problem (CUDA 11 vs 12 vs Vulkan vs HIP) and shrinks the download from the
better part of a gigabyte to about thirty megabytes. Work that genuinely needs
a big model goes to the cloud instead, which is both faster and better than a
quantised model straining on a laptop GPU.

Two things are resolved at runtime rather than hardcoded:

  * the server binary, looked up through the GitHub releases API, because the
    asset filename carries a build number that changes every release
  * the model file, tried against more than one publisher, because a single
    hardcoded URL is one repository rename away from bricking the installer

Both are verified after download -- a truncated zip or an HTML error page
saved as a .gguf must fail loudly here, not later as a baffling crash.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Callable

import httpx

from prospector import config

log = logging.getLogger(__name__)

Progress = Callable[[dict], None]

RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"

# Asset name fragments, best first. Newer releases ship "bin-win-cpu-x64";
# older ones used the instruction-set name.
ASSET_PREFERENCES = ("bin-win-cpu-x64", "bin-win-avx2-x64", "bin-win-avx-x64",
                     "bin-win-noavx-x64")

# The graphics-card build. Vulkan and nothing else, deliberately: it is the one
# backend that ships as a single self-contained zip and runs on NVIDIA, AMD and
# Intel using the display driver the machine already has. CUDA needs a second
# download matched to the driver version, ROCm needs a runtime install, and a
# wrong guess between them produces an app that appears to hang -- which is the
# whole reason GPU support was left out to begin with.
GPU_ASSET_PREFERENCES = ("bin-win-vulkan-x64",)
ASSET_EXCLUDE = ("cuda", "hip", "rocm", "vulkan", "sycl", "arm64", "musa", "cann")
GPU_ASSET_EXCLUDE = ("cuda", "hip", "rocm", "sycl", "arm64", "musa", "cann")

# Everything the engine decided last time: which build, how many layers went to
# the card, and which driver proved it. A driver update is the likeliest way a
# working setup breaks, so it is recorded and compared.
STATE_FILE = "engine.json"

# Two consecutive graphics-card failures and it stays on the processor. Trying
# forever is how a broken driver turns into an app that never starts.
MAX_GPU_FAILURES = 2

# Concurrent requests the local server accepts. Without this llama-server has a
# single slot, so the six workers the pipeline runs did not overlap at all --
# five of them sat in the server's queue while one worked, and the client-side
# timeout counted their queued time against them.
LOCAL_PARALLEL = 3

# Context per slot. llama-server divides the total context between slots, so
# this is multiplied up rather than shared out.
CONTEXT_PER_SLOT = 4096

# Ports checked for a server the user is already running, before we start one.
# 8080 first because that is llama-server's own default.
PROBE_PORTS = (8080, 8081, 8000, 1234, 11434)
OUR_PORT = 8779          # deliberately not 8080, so we never fight an existing one

# CPU model catalogue, largest first. `ram_gb` is what the machine needs to run
# it without swapping. Sizes are approximate download sizes.
MODELS = [
    {"id": "qwen2.5-7b-instruct-q4_k_m", "label": "Larger model",
     "ram_gb": 16, "size_gb": 4.7, "threads_min": 8, "layers": 28,
     "quality": "Best quality on this PC, but noticeably slower",
     "urls": [
         "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m.gguf?download=true",
         "https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf?download=true",
     ]},
    {"id": "qwen2.5-3b-instruct-q4_k_m", "label": "Standard model",
     "ram_gb": 8, "size_gb": 2.0, "threads_min": 4, "layers": 36,
     "quality": "Good balance of speed and accuracy - the usual choice",
     "urls": [
         "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf?download=true",
         "https://huggingface.co/bartowski/Qwen2.5-3B-Instruct-GGUF/resolve/main/Qwen2.5-3B-Instruct-Q4_K_M.gguf?download=true",
     ]},
    {"id": "qwen2.5-1.5b-instruct-q4_k_m", "label": "Small model",
     "ram_gb": 4, "size_gb": 1.0, "threads_min": 2, "layers": 28,
     "quality": "Fast on modest hardware; less reliable on tricky pages",
     "urls": [
         "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf?download=true",
         "https://huggingface.co/bartowski/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/Qwen2.5-1.5B-Instruct-Q4_K_M.gguf?download=true",
     ]},
]

GGUF_MAGIC = b"GGUF"

_process: subprocess.Popen | None = None


def _emit(progress: Progress | None, **fields) -> None:
    if progress:
        try:
            progress(fields)
        except Exception:  # noqa: BLE001 - a broken listener must not stop a download
            log.exception("progress callback failed")


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def engine_dir(backend: str = "cpu") -> Path:
    """Where a build lives. Backends are kept apart so both can coexist.

    Sharing one directory would mean a failed graphics-card attempt overwrites
    the working processor build, and the fallback then has nothing to fall back
    to.
    """
    base = Path(config.SHARED_DIR) / "engine"
    return base if backend == "cpu" else base / backend


def models_dir() -> Path:
    return Path(config.SHARED_DIR) / "models"


def find_server(backend: str = "cpu") -> str:
    """Path to a llama-server executable, ours preferred. "" if absent."""
    exe = "llama-server.exe" if os.name == "nt" else "llama-server"

    for candidate in engine_dir(backend).rglob(exe):
        return str(candidate)

    if backend != "cpu":
        return ""       # never answer a GPU question with the CPU build

    on_path = shutil.which(exe)
    return on_path or ""


def state_path() -> Path:
    return Path(config.SHARED_DIR) / STATE_FILE


def load_state() -> dict:
    """What the engine worked out last time. Never raises."""
    try:
        return json.loads(state_path().read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_state(**fields) -> dict:
    state = load_state()
    state.update(fields)
    try:
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(json.dumps(state, indent=2))
    except OSError:
        log.warning("could not save engine state", exc_info=True)
    return state


def find_model(model_id: str = "") -> str:
    """Path to a downloaded model file. Any of them only if no id was asked for.

    Asking for a specific model and being handed a different one is worse than
    being told it is missing. It used to fall through to the largest file on
    disk, so a user who set up on a big machine and later chose "Small model"
    got the large one started anyway -- the picker appeared to do nothing, and
    on a machine short of memory the server then timed out into "this PC may be
    too slow for the chosen model", which was advice about the wrong model.
    """
    folder = models_dir()
    if not folder.exists():
        return ""
    if model_id:
        exact = folder / f"{model_id}.gguf"
        return str(exact) if exact.exists() else ""
    files = sorted(folder.glob("*.gguf"), key=lambda p: p.stat().st_size, reverse=True)
    return str(files[0]) if files else ""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _openai_probe(port: int, timeout: float = 1.5) -> list[str]:
    """Models served by an OpenAI-compatible server on this port, [] if none."""
    for path in ("/v1/models", "/api/tags"):
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=timeout)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if "data" in data:
                return [m.get("id", "") for m in data["data"] if m.get("id")]
            if "models" in data:
                return [m.get("name", "") for m in data["models"] if m.get("name")]
        except Exception:  # noqa: BLE001
            continue
    return []


def detect_running() -> list[dict]:
    """Local AI servers already answering on this machine.

    Worth checking first: this user already runs a llama-server for another
    project, and reusing it means downloading nothing at all.
    """
    found = []
    for port in PROBE_PORTS + (OUR_PORT,):
        models = _openai_probe(port)
        if models:
            found.append({"port": port, "url": f"http://127.0.0.1:{port}/v1",
                          "models": models})
    return found


def _free_port(preferred: int = OUR_PORT) -> int:
    for port in (preferred, preferred + 1, preferred + 2, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
            except OSError:
                continue
    return preferred


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _stream_download(url: str, target: Path, progress: Progress | None,
                     label: str, step: str) -> int:
    """Download to a .part file then rename. Returns bytes written."""
    part = target.with_suffix(target.suffix + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    done = 0

    # A read timeout, not None. `timeout=None` waits forever on a stalled
    # connection, so a download that dies mid-stream hung the setup screen with
    # a progress bar that never moved again and no way to tell it had stopped.
    timeout = httpx.Timeout(connect=30.0, read=120.0, write=120.0, pool=30.0)
    with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        last = 0.0
        with part.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1024 * 512):
                fh.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last > 0.5:
                    last = now
                    pct = int(100 * done / total) if total else 0
                    size = f" ({done / 1e6:.0f} MB of {total / 1e6:.0f} MB)" if total else \
                           f" ({done / 1e6:.0f} MB)"
                    _emit(progress, step=step, status="running", percent=pct,
                          message=f"{label}...{size}")

    # A server that closes early still looks like a clean finish to the loop
    # above. Comparing against the advertised length is what catches it -- a
    # truncated GGUF passes the magic-byte check, is cached by the "file exists
    # and is big" test forever, and then fails at load time as "not enough
    # memory", which sends the user off buying RAM they do not need.
    if total and done < total * 0.995:
        part.unlink(missing_ok=True)
        raise RuntimeError(
            f"The download stopped early ({done / 1e6:.0f} MB of "
            f"{total / 1e6:.0f} MB). Press Set up again to retry.")

    # Rename only once the download completed, so an interrupted run never
    # leaves a half-file that looks finished.
    part.replace(target)
    return done


def resolve_server_asset(gpu: bool = False) -> tuple[str, str]:
    """Find the current Windows build. Returns (url, release_tag).

    Looked up live rather than hardcoded: the filename carries a build number
    that changes every release, so any URL written here would rot within weeks.

    With `gpu=True` this looks for the Vulkan build and *fails* rather than
    falling back. The CPU path's "take any x64 zip" rescue is right there and
    wrong here: on the GPU path it would happily return the CUDA archive, which
    needs a separate runtime download and would fail at launch with an error
    about a missing DLL.
    """
    try:
        resp = httpx.get(RELEASES_API, timeout=30,
                         headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        release = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 429):
            raise RuntimeError(
                "The download service is temporarily refusing requests from "
                "this connection. Wait fifteen minutes and press Set up again, "
                "or use the cloud option instead."
            ) from exc
        raise RuntimeError(
            "Could not reach the download service to fetch the local AI engine. "
            "Check your internet connection, or use the cloud option instead."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Could not reach the download service to fetch the local AI engine. "
            "Check your internet connection, or use the cloud option instead."
        ) from exc

    assets = release.get("assets") or []
    names = [(a.get("name", ""), a.get("browser_download_url", "")) for a in assets]
    exclude = GPU_ASSET_EXCLUDE if gpu else ASSET_EXCLUDE
    prefer = GPU_ASSET_PREFERENCES if gpu else ASSET_PREFERENCES

    windows = [(n, u) for n, u in names
               if n.lower().endswith(".zip")
               and "win" in n.lower()
               and not any(x in n.lower() for x in exclude)]

    for fragment in prefer:
        for name, url in windows:
            if fragment in name.lower():
                return url, release.get("tag_name", "")

    if gpu:
        # No fallback here on purpose -- see the docstring.
        raise RuntimeError("No graphics-card build is available in this release.")

    # Naming changed: take any plain Windows x64 zip rather than giving up.
    for name, url in windows:
        if "x64" in name.lower():
            return url, release.get("tag_name", "")

    raise RuntimeError(
        "No suitable local AI engine build was found for this computer. "
        "Use the cloud option instead."
    )


def download_server(progress: Progress | None = None, backend: str = "cpu") -> str:
    """Fetch and unpack the server. Returns the executable path."""
    if platform.system() != "Windows":
        raise RuntimeError(
            "Prospector can only install the local AI engine automatically on "
            "Windows. Use the cloud option instead."
        )

    url, tag = resolve_server_asset(gpu=(backend != "cpu"))
    target = engine_dir(backend)
    target.mkdir(parents=True, exist_ok=True)
    archive = Path(config.SHARED_DIR) / f"engine-{backend}.zip"

    _emit(progress, step="engine", status="starting",
          message=("Downloading the graphics-card engine (about 80 MB)..."
                   if backend != "cpu"
                   else "Downloading the local AI engine (about 30 MB)..."))
    try:
        _stream_download(url, archive, progress, "Downloading the local AI engine", "engine")
    except Exception as exc:  # noqa: BLE001
        archive.unlink(missing_ok=True)
        raise RuntimeError(
            f"Could not download the local AI engine: {exc}. Check your internet "
            f"connection, or use the cloud option instead."
        ) from exc

    _emit(progress, step="engine", status="running", message="Unpacking...")
    try:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(target)
    except zipfile.BadZipFile as exc:
        archive.unlink(missing_ok=True)
        raise RuntimeError(
            "The download was incomplete. Press Set up again to retry."
        ) from exc
    finally:
        archive.unlink(missing_ok=True)

    exe = find_server(backend)
    if not exe:
        raise RuntimeError(
            "The local AI engine unpacked but its program file was not found "
            "inside. Use the cloud option instead."
        )

    log.info("local engine installed from release %s", tag)
    _emit(progress, step="engine", status="done", message="Local AI engine installed.")
    return exe


def download_model(model: dict, progress: Progress | None = None,
                   url_override: str = "") -> str:
    """Fetch a model file, trying each publisher in turn. Returns its path."""
    target = models_dir() / f"{model['id']}.gguf"
    if target.exists() and target.stat().st_size > 1_000_000:
        return str(target)

    urls = [url_override] if url_override else list(model["urls"])
    errors: list[str] = []

    for index, url in enumerate(urls, 1):
        source = "" if len(urls) == 1 else f" (source {index} of {len(urls)})"
        _emit(progress, step="model", status="starting",
              message=f"Downloading the AI model, about {model['size_gb']} GB{source}...")
        try:
            written = _stream_download(
                url, target, progress,
                f"Downloading the AI model{source}", "model")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
            continue

        # A repository that has moved answers 200 with an HTML error page. Left
        # unchecked that lands on disk as a .gguf and fails much later with an
        # incomprehensible error, so it is caught here instead.
        try:
            with target.open("rb") as fh:
                magic = fh.read(4)
        except OSError as exc:
            errors.append(str(exc))
            continue

        if magic != GGUF_MAGIC or written < 100_000:
            target.unlink(missing_ok=True)
            errors.append("the downloaded file was not a valid model")
            continue

        _emit(progress, step="model", status="done", message="AI model ready.")
        return str(target)

    raise RuntimeError(
        "Could not download the AI model. " + "; ".join(errors[:2]) +
        ". Use the cloud option instead, or paste a direct model link in "
        "Advanced settings."
    )


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def gpu_layers_for(vram_gb: float, model: dict) -> int:
    """How many layers this card can hold. 0 means do not bother.

    Budgeted rather than set to "all of them". `-ngl 99` on a card that cannot
    hold the model does not fail -- it spills across the bus and runs slower
    than the processor would have, which is worse than not trying, because the
    user paid for a graphics card and got a slowdown.

    Reserve: ~0.8 GB for the driver and desktop, plus the KV cache for the
    context we ask for. Whatever is left divides by the per-layer size.
    """
    from prospector.hardware import MIN_USEFUL_VRAM_GB

    # The floor lives with the decision, not only with the caller. A 3 GB card
    # can technically hold two thirds of a 3B model, but the third that stays on
    # the processor is copied across the bus for every token, which is a wash at
    # best -- and the measurement below would reject it anyway after spending
    # forty seconds finding out.
    if vram_gb < MIN_USEFUL_VRAM_GB:
        return 0
    layers = int(model.get("layers", 36))
    weights_gb = float(model.get("size_gb", 2.0))
    kv_gb = (CONTEXT_PER_SLOT * LOCAL_PARALLEL) / 8192 * 0.5

    usable = vram_gb - 0.8 - kv_gb
    if usable <= 0:
        return 0

    per_layer = weights_gb / max(layers, 1)
    fits = int(usable / per_layer) if per_layer > 0 else 0
    if fits >= layers:
        return layers
    # A handful of layers is not worth a second engine download and the risk of
    # a driver that misbehaves.
    return fits if fits >= max(8, layers // 4) else 0


def _server_log() -> Path:
    return Path(config.SHARED_DIR) / "engine.log"


def start_server(model_path: str, progress: Progress | None = None,
                 port: int = 0, timeout: int = 180, backend: str = "cpu",
                 n_gpu_layers: int = 0, threads: int = 0) -> str:
    """Start the local server and wait for it to answer. Returns its base URL."""
    global _process

    port = port or _free_port()
    exe = find_server(backend)
    if not exe:
        raise RuntimeError("The local AI engine is not installed yet.")

    if not threads:
        from prospector.hardware import detect
        hw = detect()
        threads = max(2, (hw.physical_cores or hw.cpu_cores) - 1)

    cmd = [exe, "-m", model_path, "--host", "127.0.0.1", "--port", str(port),
           # Total context is divided between slots, so it is multiplied here
           # rather than shared out.
           "-c", str(CONTEXT_PER_SLOT * LOCAL_PARALLEL),
           "-t", str(threads),
           # Without this the server has exactly one slot: the pipeline's
           # workers queued behind each other and the concurrency was a fiction.
           "--parallel", str(LOCAL_PARALLEL)]
    if n_gpu_layers > 0:
        cmd += ["-ngl", str(n_gpu_layers)]

    _emit(progress, step="start", status="running",
          message="Starting the local AI. The first start takes a minute...")

    creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    # Output goes to a file rather than to DEVNULL. Discarding it made every
    # failure here undiagnosable -- and the graphics-card fallback needs to read
    # the reason to know whether trying again is pointless.
    try:
        _server_log().parent.mkdir(parents=True, exist_ok=True)
        logfile = _server_log().open("wb")
    except OSError:
        logfile = subprocess.DEVNULL

    try:
        _process = subprocess.Popen(cmd, stdout=logfile, stderr=subprocess.STDOUT,
                                    creationflags=creation)
    except OSError as exc:
        raise RuntimeError(f"Could not start the local AI: {exc}") from exc

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _process.poll() is not None:
            raise RuntimeError(
                "The local AI stopped immediately after starting. " +
                _log_hint()
            )
        if _openai_probe(port, timeout=2):
            _emit(progress, step="start", status="done", message="Local AI running.")
            return f"http://127.0.0.1:{port}/v1"
        time.sleep(2)

    stop_server()
    raise RuntimeError(
        "The local AI did not finish starting. This PC may be too slow for the "
        "chosen model - try a smaller one, or use the cloud option."
    )


def _log_hint() -> str:
    """A plain-language reading of why the server died, from its own output."""
    try:
        tail = _server_log().read_text(errors="replace")[-4000:].lower()
    except OSError:
        tail = ""
    if any(k in tail for k in ("vk::", "vulkan", "device lost", "no devices found")):
        return ("The graphics card could not be used - Prospector will use the "
                "processor instead.")
    if any(k in tail for k in ("out of memory", "failed to allocate", "cannot allocate")):
        return ("There was not enough memory for the chosen model - try a "
                "smaller one, or use the cloud option.")
    return ("This PC may not have enough memory for the chosen model - try a "
            "smaller one, or use the cloud option.")


def measure_prefill(base_url: str, model_name: str) -> float:
    """Tokens per second reading a prompt. 0.0 if the server would not answer.

    Prompt processing rather than generation, because that is what this workload
    is: the Sorter reads a page and writes two lines, so prefill is where the
    time goes and where a graphics card either helps or does not.
    """
    words = ("crusher screen conveyor plant quarry mining aggregate export "
             "distributor subsidiary tonnage capacity hydraulic bearing ") * 40
    try:
        started = time.monotonic()
        resp = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json={"model": model_name,
                  "messages": [{"role": "user",
                                "content": f"Reply with OK only.\n{words}"}],
                  "max_tokens": 4, "temperature": 0.0},
            timeout=300,
        )
        resp.raise_for_status()
        elapsed = time.monotonic() - started

        timings = resp.json().get("timings") or {}
        rate = float(timings.get("prompt_per_second") or 0.0)
        if rate:
            return rate

        # Builds that do not report timings still get measured, by the clock.
        # Returning 0.0 here was worse than useless: the caller treats a zero on
        # either side as "no comparison available" and keeps the graphics card,
        # so on any such build the card was adopted with no evidence at all --
        # defeating the entire point of measuring.
        approx_prompt_tokens = len(words) / 4
        return round(approx_prompt_tokens / elapsed, 1) if elapsed > 0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def stop_server() -> None:
    """Stop the server we started. A pre-existing one is left alone."""
    global _process
    if _process and _process.poll() is None:
        _process.terminate()
        try:
            _process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _process.kill()
    _process = None


def verify(base_url: str, model_name: str) -> tuple[bool, str]:
    """One real completion, so "ready" means ready."""
    try:
        resp = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json={"model": model_name,
                  "messages": [{"role": "user", "content": "Reply with the word OK."}],
                  "max_tokens": 5},
            timeout=300,   # a cold model loads from disk on the first call
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"The local AI did not respond: {exc}"

    if resp.status_code != 200:
        return False, f"The local AI returned an error ({resp.status_code})."
    if "choices" not in resp.json():
        return False, "The local AI gave an unexpected answer."
    return True, "Local AI is working."


# ---------------------------------------------------------------------------
# The one call the app makes
# ---------------------------------------------------------------------------

def choose_model(ram_gb: float, cores: int) -> dict:
    """Largest catalogue model this machine can run on the CPU."""
    for model in MODELS:
        if ram_gb >= model["ram_gb"] and cores >= model["threads_min"]:
            return model
    return MODELS[-1]


def _try_graphics_card(model_path: str, model: dict, hw, progress) -> dict | None:
    """Install and measure the graphics-card build. None means "not worth it".

    Nothing here trusts the card. A device that enumerates can still be a
    software rasteriser, an old driver can load and produce nothing, and a
    remote-desktop session reports an adapter that cannot do this at all. So the
    processor is measured first, the card second, and the card is kept only if
    it actually turned out faster on this machine with this model.

    The cost of being wrong is a slow run the user cannot explain. The cost of
    checking is about forty seconds, once.
    """
    layers = gpu_layers_for(hw.gpu_vram_gb, model)
    if layers <= 0:
        return None

    _emit(progress, step="gpu", status="running",
          message="Checking whether your graphics card makes this faster...")

    # Baseline on the processor, using the build that is already installed.
    cpu_rate = 0.0
    try:
        url = start_server(model_path, port=_free_port(), backend="cpu")
        served = _openai_probe(int(url.split(":")[2].split("/")[0]))
        name = served[0] if served else Path(model_path).stem
        cpu_rate = measure_prefill(url, name)
    except Exception:  # noqa: BLE001
        log.info("processor baseline failed", exc_info=True)
    finally:
        stop_server()

    try:
        if not find_server("vulkan"):
            download_server(progress, backend="vulkan")
        url = start_server(model_path, port=_free_port(), backend="vulkan",
                           n_gpu_layers=layers)
        served = _openai_probe(int(url.split(":")[2].split("/")[0]))
        name = served[0] if served else Path(model_path).stem
        gpu_rate = measure_prefill(url, name)
    except Exception as exc:  # noqa: BLE001
        log.info("graphics card unusable: %s", exc)
        stop_server()
        state = load_state()
        save_state(gpu_failures=int(state.get("gpu_failures", 0)) + 1)
        _emit(progress, step="gpu", status="done",
              message="Your graphics card could not be used - using the processor.")
        return None

    # A margin, not a tie-break. Equal speed is not worth a second engine, a
    # second failure mode and a driver that might change under us.
    if cpu_rate and gpu_rate and gpu_rate < cpu_rate * 1.25:
        stop_server()
        save_state(gpu_failures=0, gpu_rejected_rate=round(gpu_rate, 1),
                   cpu_rate=round(cpu_rate, 1))
        _emit(progress, step="gpu", status="done",
              message=("Your graphics card is not faster than the processor "
                       "here, so the processor is being used."))
        return None

    speedup = round(gpu_rate / cpu_rate, 1) if cpu_rate and gpu_rate else 0.0
    save_state(backend="vulkan", n_gpu_layers=layers, gpu_failures=0,
               gpu_rate=round(gpu_rate, 1), cpu_rate=round(cpu_rate, 1),
               gpu_name=hw.gpu_name, speedup=speedup)
    _emit(progress, step="gpu", status="done",
          message=(f"Using your graphics card - about {speedup:g}x faster."
                   if speedup else "Using your graphics card."))
    return {"url": url, "model_path": model_path, "layers": layers}


def setup(progress: Progress | None = None, model_id: str = "",
          url_override: str = "", use_gpu: bool | None = None) -> dict:
    """Get local AI working, whatever state this machine starts in."""
    from prospector.hardware import detect

    # 1. Already running something? Use it and download nothing -- but prove it
    #    answers first. This used to return an unverified server, so a stale or
    #    wedged process on port 8080 was adopted and every later call failed.
    for server in detect_running():
        if not server["models"]:
            continue
        ok, _ = verify(server["url"], server["models"][0])
        if ok:
            _emit(progress, step="detect", status="done",
                  message="Found local AI already running on this PC - using it.")
            return {"LOCAL_BASE_URL": server["url"], "LOCAL_MODEL": server["models"][0]}
        log.info("ignoring unresponsive server at %s", server["url"])

    hw = detect()
    model = next((m for m in MODELS if m["id"] == model_id), None) or \
        choose_model(hw.ram_gb, hw.cpu_cores)

    if not find_server("cpu"):
        _emit(progress, step="detect", status="done",
              message="Setting up local AI on this PC...")
        download_server(progress, backend="cpu")

    model_path = find_model(model["id"]) or download_model(model, progress, url_override)

    state = load_state()
    wants_gpu = hw.gpu_usable if use_gpu is None else bool(use_gpu)
    if int(state.get("gpu_failures", 0)) >= MAX_GPU_FAILURES:
        # It has been tried and it broke. Stop asking.
        wants_gpu = False

    running = None
    if wants_gpu and os.name == "nt":
        running = _try_graphics_card(model_path, model, hw, progress)

    if running:
        base_url = running["url"]
        backend = "vulkan"
    else:
        save_state(backend="cpu", n_gpu_layers=0)
        base_url = start_server(model_path, progress, backend="cpu")
        backend = "cpu"

    # llama-server reports the model under the file name it was given.
    served = _openai_probe(int(base_url.split(":")[2].split("/")[0]))
    model_name = served[0] if served else Path(model_path).stem

    ok, message = verify(base_url, model_name)
    if not ok:
        stop_server()
        if backend != "cpu":
            # The card passed its own probe and then failed a real request.
            # Count it, drop back, and try once on the processor rather than
            # handing the user a dead engine.
            save_state(gpu_failures=int(load_state().get("gpu_failures", 0)) + 1,
                       backend="cpu", n_gpu_layers=0)
            base_url = start_server(model_path, progress, backend="cpu")
            served = _openai_probe(int(base_url.split(":")[2].split("/")[0]))
            model_name = served[0] if served else Path(model_path).stem
            ok, message = verify(base_url, model_name)
        if not ok:
            stop_server()
            raise RuntimeError(message)

    where = "graphics card" if backend != "cpu" else "processor"
    _emit(progress, step="done", status="done",
          message=f"Local AI ready ({model['label']}, using your {where}).")
    return {"LOCAL_BASE_URL": base_url, "LOCAL_MODEL": model_name}


def describe_engine() -> dict:
    """What the local engine settled on, in plain language for the app."""
    state = load_state()
    backend = state.get("backend", "")
    if not backend:
        return {"configured": False, "where": "", "detail": ""}
    if backend == "cpu":
        detail = "Running on this PC's processor."
        if state.get("gpu_rejected_rate"):
            detail += (" Your graphics card was tried and was not faster, "
                       "so it is not being used.")
        elif int(state.get("gpu_failures", 0)) >= MAX_GPU_FAILURES:
            detail += (" Your graphics card could not be used - its driver "
                       "may need updating.")
        return {"configured": True, "where": "processor", "detail": detail}

    speedup = state.get("speedup") or 0
    detail = "Running on your graphics card"
    detail += f", about {speedup:g}x faster than the processor." if speedup else "."
    return {"configured": True, "where": "graphics card", "detail": detail}
