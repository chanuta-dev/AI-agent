import os
import json
import subprocess
from google import genai
from google.genai import types
from github import Github, Auth

def get_repo_structure():
    """סורק את מבנה הפרויקט הקיים כדי ש-Gemini יכיר את כל הקבצים."""
    tree = []
    for root, dirs, files in os.walk("."):
        if ".git" in root or "__pycache__" in root:
            continue
        for f in files:
            tree.append(os.path.normpath(os.path.join(root, f)))
    return tree

def main():
    # 1. אתחול גישה ל-GitHub ול-Gemini
    github_token = os.environ["GITHUB_TOKEN"]
    gemini_api_key = os.environ["GEMINI_API_KEY"]
    repo_name = os.environ["REPO_NAME"]
    issue_number = int(os.environ["ISSUE_NUMBER"])

    auth = Auth.Token(github_token)
    gh = Github(auth=auth)
    repo = gh.get_repo(repo_name)
    issue = repo.get_issue(issue_number)
    client = genai.Client(api_key=gemini_api_key)

    # 2. בניית היסטוריית השיחה מתוך ה-Issue באמצעות types.Content
    conversation = []
    
    # מידע מקדים ל-AI על מצב הקבצים הנוכחי
    files_list = get_repo_structure()
    context_prefix = f"[System Context: Existing project files: {json.dumps(files_list)}]\n\n"
    
    first_message = context_prefix + f"Issue #{issue.number} Title: {issue.title}\n\n{issue.body or ''}"
    conversation.append(
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=first_message)]
        )
    )
    
    for comment in issue.get_comments():
        # זיהוי אם התגובה היא של המשתמש או של הבוט
        role = "model" if comment.user.login.endswith("[bot]") or comment.user.login == "github-actions[bot]" else "user"
        conversation.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=comment.body or "")]
            )
        )

    # 3. הנחיית מערכת (System Instruction)
    system_instruction = """
    You are Gemini, an autonomous and self-upgrading software engineer operating directly inside a GitHub repository.
    
    CAPABILITIES:
    1. Chat, consult, and brainstorm with the user.
    2. Write, update, or delete ANY file in this repository (including Kotlin/Android, Python, Web, Workflows, and even your own code in agent.py!).
    
    PROTOCOL:
    - If you and the user are just chatting/planning: Respond naturally in markdown text.
    - If the user instructs to APPLY / COMMIT / EXECUTE (or says words like 'תיישם', 'בצע', 'סגור על זה', 'apply'):
      You must respond ONLY with a raw JSON object formatted like this (do NOT wrap in markdown code blocks like ```json):
      {
        "action": "commit",
        "chat_response": "Friendly summary in Hebrew of what you did",
        "commit_message": "Clear git commit message",
        "branch_name": "ai-feature-name",
        "files_to_update": [
          {
            "path": "path/to/file.ext",
            "content": "Full new content of file"
          }
        ],
        "files_to_delete": ["path/to/deleted_file.ext"]
      }
    """

    # 4. קריאה ל-Gemini
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=conversation,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.2
        )
    )

    resp_text = response.text.strip()

    # 5. ניקוי עטיפות Markdown אם המודל הוסיף בטעות
    clean_json_str = resp_text
    if clean_json_str.startswith("```json"):
        clean_json_str = clean_json_str[7:]
    elif clean_json_str.startswith("```"):
        clean_json_str = clean_json_str[3:]
    if clean_json_str.endswith("```"):
        clean_json_str = clean_json_str[:-3]
    clean_json_str = clean_json_str.strip()

    # 6. בדיקה האם המשתמש אישר ביצוע (Commit)
    if clean_json_str.startswith("{") and '"action": "commit"' in clean_json_str:
        try:
            data = json.loads(clean_json_str)
            branch = data.get("branch_name", f"ai-patch-issue-{issue_number}")
            
            # יצירת Branch
            subprocess.run(["git", "checkout", "-B", branch], check=True)
            
            # כתיבה ועדכון קבצים
            for item in data.get("files_to_update", []):
                filepath = item["path"]
                os.makedirs(os.path.dirname(filepath), exist_ok=True) if os.path.dirname(filepath) else None
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(item["content"])
                subprocess.run(["git", "add", filepath], check=True)

            # מחיקת קבצים
            for del_path in data.get("files_to_delete", []):
                if os.path.exists(del_path):
                    os.remove(del_path)
                    subprocess.run(["git", "rm", del_path], check=True)

            # ביצוע Commit ודחיפה
            subprocess.run(["git", "config", "--global", "user.name", "Gemini Bot"], check=True)
            subprocess.run(["git", "config", "--global", "user.email", "gemini-bot@github.com"], check=True)
            subprocess.run(["git", "commit", "-m", data.get("commit_message", "AI auto-update")], check=True)
            subprocess.run(["git", "push", "origin", branch, "--force"], check=True)

            # פתיחת Pull Request
            pr_title = f"🤖 Gemini: {data.get('commit_message')}"
            pr_body = f"Closes #{issue_number}\n\n{data.get('chat_response')}"
            
            try:
                pr = repo.create_pull(title=pr_title, body=pr_body, head=branch, base="main")
                pr_url = pr.html_url
            except Exception:
                pr_url = f"https://github.com/{repo_name}/tree/{branch}"

            summary_msg = f"✨ **השינויים יושמו בהצלחה!**\n\n{data.get('chat_response')}\n\n🔗 **Pull Request מוכן:** {pr_url}"
            issue.create_comment(summary_msg)
            return

        except Exception as e:
            issue.create_comment(f"⚠️ חלה שגיאה בביצוע הקומיט: {str(e)}\n\nטקסט מהמודל:\n{resp_text}")
            return

    # תגובת צ'אט רגילה
    issue.create_comment(resp_text)

if __name__ == "__main__":
    main()
