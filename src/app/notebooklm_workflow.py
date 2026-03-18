from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from src.utils.refresh_notebooklm_cookies import refresh_cookies
from src.api.notebooklm_api import AuthError, NotebookLMAPI
from src.utils.helpers import (
    list_papers,
    load_questions,
    load_results,
    load_sessions,
    save_results,
    save_sessions,
)


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
    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths
        self._login_lock = Lock()
        self._results_lock = Lock()
        self._sessions_lock = Lock()

    def run(self, thread_count: int = 1) -> None:
        questions = load_questions(self._paths.questions_path)
        results = load_results(self._paths.results_path)
        papers = list_papers(self._paths.papers_dir)
        sessions = load_sessions(self._paths.sessions_path)

        if not self._ensure_login():
            return

        if thread_count <= 1:
            for paper_path in papers:
                self._process_paper(paper_path, questions, results, sessions)
        else:
            with ThreadPoolExecutor(max_workers=thread_count) as executor:
                futures = [
                    executor.submit(self._process_paper, paper, questions, results, sessions)
                    for paper in papers
                ]
                for future in futures:
                    future.result()

        print(f"Finished. Results saved to {self._paths.results_path}.")

    def _process_paper(
        self,
        paper_path: Path,
        questions: list[str],
        results: dict,
        sessions: dict,
    ) -> None:
        for attempt in range(2):
            notebook_api = self._ensure_login()
            if not notebook_api:
                return

            try:
                self._process_paper_with_api(
                    notebook_api,
                    paper_path,
                    questions,
                    results,
                    sessions,
                )
                return
            except AuthError:
                if attempt == 0:
                    print("Authentication expired. Retrying article...")
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
    ) -> None:
        paper_name = paper_path.name
        with self._sessions_lock:
            session = sessions.setdefault(paper_name, {})
            if session.get("source_name") != paper_name:
                session.clear()
                session["source_name"] = paper_name

        notebook_id = session.get("notebook_id")
        if notebook_id and not notebook_api.check_notebook_exists(notebook_id):
            print(f"Notebook missing for {paper_name}. Resetting session.")
            with self._sessions_lock:
                session.clear()
                session["source_name"] = paper_name
                save_sessions(self._paths.sessions_path, sessions)

        with self._results_lock:
            paper_results = results.setdefault(paper_name, {})

        if all(paper_results.get(question) for question in questions):
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
                    }
                )
                save_sessions(self._paths.sessions_path, sessions)

        source_id = session.get("source_id")
        if not source_id:
            source_id = notebook_api.pre_attachment(paper_name, notebook_id)
            with self._sessions_lock:
                session["source_id"] = source_id
                session["uploaded"] = False
                session["processed"] = False
                save_sessions(self._paths.sessions_path, sessions)

        if not session.get("uploaded"):
            upload_id, upload_protocol = notebook_api.attachment_handshake(
                paper_path, notebook_id, source_id
            )
            notebook_api.attach_finally(paper_path, upload_id, upload_protocol)
            with self._sessions_lock:
                session["uploaded"] = True
                save_sessions(self._paths.sessions_path, sessions)

        if not session.get("processed"):
            notebook_api.wait_for_processing(notebook_id)
            with self._sessions_lock:
                session["processed"] = True
                save_sessions(self._paths.sessions_path, sessions)

        for question in questions:
            with self._results_lock:
                if paper_results.get(question):
                    continue

            answer = notebook_api.send_message(notebook_id, source_id, question)
            with self._results_lock:
                paper_results[question] = answer or ""
                save_results(self._paths.results_path, results)

    def _ensure_login(self) -> NotebookLMAPI | None:
        notebook_api = NotebookLMAPI()
        if notebook_api.check_success_login():
            return notebook_api

        with self._login_lock:
            notebook_api = NotebookLMAPI()
            if notebook_api.check_success_login():
                return notebook_api

            print("Login failed. Attempting to refresh cookies...")
            try:
                refreshed = refresh_cookies(force=True)
            except Exception as exc:
                print(f"Cookie refresh failed: {exc}")
                return None

            if not refreshed:
                print("Cookie refresh did not produce a valid session.")
                return None

            notebook_api = NotebookLMAPI()
            if not notebook_api.check_success_login():
                print("Login failed after refreshing cookies.")
                return None

            return notebook_api
