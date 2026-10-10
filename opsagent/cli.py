"""Command line entry point.

  opsagent run "Process the latest invoice from Acme"      # terminal approvals
  opsagent run "..." --approvals console                   # approve from the web console
  opsagent run "..." --headed                              # watch the browser
  opsagent resume <run_id>                                 # continue an interrupted run
  opsagent sandbox [--reset] [--faults flaky_submit,layout_b]
  opsagent upkeep --watch [--workers 4]                    # background reflection worker pool
"""

import argparse
import sys

import httpx

from opsagent import config
from opsagent.human import FileChannel, TerminalChannel
from opsagent.runtime import Operator
from opsagent.store import RunStore


def _sandbox(args) -> None:
    if args.reset:
        httpx.post(f"{config.SANDBOX_URL}/__admin/reset", timeout=30).raise_for_status()
        print("sandbox reset")
    if args.faults is not None:
        faults = [f for f in args.faults.split(",") if f]
        print(httpx.post(f"{config.SANDBOX_URL}/__admin/faults", json={"faults": faults}, timeout=10).json())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="opsagent")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run a request")
    run.add_argument("request")
    resume = sub.add_parser("resume", help="resume an interrupted run")
    resume.add_argument("run_id")
    for sp in (run, resume):
        sp.add_argument("--approvals", choices=["terminal", "console"], default="terminal")
        sp.add_argument("--headed", action="store_true", help="show the browser window")
        sp.add_argument("--max-steps", type=int, default=60)
        sp.add_argument("--no-memory", action="store_true", help="disable experience memory (ablation)")
    run.add_argument("--run-id", default=None)
    up = sub.add_parser("upkeep", help="background memory upkeep (reflection off the critical path)")
    up.add_argument("--watch", action="store_true", help="keep running and process new runs as they finish")
    up.add_argument("--workers", type=int, default=4)
    sb = sub.add_parser("sandbox", help="control the sandbox company")
    sb.add_argument("--reset", action="store_true")
    sb.add_argument("--faults", default=None, help="comma list: flaky_submit,layout_b,toasts,session_expiry ('' = none)")
    args = p.parse_args(argv)

    if args.cmd == "sandbox":
        _sandbox(args)
        return 0
    if args.cmd == "upkeep":
        from opsagent.upkeep import FileQueue, WorkerPool
        queue = FileQueue()
        print(f"upkeep: {args.workers} workers, queue {queue.root} {queue.counts()}"
              + (" — watching (Ctrl-C to stop)" if args.watch else ""))
        try:
            WorkerPool(queue, workers=args.workers).run(watch=args.watch)
        except KeyboardInterrupt:
            pass
        print("upkeep:", queue.counts())
        return 0
    if args.cmd == "run":
        request = args.request
    else:
        cp = RunStore(args.run_id).load_checkpoint()
        if not cp:
            print(f"no checkpoint for run {args.run_id}")
            return 1
        request = cp["request"]
    op = Operator(request, human=TerminalChannel(), run_id=args.run_id, headless=not args.headed,
                  max_steps=args.max_steps, memory=not args.no_memory)
    if args.approvals == "console":
        op.ctx.human = FileChannel(op.store)
    result = op.run()
    return 0 if result.status in ("verified", "justified_stop") else 2


if __name__ == "__main__":
    sys.exit(main())
