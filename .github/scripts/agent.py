import os
import json
import re
import subprocess
import urllib.request
import urllib.error
import time

MODEL_NAME = "gemini-3.8-flash"

def extract_json(raw_text):
    """מחלץ אובייקט JSON מתוך טקסט חופשי (כולל תמיכה ב-Markdown או טקסט נלווה)."""
    text = raw_text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        text = match.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
    return json.loads(text)

def github_api_request(url, token, data=None, method="GET"):
    """קריאה ישירה ל-GitHub REST API ללא ספריות כבדות."""
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

def get_repo_files_and_content():
    """סורק את כל קבצי הפרויקט וטוען את תוכנם לקונטקסט."""
    repo_files = {}
    file_list = []
    
    for root, dirs, files in os.walk("."):
        parts = os.path.normpath(root).split(os.sep)
        if ".git" in parts or "__pycache__" in parts or ".agent_core" in parts:
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

def get_available_groq_models(groq_key):
    """שליפת רשימת המודלים הזמינים בזמן אמת מתוך חשבון ה-Groq."""
    url = "https://api.groq.com/openai/v1/models"
    headers = {
        'Authorization': f'Bearer {groq_key.strip()}',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            models = [m["id"] for m in data.get("data", [])]
            
            # סינון מודלי סאונד או הגנות
            filtered = [
                m for m in models 
                if not any(bad in m.lower() for bad in ["whisper", "guard", "tts", "vision"])
            ]
            
            # עדיפות עליונה ל-compound שיש לו מגבלת טוקנים ענקית (30k-70k)
            priority_keywords = ["compound", "120b", "3.8", "3.6", "27b", "20b", "allam", "orpheus"]
            def sort_key(model_id):
                for idx, kw in enumerate(priority_keywords):
                    if kw in model_id.lower():
                        return idx
                return len(priority_keywords)
            
            filtered.sort(key=sort_key)
            return filtered if filtered else models
    except Exception as e:
        print(f"⚠️ שגיאה בשליפת מודלי Groq דינמית: {e}")
        return ["groq/compound", "openai/gpt-oss-120b", "qwen/qwen3.8-27b", "groq/compound-mini"]

def call_groq_api(groq_key, contents, system_instruction):
    """קריאת גיבוי למודלים הזמינים בחשבון ה-Groq."""
    available_models = get_available_groq_models(groq_key)
    print(f"📋 מודלי Groq זמינים לחשבון (לפי סדר עדיפות): {available_models}")
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    messages = [{"role": "system", "content": system_instruction}]
    for c in contents:
        role = "assistant" if c["role"] == "model" else "user"
        messages.append({"role": role, "content": c["parts"][0]["text"]})
        
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {groq_key.strip()}',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }

    last_err = None
    for model in available_models:
        try:
            print(f"🔄 מנסה מודל Groq: {model}...")
            # ללא response_format מאולץ כדי למנוע קריסות של 400
            payload = {
                "model": model,
                "messages": messages,
                "temperature": 0.2
            }
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(url, data=data, headers=headers)
            
            with urllib.request.urlopen(req, timeout=35) as response:
                res_data = json.loads(response.read().decode())
                raw_text = res_data['choices'][0]['message']['content']
                print(f"✅ הצלחה עם Groq ({model})!")
                return model, extract_json(raw_text)
        except Exception as e:
            print(f"⚠️ Groq ({model}) נכשל: {e}")
            last_err = e

    raise RuntimeError(f"כל מודלי Groq נכשלו: {last_err}")

def generate_with_smart_retry(gemini_keys, groq_key, contents, system_instruction):
    """מנגנון Failover מלא: מעבר סדרתי על כל מפתחות Gemini -> גיבוי ל-Groq."""
    last_error = None

    # 1. ניסיון סדרתי מול כל מפתחות Gemini שהוגדרו (עד 5 ומעלה)
    for i, key in enumerate(gemini_keys):
        try:
            print(f"🔄 מנסה Gemini (מפתח #{i + 1} מתוך {len(gemini_keys)})...")
            result = call_gemini_api(key, contents, system_instruction)
            print(f"✅ הצלחה עם Gemini (מפתח #{i + 1})")
            return f"Gemini 3.7 (מפתח #{i + 1})", result
        except urllib.error.HTTPError as e:
            status = e.code
            print(f"⚠️ מפתח Gemini #{i + 1} נכשל עם קוד שגיאה {status}")
            last_error = f"Gemini Key #{i + 1} HTTP {status}"
            continue  # עובר מיד למפתח הבא ללא המתנה מיותרת
        except Exception as e:
            print(f"⚠️ מפתח Gemini #{i + 1} נכשל: {e}")
            last_error = str(e)
            continue

    # 2. מעבר אוטומטי ל-Groq אם כל מפתחות Gemini מוצו
    if groq_key:
        try:
            print("⚡ כל מפתחות Gemini מוצו/עמוסים, מפעיל גיבוי Groq...")
            used_model, result = call_groq_api(groq_key, contents, system_instruction)
            return f"Groq ({used_model})", result
        except Exception as e:
            print(f"⚠️ שגיאה ב-Groq: {e}")
            last_error = f"Groq Error: {e}"

    raise RuntimeError(f"כל הניסיונות נכשלו: {last_error}")

def post_issue_comment(repo_name, issue_number, token, body):
    """שליחת תגובה ל-Issue."""
    url = f"https://api.github.com/repos/{repo_name}/issues/{issue_number}/comments"
    try:
        github_api_request(url, token, data={"body": body}, method="POST")
    except Exception as e:
        print(f"שגיאה בשליחת תגובה: {e}")

def main():
    github_token = os.environ["GITHUB_TOKEN"]
    groq_key = os.environ.get("GROQ_API_KEY", "")

    # איסוף דינמי של כל מפתחות Gemini (תומך ב-GEMINI_API_KEY ועד GEMINI_API_KEY_5 ומעלה)
    gemini_keys = []
    # מפתח ראשי
    if os.environ.get("GEMINI_API_KEY"):
        gemini_keys.append(os.environ["GEMINI_API_KEY"].strip())
    # מפתחות גיבוי 2 עד 10
    for i in range(2, 11):
        k = os.environ.get(f"GEMINI_API_KEY_{i}", "").strip()
        if k and k not in gemini_keys:
            gemini_keys.append(k)

    print(f"🔑 זוהו {len(gemini_keys)} מפתחות Gemini פעילים.")

    repo_name = os.environ["REPO_NAME"]
    issue_number = int(os.environ["ISSUE_NUMBER"])

    # טעינת נתוני ה-Issue
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
    2. NEVER include `.github/workflows/` files in `files_to_update`. If suggesting workflow changes, explain them in `chat_response` and provide the exact YAML snippet for the user.
    3. You CAN modify `.github/scripts/agent.py` and ALL application files directly via `files_to_update`.
    
    IF CHATTING / EXPLAINING / BRAINSTORMING:
    {
      "action": "chat",
      "chat_response": "Your natural markdown response in Hebrew"
    }
    
    IF INSTRUCTED TO APPLY / COMMIT / EXECUTE:
    {
      "action": "commit",
      "chat_response": "Summary in Hebrew of the applied changes",
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
        post_issue_comment(repo_name, issue_number, github_token, f"⚠️ המערכת בעומס זמני מול כל ה-APIs: {str(e)}")
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
