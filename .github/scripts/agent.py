import os
import json
import re
import subprocess
import urllib.request
import urllib.error
import time

# רשימת מודלי Gemini לפי סדר עדיפות
GEMINI_MODELS = [
    "gemini-3.7-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash"
]

# מודלים מהירים ב-Groq לגיבוי מיידי
GROQ_CANDIDATE_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant"
]

def github_api_request(url, token, data=None, method="GET"):
    """ביצוע קריאות ישירות ל-GitHub REST API ללא צורך בספריות חיצוניות."""
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "GitHub-Agent-Bot"
    }
    encoded_data = json.dumps(data).encode("utf-8") if data else None
    if encoded_data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())

def get_repo_files_and_content():
    """סורק את כל קבצי הפרויקט וטוען את תוכנם לקונטקסט."""
    repo_files = {}
    file_list = []
    
    for root, dirs, files in os.walk("."):
        parts = os.path.normpath(root).split(os.sep)
        if ".git" in parts or "__pycache__" in parts or ".github" in parts and "workflows" in parts:
            continue
            
        for f in files:
            filepath = os.path.normpath(os.path.join(root, f))
            if filepath.startswith(f".{os.sep}"):
                filepath = filepath[2:]
            file_list.append(filepath)
            
            try:
                if os.path.getsize(filepath) < 30000:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as file_handle:
                        repo_files[filepath] = file_handle.read()
            except Exception:
                pass
                
    return file_list, repo_files

def call_gemini_model(api_key, model_name, contents, system_instruction):
    """קריאה ל-Gemini לפי שם מודל ספציפי."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2
        }
    }
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    
    with urllib.request.urlopen(req, timeout=25) as response:
        res_data = json.loads(response.read().decode())
        raw_text = res_data['candidates'][0]['content']['parts'][0]['text']
        return json.loads(raw_text)

def call_groq_api(groq_key, contents, system_instruction):
    """קריאת גיבוי ל-Groq עם מעבר אוטומטי בין מודלים."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    messages = [{"role": "system", "content": system_instruction}]
    for c in contents:
        role = "assistant" if c["role"] == "model" else "user"
        messages.append({"role": role, "content": c["parts"][0]["text"]})
        
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {groq_key.strip()}',
        'User-Agent': 'GitHub-Autonomous-Agent/1.0'
    }

    last_groq_error = None
    for model in GROQ_CANDIDATE_MODELS:
        try:
            print(f"🔄 מנסה מודל Groq: {model}...")
            payload = {
                "model": model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": 0.2
            }
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(url, data=data, headers=headers)
            
            with urllib.request.urlopen(req, timeout=20) as response:
                res_data = json.loads(response.read().decode())
                raw_text = res_data['choices'][0]['message']['content']
                print(f"✅ הצלחה עם מודל Groq: {model}")
                return model, json.loads(raw_text)
        except Exception as e:
            print(f"⚠️ Groq ({model}) נכשל: {e}")
            last_groq_error = e

    raise RuntimeError(f"כל מודלי Groq נכשלו: {last_groq_error}")

def generate_with_smart_retry(gemini_keys, groq_key, contents, system_instruction):
    """מנגנון Failover רב-שכבתי להבטחת מהירות ושרידות מקסימלית."""
    last_error = None

    # 1. ניסיון סבב מודלים של Gemini בכל המפתחות
    for model_name in GEMINI_MODELS:
        for i, key in enumerate(gemini_keys):
            if not key:
                continue
            try:
                print(f"🔄 מנסה {model_name} (מפתח #{i + 1})...")
                result = call_gemini_model(key, model_name, contents, system_instruction)
                print(f"✅ הצלחה עם {model_name} (מפתח #{i + 1})")
                return model_name, result
            except urllib.error.HTTPError as e:
                status = e.code
                print(f"⚠️ {model_name} (מפתח #{i + 1}) נכשל: HTTP {status}")
                last_error = f"Gemini ({model_name}) HTTP {status}"
                if status in (429, 503, 500):
                    continue
            except Exception as e:
                print(f"⚠️ {model_name} (מפתח #{i + 1}) נכשל: {e}")
                last_error = str(e)

    # 2. גיבוי Groq מהיר אם Gemini אינו זמין
    if groq_key:
        try:
            print("⚡ Gemini בעומס, עובר לגיבוי Groq...")
            used_model, result = call_groq_api(groq_key, contents, system_instruction)
            return f"Groq ({used_model})", result
        except Exception as e:
            print(f"⚠️ שגיאה ב-Groq: {e}")
            last_error = f"Groq Error: {e}"

    raise RuntimeError(f"כל הניסיונות נכשלו: {last_error}")

def post_issue_comment(repo_name, issue_number, token, body):
    """שליחת תגובה ל-Issue ישירות דרך ה-API."""
    url = f"https://api.github.com/repos/{repo_name}/issues/{issue_number}/comments"
    try:
        github_api_request(url, token, data={"body": body}, method="POST")
    except Exception as e:
        print(f"שגיאה בשליחת תגובה: {e}")

def main():
    github_token = os.environ["GITHUB_TOKEN"]
    primary_gemini_key = os.environ.get("GEMINI_API_KEY", "")
    backup_gemini_key = os.environ.get("GEMINI_API_KEY_2", "")
    groq_key = os.environ.get("GROQ_API_KEY", "")

    gemini_keys = [k for k in [primary_gemini_key, backup_gemini_key] if k]
    repo_name = os.environ["REPO_NAME"]
    issue_number = int(os.environ["ISSUE_NUMBER"])

    # טעינת נתוני Issue ותגובות
    issue_data = github_api_request(f"https://api.github.com/repos/{repo_name}/issues/{issue_number}", github_token)
    comments_data = github_api_request(f"https://api.github.com/repos/{repo_name}/issues/{issue_number}/comments", github_token)

    file_list, repo_files_content = get_repo_files_and_content()
    context_prefix = (
        f"[Repository: {repo_name}]\n"
        f"[Existing Files: {json.dumps(file_list)}]\n"
        f"[Files Content:\n{json.dumps(repo_files_content, ensure_ascii=False, indent=2)}]\n\n"
    )
    
    initial_user_msg = context_prefix + f"Issue #{issue_number} Title: {issue_data.get('title', '')}\n\n{issue_data.get('body') or ''}"
    conversation = [{"role": "user", "parts": [{"text": initial_user_msg}]}]
    
    for comment in comments_data:
        author = comment.get("user", {}).get("login", "")
        role = "model" if author.endswith("[bot]") or author == "github-actions[bot]" else "user"
        conversation.append({"role": role, "parts": [{"text": comment.get("body") or ""}]})

    system_instruction = """
    You are an autonomous AI software engineer operating inside this GitHub repository.
    You communicate naturally, clearly, and helpfully in Hebrew.
    
    CRITICAL WORKFLOW RULES:
    1. ALWAYS return a VALID JSON object (no markdown code blocks around the JSON).
    2. GitHub security prevents automated bots from pushing commits to `.github/workflows/`.
       - NEVER include files starting with `.github/workflows/` in `files_to_update`.
       - If you recommend optimizations for `.github/workflows/`, explain them in `chat_response` and provide the exact YAML snippet for the user.
       - You CAN modify `.github/scripts/agent.py` and ALL application files directly via `files_to_update`.
    
    IF CHATTING / EXPLAINING / BRAINSTORMING:
    {
      "action": "chat",
      "chat_response": "Your natural markdown response in Hebrew (including YAML suggestions if relevant)"
    }
    
    IF INSTRUCTED TO APPLY / COMMIT / EXECUTE:
    {
      "action": "commit",
      "chat_response": "Summary in Hebrew of the applied changes (and manual YAML instructions if needed)",
      "commit_message": "Clear Git commit message",
      "branch_name": "ai-feature-name",
      "files_to_update": [
        {
          "path": "path/to/file.ext",
          "content": "Full updated code/content"
        }
      ],
      "files_to_delete": []
    }
    """

    try:
        provider_used, response_data = generate_with_smart_retry(gemini_keys, groq_key, conversation, system_instruction)
    except Exception as e:
        post_issue_comment(repo_name, issue_number, github_token, f"⚠️ המערכת בעומס זמני מול ה-API: {str(e)}")
        return

    action = response_data.get("action", "chat")
    chat_reply = response_data.get("chat_response", "אני כאן כדי לעזור!")

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
            
            # אימות ודחיפה באמצעות Access Token ישירות
            remote_url = f"https://x-access-token:{github_token}@github.com/{repo_name}.git"
            subprocess.run(["git", "remote", "set-url", "origin", remote_url], check=True)
            subprocess.run(["git", "push", "origin", branch, "--force"], check=True)

            pr_title = f"🤖 AI Update: {response_data.get('commit_message')}"
            pr_body = f"Closes #{issue_number}\n\n{chat_reply}\n\n*Generated with {provider_used}*"
            
            pr_data = {
                "title": pr_title,
                "body": pr_body,
                "head": branch,
                "base": "main"
            }
            
            try:
                res = github_api_request(f"https://api.github.com/repos/{repo_name}/pulls", github_token, data=pr_data, method="POST")
                pr_url = res.get("html_url", f"https://github.com/{repo_name}/tree/{branch}")
            except Exception:
                pr_url = f"https://github.com/{repo_name}/tree/{branch}"

            summary = f"✨ **בוצע בהצלחה (באמצעות {provider_used})!**\n\n{chat_reply}\n\n🔗 **Pull Request מוכן:** {pr_url}"
            post_issue_comment(repo_name, issue_number, github_token, summary)

        except Exception as e:
            post_issue_comment(repo_name, issue_number, github_token, f"⚠️ חלה שגיאה בביצוע ה-Commit: {str(e)}")
    else:
        post_issue_comment(repo_name, issue_number, github_token, chat_reply)

if __name__ == "__main__":
    main()
