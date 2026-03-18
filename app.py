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
    args = parser.parse_args()

    thread_count = max(1, args.threads)
    base_dir = Path(__file__).resolve().parent
    workflow = NotebookLMWorkflow(AppPaths.from_base_dir(base_dir))
    workflow.run(thread_count=thread_count)

if __name__ == "__main__":
    main()

