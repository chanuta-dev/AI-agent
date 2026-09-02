import os
import json
import subprocess
import urllib.request
import urllib.error
import time
from github import Github, Auth

# נעילה מוחלטת על המודל החכם והעדכני ביותר!
MODEL_NAME = "gemini-3.7-flash"

def get_repo_structure():
    """סורק את מבנה הפרויקט הקיים."""
    tree = []
    for root, dirs, files in os.walk("."):
        if ".git" in root or "__pycache__" in root:
            continue
        for f in files:
            tree.append(os.path.normpath(os.path.join(root, f)))
    return tree

def generate_content_rest(api_key, contents, system_instruction):
    """קריאה ישירה ונקייה ל-API של גוגל (המקבילה המדויקת ל-fetch מ-JS)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={api_key}"
    
    payload = {
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        },
        "contents": contents,
        "generationConfig": {
            "responseMimeType": "application/json", # מבטיח תמיד קבלת JSON טהור
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

    # 2. בניית היסטוריית השיחה מה-Issue
    files_list = get_repo_structure()
    context_prefix = f"[System Context: Existing project files: {json.dumps(files_list)}]\n\n"
    
    initial_user_msg = context_prefix + f"Issue #{issue.number} Title: {issue.title}\n\n{issue.body or ''}"
    
    conversation = [
        {"role": "user", "parts": [{"text": initial_user_msg}]}
    ]
    
    for comment in issue.get_comments():
        role = "model" if comment.user.login.endswith("[bot]") or comment.user.login == "github-actions[bot]" else "user"
        conversation.append({"role": role, "parts": [{"text": comment.body or ""}]})

    # 3. הנחיית מערכת שתמיד דורשת JSON 
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

    # 4. ביצוע הקריאה עם מנגנון Retry במקרה של עומס רגעי
    response_data = None
    for attempt in range(3):
        try:
            response_data = generate_content_rest(gemini_api_key, conversation, system_instruction)
            break
        except Exception as e:
            if attempt == 2:
                issue.create_comment(f"⚠️ חלה שגיאת תקשורת מול המודל ({MODEL_NAME}): {str(e)}")
                return
            time.sleep(2.5) # המתנה של 2.5 שניות לפני ניסיון חוזר

    # 5. חילוץ התשובה
    action = response_data.get("action", "chat")
    chat_reply = response_data.get("chat_response", "אני כאן כדי לעזור!")

    # 6. טיפול בפעולה (Push לקוד או שיחה)
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
        # בשיחה רגילה - המשתמש מקבל רק את הטקסט הטבעי!
        issue.create_comment(chat_reply)

if __name__ == "__main__":
    main()
