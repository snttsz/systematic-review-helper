from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from src.api.notebooklm_api import AuthError, ConversationLimitError, NotebookLMAPI
from src.utils.helpers import (
    list_papers,
    load_questions,
    load_results,
    load_sessions,
    is_blank_answer,
    save_results,
    save_sessions,
)
from src.utils.account_manager import AccountManager


@dataclass(frozen=True)
class AppPaths:
    base_dir: Path
    data_dir: Path
    papers_dir: Path
    questions_path: Path
    results_path: Path
    sessions_path: Path

    @classmethod
    def from_base_dir(cls, base_dir: Path) -> "AppPaths":
        data_dir = base_dir / "data"
        return cls(
            base_dir=base_dir,
            data_dir=data_dir,
            papers_dir=base_dir / "papers",
            questions_path=data_dir / "questions.json",
            results_path=data_dir / "results.json",
            sessions_path=data_dir / "memory" / "sessions_ids.json",
        )


class NotebookLMWorkflow:
    def __init__(self, paths: AppPaths, cooldown_minutes: int | None = None) -> None:
        self._paths = paths
        self._results_lock = Lock()
        self._sessions_lock = Lock()
        self._account_manager = AccountManager(paths.data_dir, cooldown_minutes=cooldown_minutes)

    def run(self, thread_count: int = 1) -> None:
        questions = load_questions(self._paths.questions_path)
        results = load_results(self._paths.results_path)
        papers = list_papers(self._paths.papers_dir)
        try:
            self._account_manager.ensure_active_api()
        except Exception as exc:
            print(f"Login failed: {exc}")
            return

        if thread_count <= 1:
            for paper_path in papers:
                self._process_paper(paper_path, questions, results)
        else:
            with ThreadPoolExecutor(max_workers=thread_count) as executor:
                futures = [
                    executor.submit(self._process_paper, paper, questions, results)
                    for paper in papers
                ]
                for future in futures:
                    future.result()

        print(f"Finished. Results saved to {self._paths.results_path}.")

    def add_new_account(self, user_email: str | None = None) -> str:
        return self._account_manager.add_new_account(user_email)

    def _process_paper(
        self,
        paper_path: Path,
        questions: list[str],
        results: dict,
    ) -> None:
        while True:
            context = self._account_manager.get_active_context()
            sessions = load_sessions(context.sessions_path)

            for attempt in range(2):
                notebook_api = self._account_manager.ensure_active_api()
                try:
                    self._process_paper_with_api(
                        notebook_api,
                        paper_path,
                        questions,
                        results,
                        sessions,
                        context.account_id,
                        context.sessions_path,
                    )
                    return
                except ConversationLimitError:
                    print("Conversation limit reached. Switching account...")
                    if not self._account_manager.mark_limit_and_rotate(context.account_id):
                        print("All accounts reached the conversation limit. Stopping.")
                        raise SystemExit(1)
                    break
                except AuthError:
                    if attempt == 0:
                        print("Authentication expired. Retrying article...")
                        self._account_manager.refresh_account(context.account_id)
                        continue
                    print("Authentication failed after retry. Skipping article.")
                    return

    def _process_paper_with_api(
        self,
        notebook_api: NotebookLMAPI,
        paper_path: Path,
        questions: list[str],
        results: dict,
        sessions: dict,
        account_id: str,
        sessions_path: Path,
    ) -> None:
        paper_name = paper_path.name
        with self._sessions_lock:
            session = sessions.setdefault(paper_name, {})
            if session.get("source_name") != paper_name:
                session.clear()
                session["source_name"] = paper_name
            session["account_id"] = account_id

        notebook_id = session.get("notebook_id")
        if notebook_id and not notebook_api.check_notebook_exists(notebook_id):
            print(f"Notebook missing for {paper_name}. Resetting session.")
            with self._sessions_lock:
                session.clear()
                session["source_name"] = paper_name
                session["account_id"] = account_id
                save_sessions(sessions_path, sessions)

        with self._results_lock:
            paper_results = results.setdefault(paper_name, {})

        if all(not is_blank_answer(paper_results.get(question)) for question in questions):
            return

        print(f"Processing {paper_name}...")

        notebook_id = session.get("notebook_id")
        if not notebook_id:
            notebook_id = notebook_api.create_notebook()
            with self._sessions_lock:
                session.update(
                    {
                        "notebook_id": notebook_id,
                        "source_id": None,
                        "uploaded": False,
                        "processed": False,
                        "source_name": paper_name,
                        "account_id": account_id,
                    }
                )
                save_sessions(sessions_path, sessions)

        source_id = session.get("source_id")
        if not source_id:
            source_id = notebook_api.pre_attachment(paper_name, notebook_id)
            with self._sessions_lock:
                session["source_id"] = source_id
                session["uploaded"] = False
                session["processed"] = False
                save_sessions(sessions_path, sessions)

        if not session.get("uploaded"):
            upload_id, upload_protocol = notebook_api.attachment_handshake(
                paper_path, notebook_id, source_id
            )
            notebook_api.attach_finally(paper_path, upload_id, upload_protocol)
            with self._sessions_lock:
                session["uploaded"] = True
                save_sessions(sessions_path, sessions)

        if not session.get("processed"):
            notebook_api.wait_for_processing(notebook_id)
            with self._sessions_lock:
                session["processed"] = True
                save_sessions(sessions_path, sessions)

        for question in questions:
            with self._results_lock:
                if not is_blank_answer(paper_results.get(question)):
                    continue

            answer = notebook_api.send_message(notebook_id, source_id, question)
            self._account_manager.record_question(account_id)
            with self._results_lock:
                paper_results[question] = "" if is_blank_answer(answer) else answer
                save_results(self._paths.results_path, results)
