import os
import json
import subprocess
import urllib.request
import urllib.error
from github import Github, Auth

def get_repo_structure():
    """סורק את מבנה הפרויקט הקיים."""
    tree = []
    for root, dirs, files in os.walk("."):
        if ".git" in root or "__pycache__" in root:
            continue
        for f in files:
            tree.append(os.path.normpath(os.path.join(root, f)))
    return tree

def get_best_model(api_key):
    """שולף דינמית את מודל ה-Flash העדכני ביותר (בדיוק כמו בפרויקט ה-JS שלך)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
            # מסנן רק מודלים מסוג Flash שתומכים בטקסט רגיל (ללא Live או אקספרימנטליים)
            flash_models = [
                m['name'].replace('models/', '') for m in res_data.get('models', [])
                if 'flash' in m['name'].lower()
                and 'exp' not in m['name'].lower()
                and 'live' not in m['name'].lower()
                and 'generateContent' in m.get('supportedGenerationMethods', [])
            ]
            if flash_models:
                chosen = flash_models[-1]
                print(f"🎯 Dynamic Model Selected: {chosen}")
                return chosen
    except Exception as e:
        print(f"⚠️ Could not fetch models dynamically: {e}")
    
    # ברירת מחדל אמינה ויציבה למקרה שאין אינטרנט או שהחיפוש נכשל
    return "gemini-3.7-flash"

def generate_content_rest(api_key, model_name, contents, system_instruction):
    """קריאה ישירה ונקייה ל-API של גוגל (המקבילה המדויקת ל-fetch שלך)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    
    payload = {
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        },
        "contents": contents,
        "generationConfig": {
            "responseMimeType": "application/json", # מכריח קבלת JSON טהור!
            "temperature": 0.2
        }
    }
    
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
            raw_text = res_data['candidates'][0]['content']['parts'][0]['text']
            return json.loads(raw_text) # ממיר את הטקסט למילון פייתון
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        raise Exception(f"API Error {e.code}: {error_body}")

def main():
    # 1. התחברות ל-GitHub
    github_token = os.environ["GITHUB_TOKEN"]
    gemini_api_key = os.environ["GEMINI_API_KEY"]
    repo_name = os.environ["REPO_NAME"]
    issue_number = int(os.environ["ISSUE_NUMBER"])

    auth = Auth.Token(github_token)
    gh = Github(auth=auth)
    repo = gh.get_repo(repo_name)
    issue = repo.get_issue(issue_number)

    # 2. בחירת המודל
    selected_model = get_best_model(gemini_api_key)

    # 3. בניית היסטוריית השיחה מה-Issue במבנה של REST API
    files_list = get_repo_structure()
    context_prefix = f"[System Context: Existing project files: {json.dumps(files_list)}]\n\n"
    
    initial_user_msg = context_prefix + f"Issue #{issue.number} Title: {issue.title}\n\n{issue.body or ''}"
    
    conversation = [
        {"role": "user", "parts": [{"text": initial_user_msg}]}
    ]
    
    for comment in issue.get_comments():
        role = "model" if comment.user.login.endswith("[bot]") or comment.user.login == "github-actions[bot]" else "user"
        conversation.append({"role": role, "parts": [{"text": comment.body or ""}]})

    # 4. הנחיית מערכת שתמיד דורשת JSON (אבל המשתמש לא יראה אותו)
    system_instruction = """
    You are an autonomous software engineer operating inside a GitHub repository.
    You communicate naturally in Hebrew.
    
    CRITICAL: You MUST ALWAYS respond with a VALID JSON object. Do not wrap it in markdown.
    
    IF THE USER IS JUST CHATTING, ASKING QUESTIONS, OR PLANNING:
    {
      "action": "chat",
      "chat_response": "Your natural, friendly markdown response in Hebrew"
    }
    
    IF THE USER EXPLICITLY ASKS TO APPLY, EXECUTE OR COMMIT CHANGES (e.g., 'תיישם', 'בצע'):
    {
      "action": "commit",
      "chat_response": "Friendly summary of what you did",
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

    # 5. ביצוע הקריאה (REST API)
    try:
        response_data = generate_content_rest(gemini_api_key, selected_model, conversation, system_instruction)
    except Exception as e:
        issue.create_comment(f"⚠️ חלה שגיאת תקשורת מול המודל ({selected_model}): {str(e)}")
        return

    action = response_data.get("action", "chat")
    chat_reply = response_data.get("chat_response", "אני כאן כדי לעזור!")

    # 6. טיפול בפעולה
    if action == "commit":
        try:
            branch = response_data.get("branch_name", f"ai-patch-issue-{issue_number}")
            
            subprocess.run(["git", "checkout", "-B", branch], check=True)
            
            for item in response_data.get("files_to_update", []):
                filepath = item["path"]
                os.makedirs(os.path.dirname(filepath), exist_ok=True) if os.path.dirname(filepath) else None
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(item["content"])
                subprocess.run(["git", "add", filepath], check=True)

            for del_path in response_data.get("files_to_delete", []):
                if os.path.exists(del_path):
                    os.remove(del_path)
                    subprocess.run(["git", "rm", del_path], check=True)

            subprocess.run(["git", "config", "--global", "user.name", "Gemini AI Agent"], check=True)
            subprocess.run(["git", "config", "--global", "user.email", "gemini-bot@github.com"], check=True)
            subprocess.run(["git", "commit", "-m", response_data.get("commit_message", "AI auto-update")], check=True)
            subprocess.run(["git", "push", "origin", branch, "--force"], check=True)

            pr_title = f"🤖 AI Update: {response_data.get('commit_message')}"
            pr_body = f"Closes #{issue_number}\n\n{chat_reply}"
            
            try:
                pr = repo.create_pull(title=pr_title, body=pr_body, head=branch, base="main")
                pr_url = pr.html_url
            except Exception:
                pr_url = f"https://github.com/{repo_name}/tree/{branch}"

            summary = f"✨ **בוצע בהצלחה (באמצעות {selected_model})!**\n\n{chat_reply}\n\n🔗 **Pull Request מוכן:** {pr_url}"
            issue.create_comment(summary)

        except Exception as e:
            issue.create_comment(f"⚠️ חלה שגיאה בביצוע ה-Commit: {str(e)}")
    else:
        # בשיחה רגילה - המשתמש מקבל רק את הטקסט!
        issue.create_comment(chat_reply)

if __name__ == "__main__":
    main()
