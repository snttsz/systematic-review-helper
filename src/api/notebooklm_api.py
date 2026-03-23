
import json
import httpx
import re
from pathlib import Path
import time
from httpx import Client
from dotenv import load_dotenv
import os
from urllib.parse import urlparse, parse_qs

load_dotenv(override=True)

class AuthError(Exception):
    pass


class ConversationLimitError(AuthError):
    pass

class NotebookLMAPI:

    def __init__(self, cookies: str | None = None, user_email: str | None = None):
        raw_cookies = cookies if cookies is not None else os.getenv("COOKIES")

        cookies = dict(
            item.strip().split("=", 1)
            for item in raw_cookies.split(";")
            if item.strip()
        )
        
        self.client = Client(cookies=cookies, timeout=30)
        self.host = "https://notebooklm.google.com"

        self.user_email = user_email if user_email is not None else os.getenv("USER_EMAIL", "")

        self.action_token = None
        self.f_sid = None

    def check_success_login(self):

        user_email = self.user_email

        response = self.client.get(self.host)

        if self._is_unauthorized(response):
            return False

        match = re.search(r'"SNlM0e":"(.*?)"', response.text)
        action_token = match.group(1) if match else None

        match = re.search(r'"FdrFJe":"(.*?)"', response.text)
        f_sid = match.group(1) if match else None

        if response.status_code == 200 and user_email in response.text:
            self.action_token = action_token
            self.f_sid = f_sid
            return True
        
        return False

    def check_notebook_exists(self, notebook_id: str) -> bool:
        if not notebook_id:
            return False

        response = self.client.get(f"{self.host}/notebook/{notebook_id}")
        self._raise_if_unauthorized(response)
        return response.status_code == 200
    
    def create_notebook(self):

        path = "/_/LabsTailwindUi/data/batchexecute"

        params = {
            "rpcids": "CCqFvf",
            "source-path": "/",
            "bl": "boq_labs-tailwind-frontend_20260316.13_p0",
            "f.sid": self.f_sid,
            "hl": "pt",
            "_reqid": self._get_google_reqid(),
            "rt": "c"
        }

        data = {
            "f.req": '[[["CCqFvf","[\\"\\",null,null,[2],[1,null,null,null,null,null,null,null,null,null,[1]]]",null,"generic"]]]',
            "at": self.action_token
        }

        response = self.client.post(self.host + path, params=params, data=data)
        self._raise_if_unauthorized(response)

        ids = re.findall(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', response.text)

        if ids:
            notebook_id = ids[0]
            target_id = ids[1]
        else:
            raise Exception("Failed to create notebook. No valid IDs found in response. Response text: " + response.text)

        return notebook_id
    
    def pre_attachment(self, file_name : str, notebook_id): 

        path = "/_/LabsTailwindUi/data/batchexecute"

        params = {
            "rpcids": "o4cbdc",
            "source-path": f"/notebook/{notebook_id}",
            "bl": "boq_labs-tailwind-frontend_20260315.03_p0",
            "f.sid": self.f_sid,
            "hl": "pt",
            "_reqid": self._get_google_reqid(),
            "rt": "c"
        }

        data = {
            "f.req": f'[[["o4cbdc","[[[\\"{file_name}\\"]],\\"{notebook_id}\\",[2],[1,null,null,null,null,null,null,null,null,null,[1]]]",null,"generic"]]]',
            "at": self.action_token
        }

        response = self.client.post(self.host + path, params=params, data=data)
        self._raise_if_unauthorized(response)

        ids = re.findall(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', response.text)
        source_id = list(set(ids))[0]

        if not source_id:
            raise Exception("Failed to pre-attach file. No valid source ID found in response. Response text: " + response.text)

        return source_id
    
    def attachment_handshake(self, source_path: Path, notebook_id, source_id):

        path = "/upload/_/"

        params = {
            "authuser": "0"
        }

        file_name = source_path.name
        file_size = str(source_path.stat().st_size)

        json_payload = {
            "PROJECT_ID": notebook_id,
            "SOURCE_NAME": file_name,
            "SOURCE_ID": source_id
        }

        extra_headers = {
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": file_size,
            "X-Goog-Authuser": "0",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "X-Same-Domain": "1",
        }

        missing = object()
        original_header_values = {
            key: self.client.headers.get(key, missing)
            for key in extra_headers
        }

        self.client.headers.update(extra_headers)

        raw_data = json.dumps(json_payload, separators=(',', ':'))

        try:
            response = self.client.post(self.host + path, params=params, content=raw_data)
        finally:
            for key, original_value in original_header_values.items():
                if original_value is missing:
                    self.client.headers.pop(key, None)
                else:
                    self.client.headers[key] = original_value

        self._raise_if_unauthorized(response)

        upload_url = response.headers.get("X-Goog-Upload-Url")

        if not upload_url:
            raise Exception("Failed to get upload URL. Response headers: " + str(response.headers))
        
        parsed_url = urlparse(upload_url)
        params = parse_qs(parsed_url.query)

        upload_id = params.get("upload_id", [None])[0]
        upload_protocol = params.get("upload_protocol", [None])[0]  

        if not upload_id or not upload_protocol:
            raise Exception("Failed to parse upload URL. Missing upload_id or upload_protocol. Upload URL: " + upload_url)
        
        return upload_id, upload_protocol

    def attach_finally(self, source_path: Path, upload_id, upload_protocol):

        url = "/upload/_/"

        params = {
            "authuser": "0",
            "upload_id": upload_id,
            "upload_protocol": upload_protocol
        }

        extra_headers = {
            "X-Goog-Upload-Command": "upload, finalize",
            "X-Goog-Upload-Offset": "0",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"
        }

        missing = object()
        original_header_values = {
            key: self.client.headers.get(key, missing)
            for key in extra_headers
        }

        self.client.headers.update(extra_headers)

        with open(source_path, "rb") as f:
            file_data = f.read()

        try:
            response = self.client.post(self.host + url, params=params, content=file_data, timeout=100)
        finally:
            for key, original_value in original_header_values.items():
                if original_value is missing:
                    self.client.headers.pop(key, None)
                else:
                    self.client.headers[key] = original_value

        self._raise_if_unauthorized(response)

    def check_answer_status(self, notebook_id):

        path = "/_/LabsTailwindUi/data/batchexecute"

        params = {
            "rpcids": "VfAZjd",
            "source-path": f"/notebook/{notebook_id}",
            "bl": "boq_labs-tailwind-frontend_20260316.13_p0",
            "f.sid": self.f_sid,
            "hl": "pt",
            "_reqid": self._get_google_reqid(),
            "rt": "c"
        }

        f_req_payload = f'[[["VfAZjd","[\\"{notebook_id}\\",[2]]",null,"generic"]]]'

        data = {
            "f.req": f_req_payload,
            "at": self.action_token
        }

        response = self.client.post(self.host + path, params=params, data=data)
        self._raise_if_unauthorized(response)

        return response.text

    def wait_for_processing(self, notebook_id, max_retries=100, delay=10):

        for _ in range(max_retries):
            try:
                raw_response = self.check_answer_status(notebook_id)
            except httpx.ConnectError as exc:
                time.sleep(delay)
                continue
            
            if not raw_response:
                time.sleep(delay)
                continue

            clean_text = raw_response.replace(")]}'", "").strip()
            size_match = re.search(r'^\d+$', clean_text, re.MULTILINE)

            if size_match:
                block_size = int(size_match.group())

                if block_size > 300:
                    outer_data = self._parse_google_batch_response(raw_response)
                    return outer_data
            
            time.sleep(delay)
        
        raise Exception("Processing timed out after multiple attempts.")
    
    def send_message(self, notebook_id, source_id, message: str):

        path = "/_/LabsTailwindUi/data/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateFreeFormStreamed"

        params = {
            "bl": "boq_labs-tailwind-frontend_20260316.13_p0",
            "f.sid": self.f_sid,
            "hl": "pt",
            "_reqid": self._get_google_reqid(),
            "rt": "c"
        }

        payload_str = json.dumps([
            [[[source_id]]], 
            message, 
            None, 
            [2, None, [1], [1]], 
            notebook_id, 
            None, 
            None, 
            notebook_id,
            1
        ])

        data = {
            "f.req": json.dumps([None, payload_str]),
            "at": self.action_token
        }

        response = self.client.post(self.host + path, params=params, data=data, timeout=120)

        self._raise_if_unauthorized(response)
        if self._has_user_displayable_error(response.text):
            self._debug_print_error(message, response.text)
            self._dump_debug_response(response.text, notebook_id, source_id, message)
            raise ConversationLimitError("NotebookLM conversation limit reached.")
        final_answer = self._extract_final_answer(response.text)
        self._debug_print_answer(message, final_answer, response.text)
        if not final_answer:
            self._dump_debug_response(response.text, notebook_id, source_id, message)

        return final_answer
    
    def _parse_google_batch_response(self, response_text):

        clean_text = response_text.replace(")]}'", "").strip()
        match = re.search(r'\[.*\]', clean_text, re.DOTALL)
        if not match:
            return None
            
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return None

    def _extract_final_answer(self, response_text: str):
        if not response_text:
            return None

        clean_text = response_text.replace(")]}'", "")
        decoder = json.JSONDecoder()
        idx = 0
        last_answer = None

        while idx < len(clean_text):
            start = clean_text.find("[", idx)
            if start == -1:
                break

            try:
                obj, end = decoder.raw_decode(clean_text[start:])
            except json.JSONDecodeError:
                idx = start + 1
                continue

            idx = start + end

            if not isinstance(obj, list):
                continue

            for entry in obj:
                if not isinstance(entry, list) or len(entry) < 3:
                    continue
                if entry[0] != "wrb.fr":
                    continue

                payload = entry[2]
                if not isinstance(payload, str):
                    continue

                try:
                    inner, _ = decoder.raw_decode(payload)
                except json.JSONDecodeError:
                    continue

                text = self._find_first_string(inner)
                if text:
                    last_answer = text

        return last_answer

    def _find_first_string(self, node):
        queue = [node]

        while queue:
            current = queue.pop(0)
            if isinstance(current, str):
                return current
            if isinstance(current, list):
                queue.extend(current)

        return None

    def _dump_debug_response(self, response_text: str, notebook_id: str, source_id: str, message: str) -> None:
        debug_flag = os.getenv("NOTEBOOKLM_DEBUG_RESPONSES", "").strip().lower()
        if debug_flag not in {"1", "true", "yes"}:
            return

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        debug_dir = Path(os.getenv("NOTEBOOKLM_DEBUG_DIR", "data/memory/response_dumps"))
        debug_dir.mkdir(parents=True, exist_ok=True)

        safe_message = re.sub(r"[^a-zA-Z0-9_\-]+", "_", (message or "")[:60])
        file_name = f"{timestamp}_{safe_message}.txt"
        file_path = debug_dir / file_name

        header = (
            f"notebook_id: {notebook_id}\n"
            f"source_id: {source_id}\n"
            f"question: {message}\n"
            "-----\n"
        )

        file_path.write_text(header + (response_text or ""), encoding="utf-8")

    def _debug_print_answer(self, message: str, final_answer: str | None, response_text: str) -> None:
        debug_flag = os.getenv("NOTEBOOKLM_DEBUG_PRINT", "").strip().lower()
        if debug_flag not in {"1", "true", "yes"}:
            return

        safe_question = (message or "").replace("\n", " ").strip()
        if len(safe_question) > 120:
            safe_question = safe_question[:117] + "..."

        if final_answer and final_answer.strip():
            print(f"[NotebookLM] Q: {safe_question}")
            print(f"[NotebookLM] A: {final_answer}")
            return

        raw_preview = (response_text or "").replace("\n", " ").strip()
        if len(raw_preview) > 300:
            raw_preview = raw_preview[:297] + "..."

        print(f"[NotebookLM] Q: {safe_question}")
        print("[NotebookLM] A: (vazio)")
        if raw_preview:
            print(f"[NotebookLM] raw: {raw_preview}")

    def _debug_print_error(self, message: str, response_text: str) -> None:
        debug_flag = os.getenv("NOTEBOOKLM_DEBUG_PRINT", "").strip().lower()
        if debug_flag not in {"1", "true", "yes"}:
            return

        safe_question = (message or "").replace("\n", " ").strip()
        if len(safe_question) > 120:
            safe_question = safe_question[:117] + "..."

        raw_preview = (response_text or "").replace("\n", " ").strip()
        if len(raw_preview) > 300:
            raw_preview = raw_preview[:297] + "..."

        print(f"[NotebookLM] Q: {safe_question}")
        print("[NotebookLM] error: UserDisplayableError")
        if raw_preview:
            print(f"[NotebookLM] raw: {raw_preview}")

    def _has_user_displayable_error(self, response_text: str) -> bool:
        if not response_text:
            return False
        return "UserDisplayableError" in response_text
    
    def _get_google_reqid(self):
        return str(int(time.time() * 1000) % 1000000)

    def _is_unauthorized(self, response: httpx.Response) -> bool:
        if response.status_code in (401, 403):
            return True
        if "accounts.google.com" in str(response.url):
            return True

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            if "accounts.google.com" in location:
                return True

        return False

    def _raise_if_unauthorized(self, response: httpx.Response) -> None:
        if self._is_unauthorized(response):
            raise AuthError("Authentication expired.")


if __name__ == "__main__":
    api = NotebookLMAPI()
    if not api.check_success_login():
        print("Login failed. Check COOKIES and USER_EMAIL.")
        raise SystemExit(1)

    papers_dir = Path("papers")
    if not papers_dir.exists():
        print("Missing papers/ directory.")
        raise SystemExit(1)

    candidates = sorted([p for p in papers_dir.iterdir() if p.is_file()])
    if not candidates:
        print("No files found in papers/.")
        raise SystemExit(1)

    source_path = candidates[0]
    print(f"Using file: {source_path}")

    notebook_id = api.create_notebook()
    print(f"Notebook created: {notebook_id}")

    source_id = api.pre_attachment(source_path.name, notebook_id)
    print(f"Source pre-attached: {source_id}")

    upload_id, upload_protocol = api.attachment_handshake(source_path, notebook_id, source_id)
    api.attach_finally(source_path, upload_id, upload_protocol)
    print("File uploaded. Waiting for processing...")

    api.wait_for_processing(notebook_id)
    print("Processing completed. Sending question...")

    question = "Resumo em 2 frases."
    response = api.send_message(notebook_id, source_id, question)
    print("Server response (final answer):")
    print(response)
    

