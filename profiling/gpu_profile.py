#!/usr/bin/env python3
"""Profile a <tool>-job image on the mmli2 GPU node, and project GPU queue wait.

  gpu_profile.py check
      List what would block a run. Exits 1 if anything does.
  gpu_profile.py run --image IMAGE --tool TOOL --sizes 1,5,20,50,100 [--out DIR]
      Refuse to start if `check` finds a blocker, otherwise run every size in one Job
      in the `staging` namespace, then write DIR/profile.json and DIR/profile.md. Keeps
      checking while it runs, and marks the profile contended if a GPU job starts.
  gpu_profile.py render ...same options as run...
      Print the manifest `run` would apply, without touching the cluster.
  gpu_profile.py capacity TOOLS.json
      Mean GPU queue wait at 1x, 5x and 10x traffic (see CAPACITY_NOTE.md).

Needs kubectl pointed at mmli2, with permission to create and delete Jobs and
ConfigMaps in `staging`. Standard library only.
"""
import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from string import Template

HERE = Path(__file__).resolve().parent
NAMESPACE = "staging"
GPU_NODE_LABEL = "nvidia.com/gpu.present=true"
GPU_RESOURCE = "nvidia.com/gpu"
MULTIPLIERS = (1, 5, 10)
# A ConfigMap holds at most 1 MiB; leave room for the driver and metadata.
CONFIGMAP_MAX_BYTES = 1_000_000
# How long to wait for the log stream to end after the Job has.
LOG_DRAIN_TIMEOUT_S = 120


# --- What blocks a profiling run ------------------------------------------------------

def _requests_gpu(pod):
    spec = pod["spec"]
    for container in spec.get("containers", []) + spec.get("initContainers", []):
        resources = container.get("resources") or {}
        if GPU_RESOURCE in (resources.get("limits") or {}) or GPU_RESOURCE in (resources.get("requests") or {}):
            return True
    return False


def _owned_by_job(pod):
    return any(ref.get("kind") == "Job" for ref in pod["metadata"].get("ownerReferences", []))


def gpu_blockers(pods):
    """Pods that would compete with a profiling run, as "namespace/name: reason".

    Two kinds, because GPU tools here use the GPU in two ways:
      - pods that request nvidia.com/gpu (ml-simplefold, ezspec-*): there is one GPU, so
        a running one holds it and a pending one is a user's job waiting for it;
      - Job pods under runtimeClassName nvidia with no request (cleandb-mepesm and six
        other tools in values.mmli2.prod.yaml): they see the whole card without asking
        for it, so they share it with the run and skew its time and memory.
    Long-running pods under the nvidia runtime that aren't Jobs (gpu-operator DaemonSets,
    chemscraper-services) don't block. They are always there, so their memory is part of
    the baseline the driver subtracts. resident_gpu_pods() lists them. Their use isn't
    constant, though: see "Limits of the measurements" in README.md.
    """
    blockers = []
    for pod in pods:
        if pod["status"].get("phase") not in ("Pending", "Running"):
            continue
        name = f'{pod["metadata"]["namespace"]}/{pod["metadata"]["name"]}'
        if _requests_gpu(pod):
            state = "holds the GPU" if pod["status"]["phase"] == "Running" else "is waiting for the GPU"
            blockers.append(f"{name}: {state}")
        elif pod["spec"].get("runtimeClassName") == "nvidia" and _owned_by_job(pod):
            blockers.append(f"{name}: a Job using the GPU without requesting it")
    return blockers


def resident_gpu_pods(pods, gpu_nodes):
    """Running non-Job pods on a GPU node under the nvidia runtime: part of the baseline."""
    return [
        f'{pod["metadata"]["namespace"]}/{pod["metadata"]["name"]}'
        for pod in pods
        if pod["status"].get("phase") == "Running"
        and pod["spec"].get("nodeName") in gpu_nodes
        and pod["spec"].get("runtimeClassName") == "nvidia"
        and not _requests_gpu(pod)
        and not _owned_by_job(pod)
    ]


def _kubectl_json(*args):
    out = subprocess.run(["kubectl", *args, "-o", "json"], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def _other_pods(name):
    """Every pod except this run's own, which requests the GPU too. Used only while the
    run is going. The Job controller labels its pods job-name=<name>, so another
    profiling run still counts as contention."""
    return [p for p in _kubectl_json("get", "pods", "--all-namespaces")["items"]
            if (p["metadata"].get("labels") or {}).get("job-name") != name]


def check():
    pods = _kubectl_json("get", "pods", "--all-namespaces")["items"]
    gpu_nodes = {n["metadata"]["name"] for n in _kubectl_json("get", "nodes", "-l", GPU_NODE_LABEL)["items"]}
    blockers = gpu_blockers(pods)
    for name in resident_gpu_pods(pods, gpu_nodes):
        print(f"resident (in the baseline): {name}")
    for line in blockers:
        print(f"BLOCKER {line}")
    if not blockers:
        print(f"No GPU jobs running or waiting on {', '.join(sorted(gpu_nodes)) or 'any GPU node'}.")
    return blockers


# --- The run --------------------------------------------------------------------------

def _indent(text, spaces):
    return "\n".join(" " * spaces + line for line in text.splitlines())


def render_manifest(image, tool, sizes, name, make_job, cpu="4", memory="16Gi",
                    cpu_limit="16", memory_limit="32Gi", shm=None, deadline=7200):
    """The ConfigMap (driver, sizes, one job.json per size) and Job for one run.

    Defaults: requests like ml-simplefold's, limits like its 16 CPU / 32Gi, so a size that
    would run in prod isn't OOMKilled here. No /dev/shm volume unless `shm` is given,
    because the backend only adds one for tools whose values set shmSizeLimit.
    """
    files = {"driver.py": (HERE / "driver.py").read_text(), "sizes.json": json.dumps(sizes)}
    for size in sizes:
        files[f"job-{size}.json"] = json.dumps(make_job(size))
    total = sum(len(value.encode()) for value in files.values())
    if total > CONFIGMAP_MAX_BYTES:
        raise ValueError(f"The inputs come to {total} bytes, more than a ConfigMap holds (about 1 MiB). "
                         "Profile fewer or smaller sizes per run, and fit across the runs.")
    data = "\n".join(f"  {key}: |\n{_indent(value, 4)}" for key, value in files.items())
    shm_mount = shm_volume = ""
    if shm:
        shm_mount = "            - name: dshm\n              mountPath: /dev/shm\n"
        shm_volume = (f"        - name: dshm\n          emptyDir:\n            medium: Memory\n"
                      f"            sizeLimit: {shm}\n")
    return Template((HERE / "job.yaml").read_text()).substitute(
        name=name, namespace=NAMESPACE, configmap_data=data, image=image, tool=tool,
        cpu=cpu, memory=memory, cpu_limit=cpu_limit, memory_limit=memory_limit,
        shm_mount=shm_mount, shm_volume=shm_volume, deadline=deadline)


def parse_log(text):
    """The driver's PROFILE records, in order, from a pod log."""
    return [json.loads(line[len("PROFILE "):]) for line in text.splitlines() if line.startswith("PROFILE ")]


def _wait(name, poll_s=10):
    """Wait for the Job to end. Returns (succeeded, GPU jobs seen running alongside it).

    The guard in run() only covers the start. A runtime-class tool that starts mid-run
    shares the card and inflates later sizes, so keep looking and report what was seen.
    """
    contended = []
    while True:
        status = _kubectl_json("get", "job", name, "-n", NAMESPACE)["status"]
        if status.get("succeeded") or status.get("failed"):
            return bool(status.get("succeeded")), contended
        contended += [b for b in gpu_blockers(_other_pods(name)) if b not in contended]
        time.sleep(poll_s)


def run(args):
    if check():
        sys.exit("Not starting: GPU jobs are running or waiting (above). Try again when the GPU is idle.")
    from inputs import make_job
    sizes = sorted({int(s) for s in args.sizes.split(",")})
    name = f"gpu-profile-{args.tool}-{int(time.time())}"
    manifest = render_manifest(args.image, args.tool, sizes, name, make_job, args.cpu, args.memory,
                               args.cpu_limit, args.memory_limit, args.shm, args.deadline)
    # create, not apply: apply copies the whole object into an annotation capped at 256 KiB
    subprocess.run(["kubectl", "create", "-f", "-"], input=manifest, text=True, check=True)
    try:
        # Follow the log from the start. At the deadline the Job controller deletes the
        # pod, and its log with it, so reading it afterwards would lose every record.
        logs = subprocess.Popen(["kubectl", "logs", "-f", f"job/{name}", "-n", NAMESPACE,
                                 f"--pod-running-timeout={args.deadline}s"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ok, contended = _wait(name)
        log_error = None
        try:
            log, err = logs.communicate(timeout=LOG_DRAIN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            logs.kill()
            log, err = logs.communicate()
            log_error = f"The log stream was still open {LOG_DRAIN_TIMEOUT_S} s after the Job ended (timed out); kept what had arrived."
        if logs.returncode and not log_error:
            # A failed attach (RBAC, the pod never started) would otherwise look like a run with no records.
            log_error = f"kubectl logs exited {logs.returncode}: {err.strip()}"
    finally:
        subprocess.run(["kubectl", "delete", "job,configmap", name, "-n", NAMESPACE, "--wait=false"], check=False)
    records = parse_log(log)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "profile.json").write_text(json.dumps({"image": args.image, "tool": args.tool, "sizes": sizes,
                                                  "contended_by": contended, "log_error": log_error,
                                                  "records": records}, indent=2) + "\n")
    (out / "profile.md").write_text(report(records, args.image, sizes, contended, log_error))
    print((out / "profile.md").read_text())
    if not ok:
        sys.exit("The profiling Job failed; records up to the failure are above.")


# --- Analysis -------------------------------------------------------------------------

def fit(points):
    """Least-squares line through (size, value): (fixed, per_item, r_squared)."""
    n = len(points)
    if n < 2:
        raise ValueError("need at least two sizes to separate fixed from per-item cost")
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    if sxx == 0:
        raise ValueError("need at least two different sizes")
    per_item = sum((x - mean_x) * (y - mean_y) for x, y in points) / sxx
    fixed = mean_y - per_item * mean_x
    ss_tot = sum((y - mean_y) ** 2 for _, y in points)
    ss_res = sum((y - (fixed + per_item * x)) ** 2 for x, y in points)
    return fixed, per_item, (1 - ss_res / ss_tot) if ss_tot else 1.0


def report(records, image, sizes=None, contended=(), log_error=None):
    """Markdown summary of a run: a row per size and the fitted fixed/per-item costs."""
    measured = [r for r in records if not r.get("warmup")]
    lines = [f"# Profile of `{image}`", ""]
    if log_error:
        lines += [f"**Log incomplete:** {log_error}", ""]
    if contended:
        lines += ["**Contended:** these GPU jobs ran during the profile, so later sizes may be inflated. "
                  "Re-run when the GPU is idle.", ""] + [f"- {c}" for c in contended] + [""]
    lines += ["| size | wall (s) | VRAM peak (MiB) | VRAM over baseline (MiB) | memory peak, cgroup (MiB) "
              "| peak RSS, one process (MiB) | exit |",
              "|---:|---:|---:|---:|---:|---:|---:|"]
    for r in measured:
        over = (r["vram_peak_mib"] - r["vram_baseline_mib"]
                if r.get("vram_peak_mib") is not None and r.get("vram_baseline_mib") is not None else None)
        lines.append(f'| {r["size"]} | {r["wall_s"]} | {_fmt(r.get("vram_peak_mib"))} | {_fmt(over)} '
                     f'| {_fmt(r.get("mem_peak_mib"))} | {r["peak_rss_mib"]} | {r["exit_code"]} |')
    ok = [r for r in measured if r["exit_code"] == 0]
    lines.append("")
    unfinished = [s for s in (sizes or []) if s not in {r["size"] for r in measured}]
    if unfinished:
        lines.append(f"- **Did not finish: {', '.join(map(str, unfinished))}.** The Job ended first: "
                     "it hit its deadline, or the pod was OOMKilled or evicted.")
    if len({r["size"] for r in ok}) < 2:
        lines.append("Fewer than two sizes succeeded, so fixed and per-item costs can't be separated.")
        return "\n".join(lines) + "\n"
    fixed, per_item, r2 = fit([(r["size"], r["wall_s"]) for r in ok])
    lines.append(f"- **Time:** {fixed:.1f} s fixed (process start, model load, setup) "
                 f"+ {per_item:.3f} s per item (R² {r2:.3f})")
    vram = [(r["size"], r["vram_peak_mib"] - r["vram_baseline_mib"]) for r in ok
            if r.get("vram_peak_mib") is not None and r.get("vram_baseline_mib") is not None]
    if len({x for x, _ in vram}) >= 2:
        v_fixed, v_per_item, v_r2 = fit(vram)
        lines.append(f"- **VRAM over baseline:** {v_fixed:.0f} MiB fixed + {v_per_item:.1f} MiB per item "
                     f"(R² {v_r2:.3f}). Sampled every 200 ms, so a lower bound.")
    else:
        lines.append("- **VRAM:** not measured (no nvidia-smi in the container, or the tool doesn't use the GPU).")
    failed = [r for r in measured if r["exit_code"] != 0]
    if failed:
        lines.append(f"- **Failed sizes:** {', '.join(str(r['size']) for r in failed)}. See `log_tail` in profile.json.")
    return "\n".join(lines) + "\n"


def _fmt(value):
    return "n/a" if value is None else f"{value:.0f}"


def queue_wait(tools, multiplier):
    """Mean wait for the GPU when every tool's traffic is scaled by `multiplier`.

    One GPU serving jobs first come, first served, from all tools that request it:
    an M/G/1 queue, so the Pollaczek-Khinchine formula gives the mean wait
        W = sum(lambda_i * E[S_i^2]) / (2 * (1 - rho)),   rho = sum(lambda_i * E[S_i])
    with E[S^2] = mean^2 + sd^2. Returns (rho, wait_s); wait_s is None when rho >= 1,
    where the queue grows without bound.
    """
    per_s = [(t["jobs_per_day"] * multiplier / 86400, t["service_s"], t.get("service_sd_s", 0)) for t in tools]
    rho = sum(rate * mean for rate, mean, _ in per_s)
    if rho >= 1:
        return rho, None
    second_moment = sum(rate * (mean ** 2 + sd ** 2) for rate, mean, sd in per_s)
    return rho, second_moment / (2 * (1 - rho))


def capacity_table(tools, multipliers=MULTIPLIERS):
    lines = ["| traffic | GPU utilization | mean wait for the GPU |", "|---|---:|---:|"]
    for m in multipliers:
        rho, wait = queue_wait(tools, m)
        shown = "unbounded (over capacity)" if wait is None else _duration(wait)
        lines.append(f"| {m}x | {rho:.0%} | {shown} |")
    return "\n".join(lines) + "\n"


def _duration(seconds):
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


# --- CLI ------------------------------------------------------------------------------

def _run_args(sub):
    sub.add_argument("--image", required=True, help="e.g. ghcr.io/moleculemaker/<tool>-job:sha-abc1234")
    sub.add_argument("--tool", required=True, help="the tool's job type, e.g. ezkinetics")
    sub.add_argument("--sizes", required=True, help="comma-separated input sizes, e.g. 1,5,20,50,100")
    sub.add_argument("--cpu", default="4", help="CPU request")
    sub.add_argument("--memory", default="16Gi", help="memory request")
    sub.add_argument("--cpu-limit", default="16")
    sub.add_argument("--memory-limit", default="32Gi")
    sub.add_argument("--shm", default=None,
                     help="mount /dev/shm of this size, as the backend does for a tool whose values set shmSizeLimit")
    sub.add_argument("--deadline", type=int, default=7200, help="activeDeadlineSeconds for the whole run")
    sub.add_argument("--out", default="profile-out")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    _run_args(commands.add_parser("run"))
    _run_args(commands.add_parser("render"))
    commands.add_parser("capacity").add_argument("tools", help="JSON list, see CAPACITY_NOTE.md")
    args = parser.parse_args()

    if args.command == "check":
        sys.exit(1 if check() else 0)
    if args.command == "run":
        run(args)
    if args.command == "render":
        from inputs import make_job
        sizes = sorted({int(s) for s in args.sizes.split(",")})
        print(render_manifest(args.image, args.tool, sizes, f"gpu-profile-{args.tool}-render", make_job,
                              args.cpu, args.memory, args.cpu_limit, args.memory_limit, args.shm, args.deadline))
    if args.command == "capacity":
        print(capacity_table(json.loads(Path(args.tools).read_text())))


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
