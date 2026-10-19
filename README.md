# job-template

Template for the containerized compute step of an AlphaSynthesis / AlphaSynBio app.
Each tool gets its own `<tool>-job` repo created from this one. The container wraps the
method author's code and is run asynchronously by
[mmli-backend](https://github.com/moleculemaker/mmli-backend) as a Kubernetes Job.

The included sample tool (protein length and molecular weight) is a complete, runnable
example of the contract.

## The contract

Through the backend's generic job path
([mmli-backend#144](https://github.com/moleculemaker/mmli-backend/issues/144)), the
image is launched with no command, so its `ENTRYPOINT` runs, with these environment
variables:

| Variable | Meaning |
|---|---|
| `JOB_INPUT_DIR` | Contains `job.json` (the job input, already validated against the tool's input schema) and any files uploaded with the job |
| `JOB_OUTPUT_DIR` | Everything written here is uploaded and kept as the job's output |
| `JOB_ID`, `JOB_TYPE` | Identify the job; rarely needed |

The container must:

1. Read `job.json` from `JOB_INPUT_DIR`.
2. Write `results.json` to `JOB_OUTPUT_DIR`: a frontend-ready object matching the tool's
   output schema. Large data (structures, tables, images) goes in separate files in the
   same directory, each listed in `results.json` under `artifacts`
   (`name`, `media_type`, optional `description`); the backend serves them by name.
3. Exit non-zero on failure. A non-zero exit marks the job as failed.

`wrapper/entrypoint.py` implements this and should not need to change. Tool-specific
code goes in `wrapper/tool.py`, whose `run(job, input_dir, output_dir)` returns the
`results.json` object.

The two schemas in `schemas/` are copies of the tool's schemas in mmli-backend
(`app/schemas/<tool>.input.json` and `<tool>.output.json`), which are the source of
truth. Keep them identical.

## Creating a new tool repo

1. Create `<tool>-job` from this template in the app family's org (`moleculemaker` for
   AlphaSynthesis, `iBioFoundry` for AlphaSynBio). Public unless the check below says
   otherwise.
2. Complete [LICENSED_CONTENT.md](LICENSED_CONTENT.md).
3. Bring in the method author's code at a pinned ref (see the `Dockerfile` comment) and
   pin its dependencies in `requirements.txt`, or switch the base image (e.g. to a CUDA
   image) as the method requires.
4. Write the input and output schemas, then replace `wrapper/tool.py`.
5. Replace `examples/job.json` with a small input that runs in seconds, and
   `examples/expected/` with its known-good output. Delete `examples/expected/` if the
   output is not deterministic; the smoke test then checks only the schemas.
6. Run the smoke test locally, then push. CI runs it on every PR.

## Smoke test

```sh
docker build -t my-tool:test .
pip install jsonschema
scripts/smoke-test.sh my-tool:test
```

It runs the image on `examples/job.json` with the same environment variables the backend
sets, then checks that the example input satisfies the input schema, `results.json`
satisfies the output schema, every listed artifact exists, and the output matches
`examples/expected/` if present.

GPU tools need a CPU code path (even a slow one) for the smoke test to run in CI. If the
method cannot run without a GPU, say so in the repo's README and rely on the GPU
profiling run on the cluster instead.

## Images and releases

`.github/workflows/build.yml` builds and smoke-tests every PR. Pushes to `main` publish
`ghcr.io/<org>/<tool>-job:sha-<short>`; tags `vX.Y.Z` also publish `:X.Y.Z`. There is no
`:latest`. The backend's Helm values pin an exact tag; bump it on staging first, then on
prod.

## Things that will bite you

- **Don't use `/code` as the working directory.** The backend mounts its own config files
  under `/code/app/cfg` in the job container.
- **Don't bake model weights into the image.** Large images exceed the CI runner's disk
  and slow every pull on the cluster. Download weights at runtime into a cache directory
  backed by the shared volume (configured per tool in the backend's values, as
  SimpleFold does), so they are fetched once.
- **Write `results.json` last.** The entrypoint does this for you; if you bypass it,
  make sure a partial `results.json` can never be left behind after a failure.
