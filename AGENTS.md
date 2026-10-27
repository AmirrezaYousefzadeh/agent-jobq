# jobq: guide for agents

This machine is shared by several people and several AI agents (typically one per project).
**Any job that runs longer than a few minutes, uses more than ~2 GB of RAM, more than 2 threads, or the GPU must go
through this queue.** Never start such a job directly in a shell or with `&`/`nohup`.

The command is `jobq`. Its queue directory (called `$JOBQ_DIR` below) is the folder of `readlink -f $(which jobq)`.
A daemon (systemd unit `jobq.service`) starts jobs when there
is room for them. You don't need to start or babysit it.

## Submit

```bash
jobq add --name <short_name> --threads <T> --mem <peak GB> --time <30m|6h|2d> [--gpu <MB>] [--high] -- <command ...>
```

- `--name`: letters, digits, `_ . + -`. Make it unique within your project (e.g. `train_shd_s1`).
- `--threads`: how many cores the job really uses. The job gets `OMP_NUM_THREADS`, `MKL_NUM_THREADS` and
  `OPENBLAS_NUM_THREADS` set to it, plus `JOBQ_THREADS`; pass that value to tools that take their own thread option
  (`NUM_CORES`, `-j`, `torch.set_num_threads`, ...).
- `--mem`: your best estimate of peak RAM in GB. Nobody can predict it exactly, so the queue protects itself:
  - A job starts when half its declaration + 3 GB fits in free RAM, where each running job reserves its real use
    plus half of the rest of its declaration.
  - Any job may use up to 50 GB of RAM (whatever it declared); beyond that it pages to swap.
  - If free RAM still drops below 5 GB, the memory guard steps in (below). Your job may be paused, never OOM-killed.
  For a GPU job, `--mem` is the host (CPU) RAM only; GPU memory goes in `--gpu`.
- `--time`: expected run time (`45m`, `6h`, `2d`; a plain number means hours). Optional, but always give it: it lets
  your job start earlier (see Waiting). Base it on a previous or smaller run (`done/*.json` has start and end) and
  round up. The job is never stopped for running longer.
- `--gpu`: GPU memory in MB. GPU jobs start at least 3 min apart and only if the GPU has that much free.
- Waiting: smaller jobs may start ahead of a bigger one when they fit (backfill). A HIGH job, or a job that has
  waited 30 min for RAM or threads, gets a **reserved start**: the time enough running jobs are expected to have
  ended (from their `--time`). Later jobs may then start ahead of it only if their `--time` ends before that
  reserved start, or if it would still fit with them running. A job without `--time` can't start this way.
  Running jobs without `--time` count as never ending.
- `--high`: high priority. It starts before every normal job, and normal jobs don't start while a high one waits.
  Use it only when the user asked for it.
- `--stall-min N` (default 60): the job is killed if it holds more than 1 GB of RAM and uses no CPU for N minutes.
  Use `0` for jobs that legitimately sleep while holding memory.
- `--cwd DIR` (default: current dir). The project tag is the first folder of the working directory below the
  projects root, normally the folder that contains `$JOBQ_DIR` (override with `--project`).
- Your current environment variables (PATH, conda/venv, nix, ...) are captured at submit time and used for the job,
  except names containing KEY/TOKEN/SECRET/PASSW/CREDENTIAL.
- Shell syntax works if you pass one string: `jobq add --name x --mem 4 -- 'cd sub && make -j4 > out.txt'`.

Jobs run at `nice 19`, idle I/O priority and the lowest CPU weight, so other users never notice them. On machines
installed with `--exclusive-swap`, only queue jobs may use swap.

## Check

```bash
jobq status            # daemon state, running / pending (with the reason it waits) / last 10 finished jobs
jobq status --json     # same, machine-readable (fields: running, pending[].waiting_for, done[].rc, idle_hogs)
jobq log <name>        # tail of the job's log (stdout+stderr), -n 200 for more
jobq wait <name>       # blocks until the job ends; exit 0 if rc 0, else 1. In Claude Code run it with
                       # run_in_background so you are notified when it finishes instead of polling.
jobq cancel <name>     # removes a pending job or kills a running one (whole process tree)
```

`<name>` matches across projects. Add `--project <folder>` if another project uses the same name.
A live overview is in `$JOBQ_DIR/QUEUE.md`. The daemon rewrites it every 30 s, so read it, don't edit it.
Finished jobs keep their record in `done/` (rc, start/end, peak RAM, peak swap, kill reason) and their log in `logs/`.

## Watchdog (automatic)

- Memory guard, every 5 s: if free RAM falls below 5 GB, a running CPU job is **frozen** (it stops where it is) and
  its memory pushed to swap: the job that grew fastest in the last 5 min if it holds more than 20 GB, otherwise the
  newest one (HIGH jobs last, GPU jobs never). It resumes by itself once free RAM minus its swapped-out memory is
  above 10 GB. Other jobs keep starting around a frozen job; once it has been frozen 2 h, no new job starts until it
  resumes. `status` shows it as `FROZEN`. A frozen job loses no work, only time.

- Processes left behind by a finished job are killed (they would otherwise hold RAM).
- A running job that holds > 1 GB and used no CPU for `--stall-min` minutes is killed. `status` marks it `STALLED`
  after 10 min.
- Processes outside the queue (any user) that hold > 2 GB and have been idle 30 min are listed as `IDLE-HOG` in
  `status`. They are reported only, never killed. Tell the user about them.

## Rules for agents

1. Estimate `--mem`, `--threads` and `--time` honestly. Measure on a small run first if unsure (`done/*.json` records
   `peak_ram_gb`).
2. Don't touch other projects' jobs, and don't `cancel` anything you didn't submit unless the user says so.
3. `touch $JOBQ_DIR/PAUSE` stops new starts (running jobs continue). Only do this if the user asks.
4. If `jobq status` says `daemon DOWN`, tell the user. Don't start jobs outside the queue.
5. Short, light commands (compiling a small file, a quick test under 2 GB / 2 threads / a few minutes) don't need the
   queue.

## Admin

See README.md: install, daemon log (`$JOBQ_DIR/daemon.log`, `systemctl status jobq`), `touch $JOBQ_DIR/RELOAD` to load
new code without stopping jobs, tuning constants at the top of `jobq.py`.
