import os
import json
import re
import subprocess
import urllib.request
import urllib.error
import time

GEMINI_MODELS = ["gemini-3.6-flash", "gemini-3.8-flash", "gemini-3.7-flash"]

def extract_json(raw_text):
    text = raw_text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        text = match.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
            
    text = re.sub(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)(\s*:)', r'\1"\2"\3', text)
    
    try:
        # strict=False מאפשר שורות חדשות אמיתיות בתוך המחרוזת (קריטי לקוד!)
        return json.loads(text, strict=False)
    except Exception as e:
        # רשת ביטחון: אם ה-JSON שבור לגמרי בגלל מרכאות, נדפיס אותו כצ'אט כדי שהמשתמש יראה את הקוד!
        return {
            "action": "chat",
            "chat_response": f"⚠️ **שגיאת תחביר ביצירת הקוד:** יצרתי את הפתרון, אך נוצרה שגיאת JSON פנימית (כנראה מרכאות לא שמורות בתוך הקוד). הנה הפלט המלא שרציתי לשלוח לך כדי שתוכל להעתיק את הקוד ידנית:\n\n```json\n{text}\n```"
        }

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
            "maxOutputTokens": 8192
        }
    }
    
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    
    with urllib.request.urlopen(req, timeout=75) as response:
        res_data = json.loads(response.read().decode())
        candidate = res_data.get('candidates', [{}])[0]
        parts = candidate.get('content', {}).get('parts', [])
        
        text_chunks = [p['text'] for p in parts if isinstance(p, dict) and 'text' in p and not p.get('thought', False)]
        if not text_chunks:
            text_chunks = [p.get('text', '') for p in parts if isinstance(p, dict) and 'text' in p]
            
        full_text = "\n".join(text_chunks).strip()
        if not full_text:
            raise ValueError(f"Gemini החזיר פלט ריק")
            
        return extract_json(full_text)

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
    print(f"📋 מודלי Groq: {available_models}", flush=True)
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    safe_messages = [{"role": "system", "content": system_instruction}]
    
    # הבטחת יציבות ב-Groq: לוקח רק את 3 ההודעות האחרונות בשרשור ומקצץ ל-4000 תווים!
    recent_contents = contents[-3:] if len(contents) > 3 else contents
    for c in recent_contents:
        role = "assistant" if c["role"] == "model" else "user"
        text = c["parts"][0]["text"]
        if len(text) > 4000:
            text = text[:4000] + "\n\n...[הטקסט קוצץ עקב מגבלת הזיכרון של מודל הגיבוי]..."
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
                print(f"🔄 מנסה מודל Groq: {model}...", flush=True)
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
                    return model, extract_json(raw_text)
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
                print(f"🔄 מנסה {model_name} (מפתח #{i + 1})...", flush=True)
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
        conversation[-1]["parts"][0]["text"] += "\n\n[CRITICAL REMINDER: You MUST output ONLY a valid JSON. You MUST escape all double quotes (\\\") and newlines (\\n) inside the code content field!]"

    system_instruction = f"""
    You are an autonomous AI software engineer operating inside this GitHub repository (Default branch: {default_branch}).
    You communicate naturally in Hebrew.
    
    1. ALWAYS return a perfectly VALID JSON object.
    2. NEVER return raw text or YAML. If writing YAML, put it inside the "chat_response" field.
    3. If committing code, ONLY include the files you are modifying or creating in `files_to_update`. 
    4. CRITICAL: When putting code in the "content" field, you MUST correctly escape all double quotes (\\") and use \\n for newlines so the JSON does not break!
    
    Action Types:
    - "chat": For answering questions, explanations, or snippets.
    - "commit": ONLY when asked to write code/modify files in the repo.
    """

    try:
        provider_used, response_data, debug_log = generate_with_smart_retry(gemini_keys, groq_key, conversation, system_instruction)
    except Exception as e:
        error_details = f"\n\n<details><summary>🛠️ לחץ כאן לפירוט השגיאות</summary>\n\n```text\n{str(e)}\n```\n</details>"
        post_issue_comment(repo_name, issue_number, github_token, f"⚠️ המערכת בעומס. נסה שוב בעוד מספר דקות.{error_details}")
        return

    if not isinstance(response_data, dict):
        response_data = {"action": "chat", "chat_response": str(response_data)}
        
    action = response_data.get("action", "chat")
    chat_reply = response_data.get("chat_response", "הפעולה בוצעה.")

    if action == "commit":
        try:
            branch = response_data.get("branch_name", f"ai-patch-issue-{issue_number}")
            subprocess.run(["git", "checkout", "-B", branch], check=True)
            
            valid_files = [f for f in response_data.get("files_to_update", []) if not f["path"].startswith(".github/workflows/")]
            
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
