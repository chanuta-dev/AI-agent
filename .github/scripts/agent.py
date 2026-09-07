import os
import json
import re
import subprocess
import urllib.request
import urllib.error
import time

GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]

def parse_agent_response(raw_text):
    """מפענח סופר-עמיד שמחלץ פעולות, קבצים ותגובות בכל תרחיש."""
    response_data = {}
    files_to_update = []
    
    json_obj = None
    json_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw_text)
    candidate_text = json_match.group(1) if json_match else raw_text
    
    start = candidate_text.find("{")
    end = candidate_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            json_obj = json.loads(candidate_text[start:end + 1], strict=False)
        except Exception:
            pass

    if isinstance(json_obj, dict):
        response_data = json_obj
        raw_files = json_obj.get("files_to_update", [])
        if isinstance(raw_files, dict):
            for path, content in raw_files.items():
                files_to_update.append({"path": path, "content": content})
        elif isinstance(raw_files, list):
            for item in raw_files:
                if isinstance(item, dict) and "path" in item:
                    files_to_update.append(item)
    else:
        action_m = re.search(r'"action"\s*:\s*"([^"]+)"', raw_text)
        action = action_m.group(1) if action_m else "chat"
        
        commit_m = re.search(r'"commit_message"\s*:\s*"([^"]+)"', raw_text)
        commit_msg = commit_m.group(1) if commit_m else "AI auto-update"
        
        branch_m = re.search(r'"branch_name"\s*:\s*"([^"]+)"', raw_text)
        branch_name = branch_m.group(1) if branch_m else None
        
        chat_m = re.search(r'"chat_response"\s*:\s*"([\s\S]*?)(?="\s*,\s*"[a-zA-Z_]+"|"\s*\}|$)', raw_text)
        chat_response = chat_m.group(1) if chat_m else "השינויים בוצעו בהצלחה."
        chat_response = chat_response.replace('\\n', '\n').replace('\\"', '"')
        
        response_data = {
            "action": action,
            "commit_message": commit_msg,
            "branch_name": branch_name,
            "chat_response": chat_response
        }
        
        file_pattern = re.compile(
            r'"([\w\./\-]+\.\w+)"\s*:\s*"([\s\S]*?)(?=",\s*"[\w\./\-]+\.\w+"\s*:|"\s*\}\s*,\s*"chat_response"|"\s*\}\s*$)',
            re.MULTILINE
        )
        for match in file_pattern.finditer(raw_text):
            fpath = match.group(1)
            fcontent = match.group(2).replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
            if not fpath.startswith(".github/workflows/"):
                files_to_update.append({"path": fpath, "content": fcontent})

    block_pattern = re.compile(
        r'(?:\*\*\*\s*FILE:\s*([^\s\*]+)\s*\*\*\*|###\s*FILE:\s*([^\n]+))\s*\n```[a-zA-Z]*\n([\s\S]*?)\n```',
        re.MULTILINE
    )
    for match in block_pattern.finditer(raw_text):
        fpath = (match.group(1) or match.group(2)).strip()
        fcontent = match.group(3)
        if not fpath.startswith(".github/workflows/"):
            files_to_update.append({"path": fpath, "content": fcontent})
            
    response_data["files_to_update"] = files_to_update
    return response_data

def github_api_request(url, token, data=None, method="GET"):
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "GitHub-Agent-Bot"
    }
    encoded_data = json.dumps(data).encode("utf-8") if data else None
    if encoded_data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())

def build_file_tree(file_list, max_depth=3):
    tree = {}
    for path in sorted(file_list):
        normalized = path.replace("\\", "/")
        parts = normalized.split("/")
        if len(parts) > max_depth + 1:
            parts = parts[:max_depth] + [f"... ({len(parts) - max_depth} files)"]
        curr = tree
        for part in parts:
            curr = curr.setdefault(part, {})
    
    def render(node, prefix=""):
        lines = []
        keys = sorted(node.keys())
        for i, k in enumerate(keys):
            is_last = (i == len(keys) - 1)
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{k}")
            if node[k]:
                sub_prefix = prefix + ("    " if is_last else "│   ")
                lines.extend(render(node[k], sub_prefix))
        return lines
        
    return "\n".join(render(tree))

def get_repo_files_and_content(issue_context_text=""):
    repo_files = {}
    file_list = []
    
    IGNORE_DIRS = {
        '.git', '__pycache__', '.agent_core', 'node_modules', 'build', '.gradle', 
        'bin', 'out', '.idea', 'target', '.vscode', 'res', 'drawable', 'mipmap'
    }
    VALID_EXTENSIONS = ('.py', '.java', '.kt', '.json', '.md', '.yml', '.yaml', '.gradle', '.xml', '.ts', '.js', '.properties')
    
    MAX_TOTAL_CHARS = 25000
    current_chars = 0

    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith('.') and not d.startswith('values-')]
        for f in files:
            filepath = os.path.normpath(os.path.join(root, f)).replace("\\", "/")
            if filepath.startswith("./"):
                filepath = filepath[2:]
            file_list.append(filepath)

    repo_tree = build_file_tree(file_list, max_depth=3)

    issue_words = set(re.findall(r'[\w\.-]+', issue_context_text.lower()))
    
    def priority_score(filepath):
        score = 0
        fname = os.path.basename(filepath).lower()
        if any(k in fname for k in ['summery_for_ai', 'summary_for_ai', 'project.md']):
            score += 200
        if fname in issue_words or os.path.splitext(fname)[0] in issue_words:
            score += 100
        if any(k in fname for k in ['readme', 'build.gradle', 'manifest', 'package.json', 'settings.gradle']):
            score += 50
        return score

    prioritized_files = sorted(file_list, key=priority_score, reverse=True)

    for filepath in prioritized_files:
        if current_chars >= MAX_TOTAL_CHARS:
            break
        if not filepath.endswith(VALID_EXTENSIONS):
            continue
            
        try:
            size = os.path.getsize(filepath)
            if size < 12000 and (current_chars + size <= MAX_TOTAL_CHARS):
                with open(filepath, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
                    repo_files[filepath] = content
                    current_chars += len(content)
        except Exception:
            pass

    return repo_tree, repo_files

def call_gemini_api(api_key, model_name, contents, system_instruction):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
            "maxOutputTokens": 65536,
            "thinkingConfig": {
                "thinkingLevel": "low"
            }
        }
    }
    
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    
    with urllib.request.urlopen(req, timeout=90) as response:
        res_data = json.loads(response.read().decode())
        candidate = res_data.get('candidates', [{}])[0]
        parts = candidate.get('content', {}).get('parts', [])
        
        text_chunks = [p['text'] for p in parts if isinstance(p, dict) and 'text' in p and not p.get('thought', False)]
        if not text_chunks:
            text_chunks = [p.get('text', '') for p in parts if isinstance(p, dict) and 'text' in p]
            
        full_text = "\n".join(text_chunks).strip()
        if not full_text:
            raise ValueError(f"Gemini החזיר פלט ריק")
            
        return parse_agent_response(full_text)

def get_available_groq_models(groq_key):
    url = "https://api.groq.com/openai/v1/models"
    headers = {
        'Authorization': f'Bearer {groq_key.strip()}',
        'User-Agent': 'Mozilla/5.0'
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            models = [m["id"] for m in data.get("data", [])]
            filtered = [m for m in models if not any(bad in m.lower() for bad in ["whisper", "guard", "tts", "vision", "orpheus"])]
            
            def sort_key(model_id):
                mid = model_id.lower()
                if mid == "groq/compound": return 0
                if "120b" in mid: return 1
                if "3.8" in mid: return 2
                if "3.6" in mid: return 3
                if "27b" in mid: return 4
                if "20b" in mid: return 5
                if "compound-mini" in mid: return 6
                if "allam" in mid: return 7
                return 50
            filtered.sort(key=sort_key)
            return filtered if filtered else models
    except Exception:
        return ["groq/compound", "openai/gpt-oss-120b"]

def call_groq_api(groq_key, contents, system_instruction):
    available_models = get_available_groq_models(groq_key)
    url = "https://api.groq.com/openai/v1/chat/completions"
    safe_messages = [{"role": "system", "content": system_instruction}]
    
    recent_contents = contents[-3:] if len(contents) > 3 else contents
    for c in recent_contents:
        role = "assistant" if c["role"] == "model" else "user"
        text = c["parts"][0]["text"]
        if len(text) > 3000:
            text = text[:3000] + "\n\n...[הטקסט קוצץ]..."
        safe_messages.append({"role": role, "content": text})
        
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {groq_key.strip()}',
        'User-Agent': 'Mozilla/5.0'
    }

    last_err = None
    for model in available_models:
        for attempt in range(2):
            try:
                payload = {
                    "model": model,
                    "messages": safe_messages,
                    "temperature": 0.2
                }
                data = json.dumps(payload).encode('utf-8')
                req = urllib.request.Request(url, data=data, headers=headers)
                
                with urllib.request.urlopen(req, timeout=40) as response:
                    res_data = json.loads(response.read().decode())
                    raw_text = res_data['choices'][0]['message']['content']
                    return model, parse_agent_response(raw_text)
            except urllib.error.HTTPError as e:
                err_body = e.read().decode('utf-8', errors='ignore')
                last_err = f"HTTP {e.code}: {err_body}"
                if e.code == 429 and attempt == 0:
                    match = re.search(r'try again in (\d+(\.\d+)?)s', err_body)
                    if match:
                        wait_sec = float(match.group(1)) + 2.0
                        if wait_sec <= 20:
                            time.sleep(wait_sec)
                            continue
                break
            except Exception as e:
                last_err = e
                break

    raise RuntimeError(f"כל מודלי Groq נכשלו: {last_err}")

def generate_with_smart_retry(gemini_keys, groq_key, contents, system_instruction):
    debug_log = []
    for model_name in GEMINI_MODELS:
        for i, key in enumerate(gemini_keys):
            try:
                result = call_gemini_api(key, model_name, contents, system_instruction)
                return f"{model_name} (מפתח #{i + 1})", result, "\n".join(debug_log)
            except urllib.error.HTTPError as e:
                err_msg = f"{model_name} Key #{i + 1} נכשל: HTTP {e.code}"
                debug_log.append(err_msg)
                continue
            except Exception as e:
                err_msg = f"{model_name} Key #{i + 1} שגיאה: {str(e)[:400]}"
                debug_log.append(err_msg)
                continue

    if groq_key:
        try:
            used_model, result = call_groq_api(groq_key, contents, system_instruction)
            return f"Groq ({used_model})", result, "\n".join(debug_log)
        except Exception as e:
            debug_log.append(f"Groq Error: {e}")

    raise RuntimeError(f"כל הניסיונות נכשלו.\nפירוט:\n" + "\n".join(debug_log))

def post_issue_comment(repo_name, issue_number, token, body):
    url = f"https://api.github.com/repos/{repo_name}/issues/{issue_number}/comments"
    try:
        github_api_request(url, token, data={"body": body}, method="POST")
    except Exception as e:
        print(f"שגיאה בשליחת תגובה: {e}", flush=True)

def main():
    github_token = os.environ["GITHUB_TOKEN"]
    groq_key = os.environ.get("GROQ_API_KEY", "")

    gemini_keys = []
    if os.environ.get("GEMINI_API_KEY"):
        gemini_keys.append(os.environ["GEMINI_API_KEY"].strip())
    for i in range(2, 11):
        k = os.environ.get(f"GEMINI_API_KEY_{i}", "").strip()
        if k and k not in gemini_keys:
            gemini_keys.append(k)

    repo_name = os.environ["REPO_NAME"]
    issue_number = int(os.environ["ISSUE_NUMBER"])

    issue_data = github_api_request(f"https://api.github.com/repos/{repo_name}/issues/{issue_number}", github_token)
    comments_data = github_api_request(f"https://api.github.com/repos/{repo_name}/issues/{issue_number}/comments", github_token)

    repo_info = github_api_request(f"https://api.github.com/repos/{repo_name}", github_token)
    default_branch = repo_info.get("default_branch", "main")

    valid_comments = []
    for comment in comments_data:
        body = comment.get("body") or ""
        if not body.strip().startswith("⚠️"):
            valid_comments.append(comment)

    issue_text_accumulator = f"{issue_data.get('title', '')} {issue_data.get('body') or ''}"
    for comment in valid_comments:
        issue_text_accumulator += f" {comment.get('body') or ''}"

    repo_tree, repo_files_content = get_repo_files_and_content(issue_text_accumulator)
    
    context_prefix = (
        f"[Repository: {repo_name}]\n"
        f"[Default Branch: {default_branch}]\n"
        f"[Directory Tree (TREE /F):\n{repo_tree}\n]\n"
        f"[Loaded Files Content:\n{json.dumps(repo_files_content, ensure_ascii=False, indent=2)}]\n\n"
    )
    
    initial_user_msg = context_prefix + f"Issue #{issue_number} Title: {issue_data.get('title', '')}\n\n{issue_data.get('body') or ''}"
    
    raw_conversation = [{"role": "user", "parts": [{"text": initial_user_msg}]}]
    for comment in valid_comments:
        author = comment.get("user", {}).get("login", "")
        role = "model" if author.endswith("[bot]") or author == "github-actions[bot]" else "user"
        raw_conversation.append({"role": role, "parts": [{"text": comment.get("body") or ""}]})

    conversation = []
    for msg in raw_conversation:
        if conversation and conversation[-1]["role"] == msg["role"]:
            conversation[-1]["parts"][0]["text"] += "\n\n" + msg["parts"][0]["text"]
        else:
            conversation.append(msg)

    while conversation and conversation[-1]["role"] != "user":
        conversation.pop()

    if not conversation:
        conversation = [{"role": "user", "parts": [{"text": initial_user_msg}]}]

    if conversation and conversation[-1]["role"] == "user":
        conversation[-1]["parts"][0]["text"] += "\n\n[CRITICAL REMINDER: If the user approved or asked to implement, you MUST output action: 'commit' WITH the actual code files in files_to_update! Do not just chat about it!]"

    # הנחיות חדות: איסור מוחלט על סיפורים בצ'אט אם המשתמש נתן אישור
    system_instruction = f"""
    You are an autonomous AI software engineer operating inside this GitHub repository (Default branch: {default_branch}).
    You communicate naturally in Hebrew.
    
    CRITICAL ANTI-LAZINESS & EXECUTION RULE:
    1. If the user approved, gave green light, or asked to implement (e.g. "יש אישור", "בצע", "תממש", "קדימה"):
       YOU MUST RETURN action: "commit" AND YOU MUST PROVIDE THE ACTUAL CODE in `files_to_update`!
    2. NEVER just say "It was implemented" or describe the code in chat without providing the actual files to commit. Talking without code is an error!
    
    CRITICAL MEMORY & PROTOCOL RULES:
    1. Always read `summery_for_AI.md` to understand current architecture and progress.
    2. Whenever performing action "commit", you MUST ALWAYS include `summery_for_AI.md` inside `files_to_update` with an updated progress/tasks section documenting what you just implemented!
    
    Action Types:
    - "chat": ONLY for answering questions or discussions when no code execution was requested.
    - "commit": When asked to write code, modify files, or when approval was given.
    """

    try:
        provider_used, response_data, debug_log = generate_with_smart_retry(gemini_keys, groq_key, conversation, system_instruction)
    except Exception as e:
        error_details = f"\n\n<details><summary>🛠️ לחץ כאן לפירוט השגיאות</summary>\n\n```text\n{str(e)}\n```\n</details>"
        post_issue_comment(repo_name, issue_number, github_token, f"⚠️ המערכת בעומס. נסה שוב בעוד מספר דקות.{error_details}")
        return

    if not isinstance(response_data, dict):
        response_data = {"action": "chat", "chat_response": str(response_data), "files_to_update": []}
        
    action = response_data.get("action", "chat")
    chat_reply = response_data.get("chat_response", "הפעולה בוצעה בהצלחה.")
    files_to_update = response_data.get("files_to_update", [])

    if files_to_update:
        action = "commit"

    if action == "commit" and files_to_update:
        try:
            branch = response_data.get("branch_name", f"ai-patch-issue-{issue_number}")
            subprocess.run(["git", "checkout", "-B", branch], check=True)
            
            valid_files = [f for f in files_to_update if not f["path"].startswith(".github/workflows/")]
            
            for item in valid_files:
                filepath = item["path"]
                if os.path.dirname(filepath):
                    os.makedirs(os.path.dirname(filepath), exist_ok=True)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(item["content"])
                subprocess.run(["git", "add", filepath], check=True)

            for del_path in response_data.get("files_to_delete", []):
                if not del_path.startswith(".github/workflows/") and os.path.exists(del_path):
                    os.remove(del_path)
                    subprocess.run(["git", "rm", del_path], check=True)

            subprocess.run(["git", "config", "--global", "user.name", "Gemini AI Agent"], check=True)
            subprocess.run(["git", "config", "--global", "user.email", "gemini-bot@github.com"], check=True)
            subprocess.run(["git", "commit", "-m", response_data.get("commit_message", "AI auto-update")], check=True)
            
            remote_url = f"https://x-access-token:{github_token}@github.com/{repo_name}.git"
            subprocess.run(["git", "remote", "set-url", "origin", remote_url], check=True)
            subprocess.run(["git", "push", "origin", branch, "--force"], check=True)

            pr_title = f"🤖 AI Update: {response_data.get('commit_message', 'Changes')}"
            pr_body = f"Closes #{issue_number}\n\n{chat_reply}\n\n*Generated with {provider_used}*"
            pr_data = {"title": pr_title, "body": pr_body, "head": branch, "base": default_branch}
            
            try:
                res = github_api_request(f"https://api.github.com/repos/{repo_name}/pulls", github_token, data=pr_data, method="POST")
                pr_url = res.get("html_url")
                summary = f"✨ **בוצע בהצלחה ({provider_used})!**\n\n{chat_reply}\n\n🔗 **Pull Request מוכן:** {pr_url}"
            except Exception as pr_err:
                pr_url = f"https://github.com/{repo_name}/tree/{branch}"
                summary = f"✨ **השינויים נדחפו לענף ({provider_used})!**\n\n{chat_reply}\n\n⚠️ שים לב: פתיחת ה-PR נכשלה ({pr_err}).\n🔗 **קישור לענף:** {pr_url}"

            post_issue_comment(repo_name, issue_number, github_token, summary)

        except Exception as e:
            post_issue_comment(repo_name, issue_number, github_token, f"⚠️ חלה שגיאה בביצוע ה-Commit: {str(e)}")
    else:
        post_issue_comment(repo_name, issue_number, github_token, chat_reply)

if __name__ == "__main__":
    main()
