"""What can this computer actually do?

Deliberately narrow: how much memory, how many real cores, and is there a
graphics card with enough of its own memory to be worth trying.

The graphics card is *opportunistic*. Nothing here decides to use it -- this
module only reports what exists, and `engine.py` proves by measurement that the
card is actually faster before keeping it. A card that reports itself and then
turns out to be a software rasteriser, or a driver that loads and produces
nothing, must cost the user a few seconds at setup rather than an app that
appears to hang.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)

# Below this a small model still loads, but a run long enough to be worth doing
# takes so long that the cloud is the kinder recommendation.
MIN_USABLE_RAM_GB = 6
MIN_USABLE_CORES = 2

# Seconds for one *local* model call, measured against a 3B Q4 model on a
# ~2,600-character prompt. These numbers used to be roughly ten times too
# optimistic, and the estimate they fed ("about 5 minutes for a hundred
# companies", against a reality of several hours) was the single most
# trust-destroying sentence in the app.
SECONDS_PER_CALL_BY_CORES = ((16, 14), (8, 22), (4, 45), (0, 90))

# Local model calls per company across the whole pipeline: Sorter, Analyst and
# Connector each run once. The old estimate counted one.
LOCAL_CALLS_PER_COMPANY = 3

# Web searches per company across resolve, qualify, profile and people, and the
# throttle between them. Search is a floor no amount of CPU removes.
SEARCHES_PER_COMPANY = 7
SEARCH_SECONDS = 1.7

# A graphics card below this has too little memory to hold a useful slice of
# the model, and splitting across the bus is slower than staying on the CPU.
MIN_USEFUL_VRAM_GB = 3.5


@dataclass
class Hardware:
    os_name: str
    os_version: str
    arch: str
    cpu_cores: int
    ram_gb: float
    gpu_name: str
    # Defaulted, so existing callers and tests that construct this by hand keep
    # working.
    physical_cores: int = 0
    gpu_vram_gb: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def gpu_usable(self) -> bool:
        return self.gpu_vram_gb >= MIN_USEFUL_VRAM_GB

    def summary(self) -> str:
        parts = [f"{self.cpu_cores} CPU cores", f"{self.ram_gb:.0f} GB memory"]
        if self.gpu_name:
            if self.gpu_vram_gb:
                parts.append(f"{self.gpu_name} ({self.gpu_vram_gb:.0f} GB)")
            else:
                parts.append(self.gpu_name)
        return ", ".join(parts)


def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _total_ram_gb() -> float:
    """Total physical memory. psutil would be neater but is one more thing to
    ship in the installer, and every platform has a cheap built-in answer."""
    try:
        if os.name == "nt":
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MemoryStatusEx()
            stat.dwLength = ctypes.sizeof(MemoryStatusEx)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return stat.ullTotalPhys / (1024 ** 3)

        if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names:
            return (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / (1024 ** 3)
    except Exception:  # noqa: BLE001 - detection must never crash the app
        log.debug("RAM detection failed", exc_info=True)
    return 0.0


def _gpu_name() -> str:
    """Reported for the user's benefit only. It is never used for local AI."""
    if shutil.which("nvidia-smi"):
        out = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
        if out.strip():
            return out.strip().splitlines()[0].strip()

    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return f"Apple {platform.machine()}"

    if os.name == "nt":
        out = _run(["powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_VideoController | "
                    "Select-Object -First 1 -ExpandProperty Name)"])
        if out.strip():
            return out.strip().splitlines()[0].strip()
    return ""


def _physical_cores() -> int:
    """Real cores, not hyperthreads.

    llama.cpp is memory-bandwidth bound, so giving it a thread per *logical*
    processor is slower than a thread per physical core -- on an 8-core/16-thread
    machine the difference is around 20%. os.cpu_count() reports 16.
    """
    try:
        if os.name == "nt":
            out = _run(["powershell", "-NoProfile", "-Command",
                        "(Get-CimInstance Win32_Processor | "
                        "Measure-Object -Property NumberOfCores -Sum).Sum"])
            n = int(re.search(r"\d+", out).group()) if re.search(r"\d+", out) else 0
            if n:
                return n
        elif platform.system() == "Linux":
            with open("/proc/cpuinfo") as fh:
                text = fh.read()
            # Keyed on (socket, core), not core id alone. Core ids repeat per
            # socket, so a dual-socket 2x12 machine counted 12 instead of 24 --
            # halving the thread count handed to the model and inflating every
            # time estimate on exactly the machines that are fastest.
            pairs = set()
            physical = core = None
            for line in text.splitlines():
                if line.startswith("physical id"):
                    physical = line.split(":")[-1].strip()
                elif line.startswith("core id"):
                    core = line.split(":")[-1].strip()
                    pairs.add((physical, core))
            if pairs:
                return len(pairs)
        elif platform.system() == "Darwin":
            out = _run(["sysctl", "-n", "hw.physicalcpu"])
            if out.strip().isdigit():
                return int(out.strip())
    except Exception:  # noqa: BLE001
        log.debug("physical core detection failed", exc_info=True)
    # Halving the logical count is right on every hyperthreaded x86 machine and
    # merely conservative on the rest.
    logical = os.cpu_count() or 2
    return max(1, logical // 2) if logical > 2 else logical


def _gpu_vram_gb() -> float:
    """Dedicated video memory, in GB. Zero when there is none worth using.

    Win32_VideoController.AdapterRAM is a *uint32* and therefore saturates at
    4 GB -- an 8, 12 or 24 GB card all report exactly 4,294,967,295 bytes. The
    display-class registry key holds the real 64-bit figure, so that is read
    first and AdapterRAM is only a fallback for the small-card case where it
    happens to be accurate.
    """
    if shutil.which("nvidia-smi"):
        out = _run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
        digits = re.search(r"\d+", out or "")
        if digits:
            return round(int(digits.group()) / 1024, 1)

    if os.name == "nt":
        out = _run(["powershell", "-NoProfile", "-Command",
                    "$k='HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\"
                    "{4d36e968-e325-11ce-bfc1-08002be10318}';"
                    "Get-ChildItem $k -ErrorAction SilentlyContinue | "
                    "ForEach-Object { (Get-ItemProperty $_.PSPath -Name "
                    "'HardwareInformation.qwMemorySize' -ErrorAction "
                    "SilentlyContinue).'HardwareInformation.qwMemorySize' } | "
                    "Sort-Object -Descending | Select-Object -First 1"], timeout=20)
        digits = re.search(r"\d+", out or "")
        if digits:
            gb = int(digits.group()) / (1024 ** 3)
            if gb >= 0.5:
                return round(gb, 1)

        out = _run(["powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_VideoController | "
                    "Sort-Object AdapterRAM -Descending | "
                    "Select-Object -First 1 -ExpandProperty AdapterRAM)"])
        digits = re.search(r"\d+", out or "")
        if digits:
            raw = int(digits.group())
            # The saturated value tells us "4 GB or more" and nothing else.
            # Treating it as exactly 4 GB is the safe reading.
            return round(min(raw, 4 * 1024 ** 3) / (1024 ** 3), 1)

    if platform.system() == "Darwin" and platform.machine() == "arm64":
        # Unified memory: the GPU can address most of system RAM.
        return round(_total_ram_gb() * 0.6, 1)
    return 0.0


def detect() -> Hardware:
    """Inspect this machine. Never raises."""
    return Hardware(
        os_name=platform.system() or "unknown",
        os_version=platform.release() or "",
        arch=platform.machine() or "",
        cpu_cores=os.cpu_count() or 1,
        ram_gb=round(_total_ram_gb(), 1),
        gpu_name=_gpu_name(),
        physical_cores=_physical_cores(),
        gpu_vram_gb=_gpu_vram_gb(),
    )


def seconds_per_call(cores: int) -> int:
    for threshold, seconds in SECONDS_PER_CALL_BY_CORES:
        if cores >= threshold:
            return seconds
    return SECONDS_PER_CALL_BY_CORES[-1][1]


def estimate_minutes(companies: int, hw: "Hardware | None" = None,
                     parallel: int = 3, speedup: float = 1.0,
                     cloud_share: float = 0.0) -> int:
    """Roughly how long a run of this size takes, end to end.

    Both halves are counted, because both are real and the search half is a
    floor that no amount of CPU removes:

      * local model time -- three calls per company, divided by however many the
        engine runs at once
      * web search time  -- about seven searches per company, spaced by a
        deliberate delay so the free endpoint does not block us

    The old estimate counted one model call per company, assumed a prefill speed
    no CPU achieves, and ignored search entirely. It told a sixteen-core user a
    hundred companies would take five minutes. The truth was several hours, and
    that gap did more damage to trust than any crash.
    """
    hw = hw or detect()
    cores = hw.physical_cores or hw.cpu_cores
    per_call = seconds_per_call(cores) / max(speedup, 0.1)

    local_calls = companies * LOCAL_CALLS_PER_COMPANY * (1.0 - min(cloud_share, 1.0))
    local_seconds = local_calls * per_call / max(parallel, 1)
    search_seconds = companies * SEARCHES_PER_COMPANY * SEARCH_SECONDS

    return max(1, round((local_seconds + search_seconds) / 60))


def recommend(hw: Hardware | None = None) -> dict:
    """Can this PC run local AI usefully, and with which model?"""
    from prospector.engine import choose_model

    hw = hw or detect()
    cores = hw.physical_cores or hw.cpu_cores
    model = choose_model(hw.ram_gb, hw.cpu_cores)
    # Judged on real cores, not hyperthreads -- two threads on one core is one
    # core's worth of throughput, and promising otherwise sets up a slow run.
    usable = hw.ram_gb >= MIN_USABLE_RAM_GB and cores >= MIN_USABLE_CORES

    if not usable:
        return {
            "can_run_local": False, "model": "", "label": "", "size_gb": 0,
            "quality": "", "seconds_per_call": 0, "minutes_per_hundred": 0,
            "gpu_usable": False,
            "reason": f"This PC has {hw.ram_gb:.0f} GB of memory and "
                      f"{cores} core{'' if cores == 1 else 's'}, which "
                      f"is not enough to run AI here usefully. Prospector will use "
                      f"the cloud instead.",
        }

    per_call = seconds_per_call(cores)
    minutes = estimate_minutes(100, hw)

    reason = (f"{cores} processor core{'' if cores == 1 else 's'} and "
              f"{hw.ram_gb:.0f} GB of memory can run the {model['label'].lower()} "
              f"here, free. A hundred companies takes roughly "
              f"{minutes} minutes.")

    if hw.gpu_usable:
        faster = estimate_minutes(100, hw, speedup=3.0)
        reason += (f" Your graphics card has {hw.gpu_vram_gb:.0f} GB of its own "
                   f"memory, so Prospector will try to use it - if it turns out "
                   f"faster, the same hundred companies take about {faster} "
                   f"minutes instead. It is tested during setup, and quietly "
                   f"ignored if it does not help.")
    elif hw.gpu_name:
        reason += (" Your graphics card has too little memory of its own to "
                   "help here, so the processor does the work instead.")

    reason += (" Adding a cloud key splits the work and cuts this "
               "substantially, because the slowest part moves off this PC.")

    return {"can_run_local": True, "model": model["id"], "label": model["label"],
            "size_gb": model["size_gb"], "quality": model["quality"],
            "seconds_per_call": per_call, "minutes_per_hundred": minutes,
            "gpu_usable": hw.gpu_usable, "reason": reason}


def report() -> dict:
    """Hardware plus recommendation, for the app's engine screen."""
    hw = detect()
    return {"hardware": hw.as_dict(), "summary": hw.summary(),
            "recommendation": recommend(hw)}
