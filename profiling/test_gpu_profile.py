"""Offline tests for the profiling kit: python -m unittest discover -s profiling"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import driver  # noqa: E402
import gpu_profile as gp  # noqa: E402
from inputs import make_job  # noqa: E402


def pod(name="p", phase="Running", gpu=False, init_gpu=False, runtime=None, owner="Job", node="gpu-01"):
    def container(with_gpu):
        return {"resources": {"limits": {"nvidia.com/gpu": "1"}} if with_gpu else {}}
    spec = {"containers": [container(gpu)], "initContainers": [container(init_gpu)], "nodeName": node}
    if runtime:
        spec["runtimeClassName"] = runtime
    return {
        "metadata": {"namespace": "ns", "name": name,
                     "ownerReferences": [{"kind": owner}] if owner else []},
        "spec": spec,
        "status": {"phase": phase},
    }


class TestGpuBlockers(unittest.TestCase):
    def test_a_running_pod_that_requests_the_gpu_blocks(self):
        self.assertEqual(gp.gpu_blockers([pod(gpu=True)]), ["ns/p: holds the GPU"])

    def test_a_pending_pod_waiting_for_the_gpu_blocks(self):
        """A user's job queued for the GPU: a profiling run would make it wait longer."""
        self.assertEqual(gp.gpu_blockers([pod(phase="Pending", gpu=True)]), ["ns/p: is waiting for the GPU"])

    def test_a_gpu_request_on_an_init_container_counts(self):
        """The backend runs the tool in the `job` init container."""
        self.assertEqual(len(gp.gpu_blockers([pod(init_gpu=True)])), 1)

    def test_a_job_on_the_nvidia_runtime_without_a_request_blocks(self):
        """cleandb-mepesm and others use the card without asking for it."""
        self.assertEqual(gp.gpu_blockers([pod(runtime="nvidia")]),
                         ["ns/p: a Job using the GPU without requesting it"])

    def test_resident_services_and_daemonsets_do_not_block(self):
        pods = [pod("chemscraper", runtime="nvidia", owner="ReplicaSet"),
                pod("dcgm", runtime="nvidia", owner="DaemonSet")]
        self.assertEqual(gp.gpu_blockers(pods), [])
        self.assertEqual(gp.resident_gpu_pods(pods, {"gpu-01"}), ["ns/chemscraper", "ns/dcgm"])

    def test_finished_pods_do_not_block(self):
        self.assertEqual(gp.gpu_blockers([pod(phase="Succeeded", gpu=True), pod(phase="Failed", runtime="nvidia")]), [])

    def test_ordinary_pods_do_not_block(self):
        self.assertEqual(gp.gpu_blockers([pod(), pod(owner="ReplicaSet")]), [])

    def test_residents_are_only_counted_on_a_gpu_node(self):
        self.assertEqual(gp.resident_gpu_pods([pod(runtime="nvidia", owner="ReplicaSet", node="cpu-01")], {"gpu-01"}), [])


class TestFit(unittest.TestCase):
    def test_recovers_fixed_and_per_item_cost(self):
        fixed, per_item, r2 = gp.fit([(1, 32.0), (10, 50.0), (100, 230.0)])
        self.assertAlmostEqual(fixed, 30.0)
        self.assertAlmostEqual(per_item, 2.0)
        self.assertAlmostEqual(r2, 1.0)

    def test_needs_two_different_sizes(self):
        with self.assertRaises(ValueError):
            gp.fit([(5, 1.0)])
        with self.assertRaises(ValueError):
            gp.fit([(5, 1.0), (5, 2.0)])


def record(size, wall, exit_code=0, vram=(1000, None), warmup=False):
    baseline, peak = vram
    r = {"size": size, "wall_s": wall, "exit_code": exit_code, "peak_rss_mib": 100.0,
         "vram_baseline_mib": baseline, "vram_peak_mib": peak}
    if warmup:
        r["warmup"] = True
    return r


class TestReport(unittest.TestCase):
    def test_fits_time_and_vram_and_leaves_out_the_warmup(self):
        records = [record(1, 999.0, warmup=True, vram=(1000, 9000)),
                   record(1, 32.0, vram=(1000, 5100)), record(10, 50.0, vram=(1000, 5550)),
                   record(100, 230.0, vram=(1000, 10050))]
        text = gp.report(records, "img")
        self.assertIn("30.0 s fixed", text)
        self.assertIn("2.000 s per item", text)
        self.assertIn("4050 MiB fixed + 50.0 MiB per item", text)
        self.assertNotIn("999", text)

    def test_says_when_vram_was_not_measured(self):
        text = gp.report([record(1, 1.0), record(5, 2.0)], "img")
        self.assertIn("VRAM:** not measured", text)
        self.assertIn("| 1 | 1.0 | n/a | n/a |", text)

    def test_failed_sizes_are_listed_and_left_out_of_the_fit(self):
        text = gp.report([record(1, 1.0), record(5, 2.0), record(500, 3.0, exit_code=1)], "img")
        self.assertIn("Failed sizes:** 500", text)
        self.assertIn("0.250 s per item", text)

    def test_one_size_cannot_be_fitted(self):
        self.assertIn("Fewer than two sizes", gp.report([record(1, 1.0)], "img"))


class TestRenderAndParse(unittest.TestCase):
    def test_manifest_carries_every_size_and_the_gpu_settings(self):
        text = gp.render_manifest("ghcr.io/o/t-job:sha-1", "t", [1, 5], "gpu-profile-t-1", make_job)
        for expected in ("image: ghcr.io/o/t-job:sha-1", "job-1.json: |", "job-5.json: |", "driver.py: |",
                         "runtimeClassName: nvidia", 'nvidia.com/gpu: "1"', "subPath: uws/jobs/t/.cache",
                         "namespace: staging"):
            self.assertIn(expected, text)
        self.assertNotIn("${", text)

    def test_parse_log_keeps_only_profile_records(self):
        log = 'tool chatter\nPROFILE {"size": 1}\nPROFILE {"size": 5}\n'
        self.assertEqual(gp.parse_log(log), [{"size": 1}, {"size": 5}])

    def test_sample_inputs_have_the_requested_size(self):
        self.assertEqual(len(make_job(7)["sequences"]), 7)


class TestQueueWait(unittest.TestCase):
    def test_matches_the_pollaczek_khinchine_formula(self):
        # 0.5 jobs/s of 1 s jobs, no variance: rho 0.5, W = 0.5 * 1 / (2 * 0.5) = 0.5 s
        rho, wait = gp.queue_wait([{"jobs_per_day": 43200, "service_s": 1}], 1)
        self.assertAlmostEqual(rho, 0.5)
        self.assertAlmostEqual(wait, 0.5)

    def test_variance_lengthens_the_wait(self):
        _, steady = gp.queue_wait([{"jobs_per_day": 43200, "service_s": 1}], 1)
        _, varied = gp.queue_wait([{"jobs_per_day": 43200, "service_s": 1, "service_sd_s": 1}], 1)
        self.assertAlmostEqual(varied, 2 * steady)

    def test_over_capacity_has_no_finite_wait(self):
        self.assertIsNone(gp.queue_wait([{"jobs_per_day": 43200, "service_s": 1}], 2)[1])

    def test_table_has_a_row_per_multiplier(self):
        table = gp.capacity_table([{"jobs_per_day": 10, "service_s": 1200}])
        self.assertIn("| 1x | 14% |", table)
        self.assertIn("| 10x |", table)


class TestDriver(unittest.TestCase):
    """Runs the in-pod driver on this machine against the sample tool (no GPU here)."""

    def test_records_every_size_after_an_unmeasured_warmup(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir, work_dir = Path(tmp, "profile"), Path(tmp, "work")
            profile_dir.mkdir()
            (profile_dir / "sizes.json").write_text("[1, 3]")
            for size in (1, 3):
                (profile_dir / f"job-{size}.json").write_text(json.dumps(make_job(size)))
            out = []
            with mock.patch.object(driver, "PROFILE_DIR", profile_dir), \
                    mock.patch.object(driver, "WORK_DIR", work_dir), \
                    mock.patch.object(driver.shutil, "which", lambda _: None), \
                    mock.patch.object(driver.time, "sleep", lambda _: None), \
                    mock.patch("builtins.print", lambda line, **kw: out.append(line)):
                cwd = os.getcwd()
                os.chdir(HERE.parent)  # where `python -m wrapper.entrypoint` resolves, as in the image
                try:
                    driver.main()
                finally:
                    os.chdir(cwd)
            records = gp.parse_log("\n".join(out))
            self.assertEqual([(r["size"], r.get("warmup", False)) for r in records],
                             [(1, True), (1, False), (3, False)])
            self.assertTrue(all(r["exit_code"] == 0 and r["vram_peak_mib"] is None for r in records))
            self.assertEqual(len(json.loads(Path(work_dir, "3", "out", "results.json").read_text())["rows"]), 3)

    def test_a_failing_size_records_its_exit_code_and_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp, "profile")
            profile_dir.mkdir()
            (profile_dir / "job-1.json").write_text('{"sequences": [{"id": "x", "sequence": "B"}]}')
            with mock.patch.object(driver, "PROFILE_DIR", profile_dir), \
                    mock.patch.object(driver, "WORK_DIR", Path(tmp, "work")), \
                    mock.patch.object(driver.time, "sleep", lambda _: None):
                cwd = os.getcwd()
                os.chdir(HERE.parent)
                try:
                    r = driver.run_one(1, driver.VramSampler())
                finally:
                    os.chdir(cwd)
            self.assertNotEqual(r["exit_code"], 0)
            self.assertIn("KeyError", r["log_tail"])


if __name__ == "__main__":
    unittest.main()
