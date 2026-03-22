import argparse
from pathlib import Path

from src.app.notebooklm_workflow import AppPaths, NotebookLMWorkflow

def main() -> None:
    parser = argparse.ArgumentParser(description="NotebookLM batch runner")
    parser.add_argument(
        "--threads",
        type=int,
        default=1,
        help="Number of parallel threads to use.",
    )
    parser.add_argument(
        "--add-new-account",
        action="store_true",
        help="Add a new Google account profile for NotebookLM.",
    )
    parser.add_argument(
        "--account-email",
        default="",
        help="Optional email label for the new account.",
    )
    parser.add_argument(
        "--cooldown-minutes",
        type=int,
        default=None,
        help="Cooldown in minutes before retrying an account that hit the limit.",
    )
    args = parser.parse_args()

    thread_count = max(1, args.threads)
    base_dir = Path(__file__).resolve().parent
    paths = AppPaths.from_base_dir(base_dir)
    workflow = NotebookLMWorkflow(paths, cooldown_minutes=args.cooldown_minutes)

    if args.add_new_account:
        account_id = workflow.add_new_account(args.account_email or None)
        print(f"Added account {account_id}.")
        return

    workflow.run(thread_count=thread_count)

if __name__ == "__main__":
    main()

