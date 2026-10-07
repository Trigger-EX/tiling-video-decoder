import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import resources  # noqa: E402


class FakeSampler:
    def __init__(self, cores=8, busy=0.1, mem=16000):
        self.cores, self.busy, self.mem = cores, busy, mem

    def cpu_busy(self): return self.busy
    def mem_available_mb(self): return self.mem


def gov(**kw):
    s = FakeSampler(**{k: kw.pop(k) for k in ("cores", "busy", "mem") if k in kw})
    t = [0.0]
    g = resources.Governor(sampler=s, clock=lambda: t[0], **kw)
    g.t, g.fake = t, s
    g.sample()
    return g


class GovernorTest(unittest.TestCase):
    def test_first_job_always_starts_even_when_the_machine_is_busy(self):
        g = gov(busy=0.99, mem=100)
        self.assertTrue(g.may_start(0)[0])

    def test_fills_cores_up_to_the_headroom_ceiling_then_stops(self):
        g = gov(cores=8, busy=0.0, headroom=0.25, job_cores=1)        # ceiling 75% = 6 of 8 cores
        running = 0
        while True:
            g.t[0] += 2
            g.fake.busy = running / 8                                  # system load = our jobs
            g.sample(); g.sample(); g.sample()
            ok, _ = g.may_start(running)
            if not ok:
                break
            g.started(); running += 1
        self.assertEqual(running, 6)

    def test_other_programs_load_reduces_what_we_run(self):
        g = gov(cores=8, busy=0.6, headroom=0.25, job_cores=1)         # user is already at 60%
        g.sample(); g.sample(); g.sample()
        self.assertTrue(g.may_start(1)[0])                              # 60% + 12.5% <= 75%
        g.started(); g.t[0] += 2
        g.fake.busy = 0.73; [g.sample() for _ in range(5)]
        ok, why = g.may_start(2)
        self.assertFalse(ok); self.assertIn("cpu", why)

    def test_memory_reserve_blocks_new_jobs(self):
        g = gov(mem=2300, reserve_mem_mb=2048, job_mem_mb=400, busy=0.0)
        ok, why = g.may_start(1)
        self.assertFalse(ok); self.assertIn("memory", why)
        g.fake.mem = 4000; g.sample()
        self.assertTrue(g.may_start(1)[0])

    def test_waits_after_a_start_so_load_can_show_up(self):
        g = gov(busy=0.0)
        g.started()
        self.assertEqual(g.may_start(1), (False, "settling"))
        g.t[0] += 1.5
        self.assertTrue(g.may_start(1)[0])

    def test_max_jobs_and_unknown_cpu(self):
        self.assertEqual(gov(busy=0.0, max_jobs=2).may_start(2), (False, "job limit"))
        g = gov(cores=8, busy=None)
        g.busy = None
        self.assertTrue(g.may_start(3)[0]); self.assertFalse(g.may_start(4)[0])


class RunJobsTest(unittest.TestCase):
    def test_runs_everything_and_reports_failure(self):
        g = resources.Governor(sampler=FakeSampler(busy=0.0), cooldown=0)
        ok = [("a", [sys.executable, "-c", "pass"]), ("b", [sys.executable, "-c", "pass"])]
        resources.run_jobs(ok, g, log=lambda *_: None, poll=0.01)
        bad = [("boom", [sys.executable, "-c", "import sys; sys.stderr.write('nope'); sys.exit(3)"])]
        with self.assertRaisesRegex(RuntimeError, "(?s)boom failed.*nope"):
            resources.run_jobs(bad, g, log=lambda *_: None, poll=0.01)

    @unittest.skipIf(os.name == "nt", "nice values are POSIX")
    def test_jobs_run_at_low_priority(self):
        p = resources.spawn_low_priority([sys.executable, "-c", "import os; print(os.nice(0))"])
        p.wait(); p.err_file.close()
        # nice(0) returns the current niceness; read it from /proc instead of stdout (stdout is discarded)
        q = resources.spawn_low_priority([sys.executable, "-c", "import os,sys; sys.exit(os.nice(0))"])
        self.assertEqual(q.wait(), 15)


if __name__ == "__main__":
    unittest.main()
