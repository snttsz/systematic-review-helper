import json
from pathlib import Path


def load_questions(path: Path) -> list[str]:
	with open(path, "r", encoding="utf-8") as file:
		data = json.load(file)
	if isinstance(data, dict):
		return list(data.keys())
	if isinstance(data, list):
		return [item for item in data if isinstance(item, str)]
	return []


def load_results(path: Path) -> dict:
	if not path.exists():
		return {}
	with open(path, "r", encoding="utf-8") as file:
		try:
			return json.load(file)
		except json.JSONDecodeError:
			return {}


def save_results(path: Path, results: dict) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with open(path, "w", encoding="utf-8") as file:
		json.dump(results, file, indent=2, ensure_ascii=False)


def list_papers(papers_dir: Path) -> list[Path]:
	if not papers_dir.exists():
		return []
	return sorted(
		[path for path in papers_dir.iterdir() if path.is_file()],
		key=lambda p: p.name.lower(),
	)

def load_sessions(path: Path) -> dict:
	if not path.exists():
		return {}
	with open(path, "r", encoding="utf-8") as file:
		try:
			return json.load(file)
		except json.JSONDecodeError:
			return {}


def save_sessions(path: Path, sessions: dict) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with open(path, "w", encoding="utf-8") as file:
		json.dump(sessions, file, indent=2, ensure_ascii=False)
