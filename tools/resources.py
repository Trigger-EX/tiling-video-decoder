"""Run background jobs (ffmpeg) without taking the machine away from the user.

A `Governor` looks at the whole system, not just our own processes: it samples overall CPU use and free
memory, and only lets another job start when the extra load still fits under a ceiling that leaves
`headroom` of the CPU idle and a memory reserve free. The first job always runs, so work always makes
progress. Jobs are started at low CPU (and disk) priority, so if something else needs the CPU suddenly
the desktop wins immediately, without waiting for the next scheduling decision.

Standard library only; uses `psutil` for sampling when it is installed. Sampling backends: psutil, Linux
(/proc), Windows (ctypes, untested), anything else falls back to the load average (CPU only).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time


class SystemSampler:
    """System-wide CPU busy fraction (since the previous call) and available memory."""

    def __init__(self):
        self.cores = os.cpu_count() or 1
        self._prev = None
        try:
            import psutil
            self._psutil = psutil
            psutil.cpu_percent(interval=None)
        except ImportError:
            self._psutil = None

    def cpu_busy(self):
        """0..1, or None if this platform can't tell."""
        if self._psutil:
            return self._psutil.cpu_percent(interval=None) / 100.0
        if sys.platform.startswith("linux"):
            with open("/proc/stat") as f:
                v = [int(x) for x in f.readline().split()[1:]]
            idle, total = v[3] + v[4], sum(v[:8])           # idle + iowait; user..steal
        elif os.name == "nt":
            idle, total = self._windows_times()
        else:
            try:
                return min(1.0, os.getloadavg()[0] / self.cores)   # slow-moving, but better than nothing
            except (AttributeError, OSError):
                return None
        prev, self._prev = self._prev, (idle, total)
        if prev is None or total == prev[1]:
            return None
        return max(0.0, min(1.0, 1.0 - (idle - prev[0]) / (total - prev[1])))

    def mem_available_mb(self):
        if self._psutil:
            return self._psutil.virtual_memory().available / 2**20
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / 1024
        if os.name == "nt":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tp", ctypes.c_ulonglong),
                            ("ap", ctypes.c_ulonglong), ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
            s = Status(); s.l = ctypes.sizeof(s)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            return s.ap / 2**20
        return None

    @staticmethod
    def _windows_times():
        import ctypes
        from ctypes import wintypes
        idle, kern, user = (wintypes.FILETIME() for _ in range(3))
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
        t = lambda f: (f.dwHighDateTime << 32) | f.dwLowDateTime  # noqa: E731
        return t(idle), t(kern) + t(user)                     # kernel time already includes idle


class Governor:
    def __init__(self, sampler=None, headroom=0.30, reserve_mem_mb=2048, max_jobs=None, job_cores=1.0,
                 job_mem_mb=400, cooldown=1.0, clock=time.monotonic):
        """
        headroom        fraction of total CPU to keep idle for everything else (0.30 = never plan above 70%)
        reserve_mem_mb  memory to keep available after a new job's estimated need
        job_cores       CPU cores one job is expected to use (the sampler corrects for wrong guesses)
        """
        if not 0 <= headroom < 1:
            raise ValueError("headroom must be in [0, 1)")
        self.s = sampler or SystemSampler()
        self.cores = self.s.cores
        self.headroom, self.reserve_mem_mb = headroom, reserve_mem_mb
        self.max_jobs = max_jobs or self.cores
        self.job_cores, self.job_mem_mb = job_cores, job_mem_mb
        self.cooldown, self.clock = cooldown, clock
        self.busy = None                  # smoothed system CPU use, 0..1
        self.mem_mb = None
        self._last_start = -1e9

    def sample(self):
        b = self.s.cpu_busy()
        if b is not None:
            self.busy = b if self.busy is None else 0.5 * self.busy + 0.5 * b
        self.mem_mb = self.s.mem_available_mb()

    def may_start(self, running):
        """(allowed, why not). Call sample() regularly (at least every 0.25 s) before this."""
        if running == 0:
            return True, ""              # always keep one job going so we finish eventually
        if running >= self.max_jobs:
            return False, "job limit"
        if self.clock() - self._last_start < self.cooldown:
            return False, "settling"     # the last job's load hasn't shown up in the samples yet
        if self.busy is None:            # can't measure: stay conservative
            return (running < max(1, self.cores // 2)), "no cpu sampling"
        if self.busy + self.job_cores / self.cores > 1.0 - self.headroom:
            return False, "cpu %.0f%% busy" % (100 * self.busy)
        if self.mem_mb is not None and self.mem_mb - self.job_mem_mb < self.reserve_mem_mb:
            return False, "memory %.1f GB free" % (self.mem_mb / 1024)
        return True, ""

    def started(self):
        self._last_start = self.clock()


def low_priority_command(cmd):
    """Prefix `cmd` so the disk is also deprioritised where `ionice` exists."""
    if sys.platform.startswith("linux") and shutil.which("ionice"):
        return ["ionice", "-c2", "-n7"] + cmd
    return cmd


def spawn_low_priority(cmd, nice=15):
    """Starts cmd at below-normal CPU priority; stderr goes to a temp file (never blocks the child)."""
    err = tempfile.TemporaryFile()
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x00004000          # BELOW_NORMAL_PRIORITY_CLASS
    else:
        kw["preexec_fn"] = lambda: os.nice(nice)
    p = subprocess.Popen(low_priority_command(cmd) if os.name != "nt" else cmd, stdout=subprocess.DEVNULL, stderr=err, **kw)
    p.err_file = err
    return p


def run_low_priority(cmd):
    """Runs one command to completion at low priority; returns (exit code, stderr text)."""
    p = spawn_low_priority(cmd)
    rc = p.wait()
    p.err_file.seek(0)
    err = p.err_file.read().decode(errors="replace")
    p.err_file.close()
    return rc, err


def run_jobs(jobs, governor, log=print, poll=0.25, spawn=spawn_low_priority):
    """Runs [(name, cmd)] under `governor`. Raises RuntimeError on the first failure (after stopping the rest)."""
    pending, running, done, started_at = list(jobs), [], 0, time.monotonic()
    total = len(jobs)

    def stop_all():
        for _, p in running:
            if p.poll() is None:
                p.terminate()
        for _, p in running:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    try:
        while pending or running:
            for item in list(running):
                name, p = item
                rc = p.poll()
                if rc is None:
                    continue
                running.remove(item)
                err = ""
                if rc != 0:
                    p.err_file.seek(0)
                    err = p.err_file.read().decode(errors="replace")[-1500:]
                p.err_file.close()
                if rc != 0:
                    raise RuntimeError("%s failed (exit %d):\n%s" % (name, rc, err))
                done += 1
                governor.sample()
                log("[%d/%d] %s done  | cpu %s, %s free, %d running, %ds elapsed" % (
                    done, total, name,
                    "%.0f%%" % (100 * governor.busy) if governor.busy is not None else "?",
                    "%.1f GB" % (governor.mem_mb / 1024) if governor.mem_mb is not None else "? GB",
                    len(running), time.monotonic() - started_at))
            governor.sample()
            while pending:
                ok, _ = governor.may_start(len(running))
                if not ok:
                    break
                name, cmd = pending.pop(0)
                running.append((name, spawn(cmd)))
                governor.started()
            time.sleep(poll)
    except BaseException:
        stop_all()
        for _, p in running:
            p.err_file.close()
        raise
