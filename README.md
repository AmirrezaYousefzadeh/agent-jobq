# agent-jobq: a memory-aware job queue for a machine shared by people and AI agents

> **Agents submitting jobs: read [AGENTS.md](AGENTS.md).**

One workstation, several people, and several AI coding agents, often one per project, each starting its own builds,
simulations and training runs. None of them knows what the others are running. The usual result is that two
memory-hungry jobs start at the same time, the kernel OOM killer picks a victim (often someone else's process), and
hours of work are lost.

`jobq` is a single Python file plus a systemd unit. Every agent submits heavy work with `jobq add`, and one daemon
decides when each job may start, watches memory while it runs, and pauses jobs before the machine runs out of RAM.
There is no server, database or scheduler to set up: the queue is a directory of JSON files that every agent can read.

## What it does

- **Admission by measurement, not by declaration.** Each job declares threads, peak RAM and optional GPU memory.
  Agents can't predict either well and usually over-declare, so a running job reserves its *real* RAM use plus half
  of the rest of its declaration (a new job needs half of its declaration plus a 3 GB margin free), and reserves its
  declared threads only for its first 5 minutes, afterwards the cores it actually uses. Under CPU contention each
  job's cgroup `cpu.weight` is its declared threads, so declarations act as CPU priority rather than as a gate.
- **Memory guard instead of the OOM killer.** Every 5 s: if free RAM drops below 5 GB, a CPU job is **frozen**
  (cgroup v2 freezer) and its memory pushed to swap (`memory.reclaim`): the fastest grower if it holds more than
  20 GB, else a job more than 20 GB over its declaration, else the newest job. The frozen job then heads the waiting list with the size it has shown (RAM + swap,
  at least its declaration): it resumes when that fits and leaves free RAM above the freeze line, losing no work, and keeps that size reserved afterwards so it isn't frozen again by
  the next job. HIGH-priority jobs are frozen last; GPU jobs never.
- **Swap only for the queue (optional).** With `--exclusive-swap`, logins and system services get no swap, while queue
  jobs may page to swap, e.g. a job needing more RAM than the per-job cap (50 GB by default) keeps running slowly
  instead of dying.
- **Invisible to interactive users.** Jobs run at `nice 19`, idle I/O priority, in a slice with CPU weight 1. On
  cgroup v2, `nice` only ranks processes within one cgroup, so the slice weight is what keeps the queue below
  everyone else.
- **Fair ordering with time estimates.** FIFO with backfill (small jobs start while a big one waits). A job that has
  waited 30 min gets a reserved start: when enough running jobs are expected to end, from their `--time` estimates.
  Later jobs may start ahead of it only if they end before that time or leave it enough RAM (EASY backfill), so
  big jobs don't starve and short jobs don't sit behind a multi-day run. `--high` jumps the queue.
- **Watchdog.** Kills processes a finished job left behind, kills jobs that hold RAM but have used no CPU for an hour
  (configurable per job), and reports idle processes outside the queue that hold more than 2 GB.
- **Live view.** The daemon rewrites `QUEUE.md` every 30 s: a whole-machine usage row, running jobs (CPU/GPU, declared
  vs real RAM, swap), the waiting list in start order with the reason each job waits, recent results, and a
  per-project "declared vs peak RAM" table that shows which agents over- or under-declare.
- **Agent-friendly CLI.** `jobq status --json`, and `jobq wait NAME` blocks until a job ends and returns its
  exit status (run it in the background and the agent is notified when the job finishes).
- **Survives restarts.** Jobs keep running when the daemon restarts or is upgraded (`touch RELOAD`); the new daemon
  adopts them, and each job's exit code is captured in a file.

## Requirements

Linux with systemd and cgroup v2 (any recent distribution), Python 3.9+, root access once for the install, and a swap
file or partition if you want swap overflow and freezing (on an SSD: freezing writes the job's memory to swap).
`nvidia-smi` is optional (GPU jobs).

## Install

```bash
git clone https://github.com/AmirrezaYousefzadeh/agent-jobq.git /srv/jobq      # any directory; state is stored next to the code
sudo bash /srv/jobq/systemd/install.sh [--exclusive-swap] [--queue-ram-gb N]
jobq status
```

`install.sh` creates `jobq.slice` (CPU/IO weight 1, all queue jobs together limited to total RAM − 5 GB unless
`--queue-ram-gb` is given) and `jobq.service` (runs as the user who called `sudo`, with a delegated cgroup), links
`/usr/local/bin/jobq`, and starts the daemon at boot. Jobs run as that user. `--exclusive-swap` sets
`MemorySwapMax=0` on `system.slice` and `user.slice`.

Project tags: a job's project is the first folder of its working directory below `$JOBQ_PROJECTS_ROOT` (default: the
folder that contains the queue directory). With `/work/.jobq` and projects in `/work/<project>`, a job submitted from
`/work/paperA/sim` is tagged `paperA`.

## Use

```bash
jobq add --name train_s1 --threads 4 --mem 12 --time 3h --gpu 3000 -- python train.py --seed 1
jobq add --name route --threads 8 --mem 30 --time 1d -- 'cd flow && make route > route.log'
jobq status            # or: jobq status --json, or open QUEUE.md
jobq wait train_s1     # exit 0 if the job succeeded
jobq log train_s1
jobq cancel route
```

The job gets the submitting shell's environment (minus variables that look like secrets), its working directory, and
`OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS` and `JOBQ_THREADS` set to `--threads`.
Full option list: `jobq add -h` and [AGENTS.md](AGENTS.md).

## Hooking up your agents

Put a short rule where every agent reads it, e.g. `~/.claude/CLAUDE.md` for Claude Code, or `AGENTS.md` in the
projects' root:

```markdown
# Shared machine: job queue
This PC is shared by several people and agents. Any heavy job (longer than a few minutes, more than ~2 GB RAM,
more than 2 threads, or the GPU) must be submitted with `jobq add` and checked with `jobq status`, never started
directly. Read /srv/jobq/AGENTS.md before your first submission.
```

## Tuning

Constants at the top of `jobq.py`:

| Constant | Default | Meaning |
|---|---|---|
| `BIG_GB` | 50 | RAM cap of one job; beyond it the job pages to swap |
| `THREAD_BUDGET` | 1.5 × usable CPUs | cores reserved by running jobs (usable = the daemon's CPU affinity) |
| `RAMP_MIN` | 5 | minutes a job reserves its declared threads after a start or resume, before its measured use counts |
| `MARGIN_GB` | 3 | RAM always left free |
| `OVERBOOK` | 0.5 | share of a job's unused declaration that is reserved |
| `FREEZE_BELOW_GB` | 5 | free RAM below which the memory guard freezes a job |
| `STARVE_MIN` | 30 | minutes before a waiting job gets a reserved start |
| `GROWER_GB` | 20 | frozen first: a fast grower holding more than this, else a job this far over its declaration |
| `STALL_RAM_GB` | 1 | a job is only killed as stalled if it holds this much RAM |

`touch <queue dir>/RELOAD` loads new code without stopping jobs. `touch <queue dir>/PAUSE` stops new starts.

## Limitations

- One machine, one queue owner: jobs run as the user who installed the service, not as the submitting user.
- Declarations are trusted for threads and GPU memory; only RAM is measured and policed.
- A frozen job keeps its GPU memory, open files and network connections; jobs that talk to license servers or
  remote peers may time out while frozen.
- Without a delegated cgroup (e.g. `jobq daemon` run by hand) there are no per-job cgroups: no freezing, no swap
  overflow, and RAM is limited with `ulimit -v` instead.

## License

MIT. See [LICENSE](LICENSE).

Written by Amirreza Yousefzadeh (University of Twente) with Claude Code, to keep several research-project agents from
fighting over one workstation.
