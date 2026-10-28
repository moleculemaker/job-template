# Capacity note: <tool>

Copy this into the tool's repo as `CAPACITY_NOTE.md` and fill it in after profiling.
It records whether the shared GPU can absorb the new tool, and the limits derived from
its profile.

## Profile

- Image: `ghcr.io/<org>/<tool>-job:sha-<short>`, profiled on <date>
- Time: <fixed> s fixed + <per_item> s per item (R² <r2>)
- GPU memory over baseline: <fixed_vram> MiB fixed + <per_item_vram> MiB per item
- Largest size that succeeded: <n>
- Attach or link `profile.md`.

## Limits

| Setting | Value | Derived from |
|---|---|---|
| Input schema `maxItems` | | min(time limit, memory limit, largest successful size): see profiling/README.md |
| Longest job users should wait for (`T_max`) | | Product decision: who made it? |
| Job deadline | | ≈ 2 × time(maxItems). Not yet settable per tool in the backend. |
| CPU / memory requests | | `peak_rss_mib` with headroom |

## GPU queue wait

The GPU serves jobs that request it one at a time. List every tool that requests
`nvidia.com/gpu` (today ml-simplefold, ezspec-unidock and ezspec-inference), plus the new
one, in a JSON file. Tools on the nvidia runtime without a GPU request don't queue, so
leave them out. Their memory comes off the GPU memory budget instead (see "From profile
to limits" in profiling/README.md).

```json
[
  {"tool": "ml-simplefold",    "jobs_per_day": 0, "service_s": 0, "service_sd_s": 0},
  {"tool": "ezspec-unidock",   "jobs_per_day": 0, "service_s": 0, "service_sd_s": 0},
  {"tool": "ezspec-inference", "jobs_per_day": 0, "service_s": 0, "service_sd_s": 0},
  {"tool": "<tool>",           "jobs_per_day": 0, "service_s": 0, "service_sd_s": 0}
]
```

- `jobs_per_day`: for existing tools, prod's average over the last 90 days, from the
  backend's `GET /internal/reports/usage?from=…&to=…` (`by_tool[].jobs` ÷ days). For the
  new tool, an estimate. Say where it came from.
- `service_s` and `service_sd_s`: the mean and standard deviation of how long a job holds
  the GPU. A pod that requests the GPU holds it from the moment it's scheduled until it
  terminates: image pull (`imagePullPolicy: Always`), the init container's download, the
  tool, then post-job's upload. So measure the whole pod, not just the tool:
  - existing tools: from recent pods, the time from the `PodScheduled` condition to the
    last container's `finishedAt` (`kubectl get pod -o json`);
  - the new tool: its profiled `wall_s` at a typical size, plus that same overhead
    (scheduled-to-finished minus the `job` container's run time) taken from an existing
    GPU tool's pods.

  Leaving out the overhead understates utilization, and at 5× or 10× that can turn an
  unbounded queue into a finite-looking wait. The backend doesn't record start times yet,
  so `started_at` is empty.

Then run:

```sh
python3 profiling/gpu_profile.py capacity tools.json
```

It treats the GPU as one first-come-first-served queue (M/G/1) and prints utilization and
mean wait at 1×, 5× and 10× today's traffic:

| traffic | GPU utilization | mean wait for the GPU |
|---|---:|---:|
| 1x | | |
| 5x | | |
| 10x | | |

These are averages under steady random arrivals. Bursts, such as a class submitting
together, wait longer. Above about 70% utilization, waits grow quickly.

## Conclusion

<One paragraph: can the GPU take this tool at expected traffic? If not, what changes:
a lower maxItems, a faster model, a second GPU, or scheduling?>
