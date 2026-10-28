"""Runs inside the profiling pod: one fresh tool process per input size.

Each size runs `python -m wrapper.entrypoint` in a new process, as each backend job is a
new pod, so model loading is paid every time and shows up as the fixed cost. The first
size is run once more beforehand, unmeasured, so a cold weight cache doesn't inflate the
first point.

Prints one `PROFILE {json}` line per size; gpu_profile.py reads them from the pod log.
Standard library only, since it runs in the tool's image.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PROFILE_DIR = Path("/profile")
WORK_DIR = Path("/tmp/profile")
# The container's memory use as the OOM killer sees it: every process plus /dev/shm.
# cgroup v2 first, then v1.
CGROUP_MEMORY_FILES = ["/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"]


class VramSampler:
    """Samples card-level used memory (MiB) with nvidia-smi every 200 ms.

    Card-level, so it includes every other process on the GPU; gpu_profile.py subtracts the
    baseline taken just before the run. Sampling can miss a short spike, so treat the
    peak as a lower bound.
    """

    def __init__(self):
        self.samples = []
        self._proc = None
        self._thread = None

    def start(self):
        if shutil.which("nvidia-smi") is None:
            return self
        self._proc = subprocess.Popen(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits", "-lms", "200"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        return self

    def _read(self):
        for line in self._proc.stdout:
            try:
                self.samples.append((time.monotonic(), float(line.split(",")[0])))
            except ValueError:
                pass

    def stop(self):
        if self._proc:
            self._proc.terminate()
            self._proc.wait()
            self._thread.join(timeout=2)

    def between(self, start, end):
        return [mib for t, mib in self.samples if start <= t <= end]


class MemorySampler:
    """Samples the container's cgroup memory use (MiB) every `interval_s`.

    ru_maxrss is one process's peak, so it misses DataLoader workers running alongside
    the main process, and /dev/shm. The cgroup counts all of it, which is what a memory
    limit is checked against. It also counts the driver and page cache, so it errs high.
    """

    def __init__(self, interval_s=0.2):
        self.interval_s = interval_s
        self.samples = []
        self._path = next((p for p in CGROUP_MEMORY_FILES if os.path.exists(p)), None)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._path:
            self._thread = threading.Thread(target=self._read, daemon=True)
            self._thread.start()
        return self

    def _read(self):
        while not self._stop.is_set():
            try:
                with open(self._path) as f:
                    self.samples.append((time.monotonic(), int(f.read()) / 1024 / 1024))
            except (OSError, ValueError):
                pass
            self._stop.wait(self.interval_s)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def between(self, start, end):
        return [mib for t, mib in self.samples if start <= t <= end]


def run_one(size, sampler, memory=None):
    job_dir = WORK_DIR / str(size)
    shutil.rmtree(job_dir, ignore_errors=True)
    (job_dir / "in").mkdir(parents=True)
    (job_dir / "out").mkdir()
    shutil.copy(PROFILE_DIR / f"job-{size}.json", job_dir / "in" / "job.json")
    env = dict(os.environ, JOB_ID=f"profile-{size}",
               JOB_INPUT_DIR=str(job_dir / "in"), JOB_OUTPUT_DIR=str(job_dir / "out"))

    time.sleep(1)  # a baseline window with nothing of ours on the GPU
    started = time.monotonic()
    with open(job_dir / "log.txt", "w") as log:
        proc = subprocess.Popen([sys.executable, "-m", "wrapper.entrypoint"], env=env,
                                stdout=log, stderr=subprocess.STDOUT)
        # wait4 gives this child's own peak RSS; RUSAGE_CHILDREN would accumulate
        _, status, usage = os.wait4(proc.pid, 0)
    ended = time.monotonic()

    baseline = sampler.between(started - 1, started)
    during = sampler.between(started, ended)
    memory_during = memory.between(started, ended) if memory else []
    # Not os.waitstatus_to_exitcode: that needs Python 3.9, and tool images can be older
    exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status)
    record = {
        "size": size,
        "wall_s": round(ended - started, 3),
        "exit_code": exit_code,
        "peak_rss_mib": round(usage.ru_maxrss / 1024, 1),  # Linux reports KiB; one process only
        "mem_peak_mib": round(max(memory_during), 1) if memory_during else None,
        "vram_baseline_mib": min(baseline) if baseline else None,
        "vram_peak_mib": max(during) if during else None,
    }
    if exit_code != 0:
        record["log_tail"] = (job_dir / "log.txt").read_text()[-2000:]
    return record


def main():
    sizes = json.loads((PROFILE_DIR / "sizes.json").read_text())
    sampler = VramSampler().start()
    memory = MemorySampler().start()
    try:
        warmup = run_one(sizes[0], sampler, memory)
        print("PROFILE " + json.dumps(dict(warmup, warmup=True)), flush=True)
        for size in sizes:
            print("PROFILE " + json.dumps(run_one(size, sampler, memory)), flush=True)
    finally:
        sampler.stop()
        memory.stop()


if __name__ == "__main__":
    main()
