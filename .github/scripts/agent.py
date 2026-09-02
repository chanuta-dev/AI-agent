import os
import json
import subprocess
import urllib.request
import urllib.error
import time
from github import Github, Auth

MODEL_NAME = "gemini-3.7-flash"

def get_repo_files_and_content():
    """סורק את כל קבצי הפרויקט (כולל .github) וטוען את תוכנם לקונטקסט של ה-AI."""
    repo_files = {}
    file_list = []
    
    for root, dirs, files in os.walk("."):
        parts = os.path.normpath(root).split(os.sep)
        # מתעלם מתיקיית .git האמיתית אבל לא מ-.github!
        if ".git" in parts or "__pycache__" in parts:
            continue
            
        for f in files:
            filepath = os.path.normpath(os.path.join(root, f))
            # ביטול ה-./ מתחילת הנתיב אם יש
            if filepath.startswith(f".{os.sep}"):
                filepath = filepath[2:]
            file_list.append(filepath)
            
            # קריאת תוכן הקבצים (עד 30KB לקובץ כדי לא להעמיס)
            try:
                if os.path.getsize(filepath) < 30000:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as file_handle:
                        repo_files[filepath] = file_handle.read()
            except Exception:
                pass
                
    return file_list, repo_files

def generate_content_rest(api_key, contents, system_instruction):
    """קריאה ישירה ל-API של Gemini 3.7."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={api_key}"
    
    payload = {
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        },
        "contents": contents,
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2
        }
    }
    
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    
    with urllib.request.urlopen(req) as response:
        res_data = json.loads(response.read().decode())
        raw_text = res_data['candidates'][0]['content']['parts'][0]['text']
        return json.loads(raw_text)

def main():
    # 1. התחברות
    github_token = os.environ["GITHUB_TOKEN"]
    gemini_api_key = os.environ["GEMINI_API_KEY"]
    repo_name = os.environ["REPO_NAME"]
    issue_number = int(os.environ["ISSUE_NUMBER"])

    auth = Auth.Token(github_token)
    gh = Github(auth=auth)
    repo = gh.get_repo(repo_name)
    issue = repo.get_issue(issue_number)

    # 2. איסוף כל קבצי הפרויקט והתוכן שלהם
    file_list, repo_files_content = get_repo_files_and_content()
    
    context_prefix = (
        f"[Repository Name: {repo_name}]\n"
        f"[Existing Files Structure: {json.dumps(file_list)}]\n"
        f"[Files Content in Project:\n{json.dumps(repo_files_content, ensure_ascii=False, indent=2)}]\n\n"
    )
    
    initial_user_msg = context_prefix + f"Issue #{issue.number} Title: {issue.title}\n\n{issue.body or ''}"
    
    conversation = [
        {"role": "user", "parts": [{"text": initial_user_msg}]}
    ]
    
    for comment in issue.get_comments():
        role = "model" if comment.user.login.endswith("[bot]") or comment.user.login == "github-actions[bot]" else "user"
        conversation.append({"role": role, "parts": [{"text": comment.body or ""}]})

    # 3. הנחיית מערכת
    system_instruction = """
    You are Gemini 3.7, an autonomous AI software engineer operating directly inside this GitHub repository.
    You have full visibility of all repository files and their contents provided in the context.
    
    COMMUNICATION:
    - Respond naturally, politely, and professionally in Hebrew.
    - ALWAYS return a strict JSON object with no markdown wrappers.
    
    IF CHATTING / EXPLAINING / BRAINSTORMING:
    {
      "action": "chat",
      "chat_response": "Your markdown answer in Hebrew"
    }
    
    IF INSTRUCTED TO APPLY / COMMIT / EXECUTE:
    {
      "action": "commit",
      "chat_response": "Summary in Hebrew of the changes made",
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

    # 4. ביצוע הקריאה עם מנגנון Retry חכם נגד עומס (503)
    response_data = None
    max_retries = 4
    for attempt in range(max_retries):
        try:
            response_data = generate_content_rest(gemini_api_key, conversation, system_instruction)
            break
        except Exception as e:
            if attempt == max_retries - 1:
                issue.create_comment(f"⚠️ חלה שגיאת תקשורת מול המודל ({MODEL_NAME}): {str(e)}")
                return
            # המתנה שגדלה בכל ניסיון (2s, 4s, 6s) להתאוששות מעומס רגעי
            time.sleep((attempt + 1) * 2)

    # 5. עיבוד התשובה
    action = response_data.get("action", "chat")
    chat_reply = response_data.get("chat_response", "אני כאן כדי לעזור!")

    if action == "commit":
        try:
            branch = response_data.get("branch_name", f"ai-patch-issue-{issue_number}")
            
            subprocess.run(["git", "checkout", "-B", branch], check=True)
            
            for item in response_data.get("files_to_update", []):
                filepath = item["path"]
                if os.path.dirname(filepath):
                    os.makedirs(os.path.dirname(filepath), exist_ok=True)
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

            summary = f"✨ **בוצע בהצלחה (באמצעות {MODEL_NAME})!**\n\n{chat_reply}\n\n🔗 **Pull Request מוכן:** {pr_url}"
            issue.create_comment(summary)

        except Exception as e:
            issue.create_comment(f"⚠️ חלה שגיאה בביצוע ה-Commit: {str(e)}")
    else:
        issue.create_comment(chat_reply)

if __name__ == "__main__":
    main()
