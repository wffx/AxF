from __future__ import annotations

import argparse
from pathlib import Path

from .executors import execute_python_skill
from .lane_scheduler import LaneScheduler
from .state_store import StateStore
from .workflow_loader import load_workflow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an AxF workflow")
    parser.add_argument("workflow")
    parser.add_argument("--repo", default="/linux-7.0")
    parser.add_argument("--function", required=True)
    parser.add_argument("--file", default="")
    parser.add_argument("--db", default="")
    parser.add_argument("--krepo-root", default="")
    parser.add_argument("--state-root", default="")
    args = parser.parse_args(argv)

    workflow = load_workflow(args.workflow)
    state_store = StateStore(Path(args.state_root) if args.state_root else None)
    scheduler = LaneScheduler(workflow, execute_python_skill, state_store=state_store)
    state = scheduler.run(
        {
            "repo": args.repo,
            "function": args.function,
            "file": args.file,
            "db": args.db,
            "krepo_root": args.krepo_root,
        }
    )
    return 0 if state.status.value == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
