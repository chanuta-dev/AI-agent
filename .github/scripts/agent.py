import os
import json
import subprocess
import urllib.request
import urllib.error
import time
from github import Github, Auth

MODEL_NAME = "gemini-3.7-flash"

def get_repo_files_and_content():
    """סורק את כל קבצי הפרויקט וטוען את תוכנם לקונטקסט."""
    repo_files = {}
    file_list = []
    
    for root, dirs, files in os.walk("."):
        parts = os.path.normpath(root).split(os.sep)
        if ".git" in parts or "__pycache__" in parts:
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

def call_gemini_api(api_key, contents, system_instruction):
    """קריאה ישירה ל-Gemini 3.7."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={api_key}"
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
    
    with urllib.request.urlopen(req, timeout=35) as response:
        res_data = json.loads(response.read().decode())
        raw_text = res_data['candidates'][0]['content']['parts'][0]['text']
        return json.loads(raw_text)

def call_groq_api(groq_key, contents, system_instruction):
    """קריאת גיבוי ל-Groq."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    messages = [{"role": "system", "content": system_instruction}]
    for c in contents:
        role = "assistant" if c["role"] == "model" else "user"
        messages.append({"role": role, "content": c["parts"][0]["text"]})
        
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.2
    }
    data = json.dumps(payload).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {groq_key.strip()}',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    req = urllib.request.Request(url, data=data, headers=headers)
    
    with urllib.request.urlopen(req, timeout=30) as response:
        res_data = json.loads(response.read().decode())
        raw_text = res_data['choices'][0]['message']['content']
        return json.loads(raw_text)

def generate_with_smart_retry(gemini_keys, groq_key, contents, system_instruction):
    """מנגנון Failover שמחליף מפתחות וספקים ברגע של 429 או 503."""
    last_error = None

    for i, key in enumerate(gemini_keys):
        if not key:
            continue
        try:
            print(f"🔄 מנסה Gemini 3.7 (מפתח #{i + 1})...")
            result = call_gemini_api(key, contents, system_instruction)
            print(f"✅ הצלחה עם Gemini 3.7 (מפתח #{i + 1})")
            return "Gemini 3.7", result
        except urllib.error.HTTPError as e:
            status = e.code
            err_body = e.read().decode()
            print(f"⚠️ מפתח #{i + 1} נכשל עם שגיאה {status}: {err_body}")
            last_error = f"Gemini Error {status}"
            continue
        except Exception as e:
            last_error = str(e)
            time.sleep(1)

    if groq_key:
        try:
            print("⚡ מפתחות גוגל עמוסים, מפעיל גיבוי Groq...")
            result = call_groq_api(groq_key, contents, system_instruction)
            print("✅ הצלחה עם Groq!")
            return "Groq Backup", result
        except Exception as e:
            print(f"⚠️ שגיאה ב-Groq: {e}")
            last_error = f"Groq Error: {e}"

    raise RuntimeError(f"כל הניסיונות נכשלו: {last_error}")

def main():
    # 1. איסוף משתנים
    github_token = os.environ["GITHUB_TOKEN"]
    primary_gemini_key = os.environ.get("GEMINI_API_KEY", "")
    backup_gemini_key = os.environ.get("GEMINI_API_KEY_2", "")
    groq_key = os.environ.get("GROQ_API_KEY", "")

    gemini_keys = [k for k in [primary_gemini_key, backup_gemini_key] if k]

    repo_name = os.environ["REPO_NAME"]
    issue_number = int(os.environ["ISSUE_NUMBER"])

    auth = Auth.Token(github_token)
    gh = Github(auth=auth)
    repo = gh.get_repo(repo_name)
    issue = repo.get_issue(issue_number)

    # 2. קונטקסט
    file_list, repo_files_content = get_repo_files_and_content()
    context_prefix = (
        f"[Repository: {repo_name}]\n"
        f"[Existing Files: {json.dumps(file_list)}]\n"
        f"[Files Content:\n{json.dumps(repo_files_content, ensure_ascii=False, indent=2)}]\n\n"
    )
    
    initial_user_msg = context_prefix + f"Issue #{issue.number} Title: {issue.title}\n\n{issue.body or ''}"
    conversation = [{"role": "user", "parts": [{"text": initial_user_msg}]}]
    
    for comment in issue.get_comments():
        role = "model" if comment.user.login.endswith("[bot]") or comment.user.login == "github-actions[bot]" else "user"
        conversation.append({"role": role, "parts": [{"text": comment.body or ""}]})

    # 3. הנחיית מערכת משודרגת
    system_instruction = """
    You are an autonomous AI software engineer operating inside this GitHub repository.
    You communicate naturally, clearly, and helpfully in Hebrew.
    
    CRITICAL WORKFLOW RULES:
    1. ALWAYS return a VALID JSON object (no markdown code blocks around the JSON).
    2. GitHub security prevents automated bots from pushing commits to `.github/workflows/`.
       - NEVER include files starting with `.github/workflows/` in `files_to_update`.
       - If you recommend or design optimizations/changes for `.github/workflows/` (e.g. `gemini_agent.yml`), explain them clearly inside `chat_response` and provide the exact YAML code block for the user to copy-paste manually!
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

    # 4. הרצה
    try:
        provider_used, response_data = generate_with_smart_retry(gemini_keys, groq_key, conversation, system_instruction)
    except Exception as e:
        issue.create_comment(f"⚠️ המערכת בעומס זמני מול ה-API: {str(e)}")
        return

    action = response_data.get("action", "chat")
    chat_reply = response_data.get("chat_response", "אני כאן כדי לעזור!")

    # 5. ביצוע פעולות
    if action == "commit":
        try:
            branch = response_data.get("branch_name", f"ai-patch-issue-{issue_number}")
            subprocess.run(["git", "checkout", "-B", branch], check=True)
            
            # סינון בטיחותי - קבצים שמותר לדחוף
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
            subprocess.run(["git", "push", "origin", branch, "--force"], check=True)

            pr_title = f"🤖 AI Update: {response_data.get('commit_message')}"
            pr_body = f"Closes #{issue_number}\n\n{chat_reply}\n\n*Generated with {provider_used}*"
            
            try:
                pr = repo.create_pull(title=pr_title, body=pr_body, head=branch, base="main")
                pr_url = pr.html_url
            except Exception:
                pr_url = f"https://github.com/{repo_name}/tree/{branch}"

            summary = f"✨ **בוצע בהצלחה (באמצעות {provider_used})!**\n\n{chat_reply}\n\n🔗 **Pull Request מוכן:** {pr_url}"
            issue.create_comment(summary)

        except Exception as e:
            issue.create_comment(f"⚠️ חלה שגיאה בביצוע ה-Commit: {str(e)}")
    else:
        issue.create_comment(chat_reply)

if __name__ == "__main__":
    main()
