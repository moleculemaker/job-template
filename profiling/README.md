# GPU profiling

Every tool's compute phase ends with a resource profile: how long a job takes and how
much GPU memory it needs, as a function of input size. The profile sets the input
schema's `maxItems`, the job deadline, and the capacity note (`CAPACITY_NOTE.md`).

mmli2 has one GPU, a 32 GB Tesla V100 on `mmli2-worker-gpu-01`, shared by every GPU tool
on staging and prod. Profile only while it is idle.

## Run it

Set up `inputs.py` first: `make_job(size)` must build a realistic input of `size` items
for your tool (for EZKinetics, enzyme-substrate pairs). It may go past the schema's
current `maxItems`; that is how you find the limit.

```sh
cd profiling
python3 gpu_profile.py check        # is anything using the GPU? exits 1 if so
python3 gpu_profile.py render --image ghcr.io/<org>/<tool>-job:sha-<short> --tool <tool> --sizes 1,5,20,50,100
python3 gpu_profile.py run    --image ghcr.io/<org>/<tool>-job:sha-<short> --tool <tool> --sizes 1,5,20,50,100 --out out-<tool>
```

`render` prints the manifest without touching the cluster. `run` checks the GPU first and
refuses to start if anything blocks (below). Then it applies one ConfigMap and one Job in
`staging`, waits, reads the results from the pod log, deletes both, and writes
`out-<tool>/profile.json` and `profile.md`. You need kubectl access to mmli2 with
create/delete on Jobs and ConfigMaps in `staging`.

Use `--cpu`, `--memory` and `--shm` to match the tool's `kubernetes_jobs` values. If
the whole series won't finish in two hours, raise `--deadline`.

## What blocks a run

`check` blocks on two kinds of pod, running or pending:

- **Any pod that requests `nvidia.com/gpu`** (today: ml-simplefold, ezspec-unidock,
  ezspec-inference). A running one holds the GPU. A pending one is a user's job waiting
  for it, which a profiling run would delay.
- **Any Job pod under `runtimeClassName: nvidia` that doesn't request a GPU** (today:
  cleandb-mepesm, aceretro, novostoic-enzrank, oed-catpred, oed-dlkcat, oed-unikp,
  reactionminer; from mmli-backend's `chart/values.mmli2.*.yaml`). These
  see the whole card without asking the scheduler. Running one beside the profile would
  skew both its time and its memory.

Long-running pods under the nvidia runtime that aren't Jobs don't block: the gpu-operator
DaemonSets, and chemscraper-services, which holds about 2.8 GB of the card around the
clock. `check` lists them as residents. They are always there, so their memory is in the
baseline the measurement subtracts.

The check runs once, before launch. Your Job requests the GPU, so GPU-requesting jobs
submitted during the run wait behind it. A Job on the nvidia runtime without a GPU
request can still start alongside it. If the numbers look off, look at what ran during
the window.

## What is measured

The Job mirrors how the backend launches a GPU tool. It uses the same node selector,
toleration, runtime class, GPU request, root user, `/root/.cache` mount on the shared
volume and `/dev/shm`, and it runs the same `python -m wrapper.entrypoint` on a
`job.json`. Each size runs as a fresh process, as each real job is a fresh pod, so every
run pays model loading. Before the series, the smallest size runs once more without being
measured, so a cold weight cache doesn't skew the first point. That first warm-up run is
also when weights are downloaded into the tool's cache on the shared volume.

For each size, `profile.json` records:

| Field | Meaning |
|---|---|
| `wall_s` | Time for the whole tool process |
| `vram_baseline_mib`, `vram_peak_mib` | Card memory in use in the second before the run, and at its peak during it. Card-level (nvidia-smi every 200 ms), so the peak is a lower bound. Null if the image has no `nvidia-smi` or the tool doesn't use the GPU. |
| `peak_rss_mib` | The tool process's peak host memory, for the memory request |
| `exit_code`, `log_tail` | The tool's exit code, and its last output if it failed |

`profile.md` fits a straight line through the successful sizes:
`time = fixed + per_item × size`, and the same for memory over the baseline. The fixed
part is process start, model load and setup. The per-item part is what each extra input
costs. Check the R² value. If it is well below 1, the cost isn't linear in your chosen
size: plot it before deriving limits from it.

## From profile to limits

**`maxItems` in the input schema.** Take the smaller of:

- the time limit: `floor((T_max − fixed) / per_item)`, where `T_max` is the longest job
  you're willing to make a user wait for. That's a product decision; write it down.
- the memory limit: `floor((budget − fixed_vram) / per_item_vram)`. Here `budget` is
  32768 MiB, minus the baseline, minus the peaks of nvidia-runtime tools that may run
  beside yours (they don't queue), minus 10% headroom.

Never go past the largest size that succeeded in the profile. ml-simplefold shows why:
its UI accepts 1022 residues, a size that runs out of GPU memory even with the card to
itself (see the comments in mmli-backend's `app/cfg/config.yaml`).

**Job deadline.** For your tool, the right value is about `2 × time(maxItems)` plus the
image pull time, so that a hung job frees the GPU instead of holding it. **The backend
can't apply it yet:** it reads `activeDeadlineSeconds` only from
`kubernetes_jobs.defaults` (259200 s, 3 days; mmli-backend
`app/services/kubejob_service.py`, in `create_job`). Record the value in the capacity
note until the backend reads a per-tool setting.

## Tests

```sh
python3 -m unittest discover -s profiling
```

These cover the blocker rules, the fit, the report, the queue-wait formula and the
in-pod driver. The driver runs locally against the sample tool, with no GPU. CI runs them
too.
