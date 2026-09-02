import os
import json
import subprocess
from google import genai
from google.genai import types
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

def get_best_available_model(client):
    """שולף דינמית את מודל ה-Flash העדכני ביותר שפעיל בחשבון."""
    try:
        models = list(client.models.list())
        flash_models = [
            m.name.replace("models/", "")
            for m in models
            if "flash" in m.name.lower() and "exp" not in m.name.lower()
        ]
        if flash_models:
            return flash_models[-1]
    except Exception:
        pass
    return "gemini-3.7-flash"

# --- הכלי של Gemini לעדכון קבצים ב-GitHub ---
def apply_changes(commit_message: str, branch_name: str, files_to_update: list[dict], files_to_delete: list[str] = []) -> str:
    """
    Applies code changes, writes or deletes files, and pushes to a Git branch.
    
    Args:
        commit_message: Clear description of the changes made.
        branch_name: The branch name for this feature/fix.
        files_to_update: List of dicts where each item has 'path' and 'content'.
        files_to_delete: List of file paths to remove.
    """
    # יצירת Branch
    subprocess.run(["git", "checkout", "-B", branch_name], check=True)
    
    # כתיבת קבצים מעודכנים
    for item in files_to_update:
        filepath = item["path"]
        os.makedirs(os.path.dirname(filepath), exist_ok=True) if os.path.dirname(filepath) else None
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(item["content"])
        subprocess.run(["git", "add", filepath], check=True)

    # מחיקת קבצים
    for del_path in files_to_delete:
        if os.path.exists(del_path):
            os.remove(del_path)
            subprocess.run(["git", "rm", del_path], check=True)

    # ביצוע Commit ודחיפה
    subprocess.run(["git", "config", "--global", "user.name", "Gemini Bot"], check=True)
    subprocess.run(["git", "config", "--global", "user.email", "gemini-bot@github.com"], check=True)
    subprocess.run(["git", "commit", "-m", commit_message], check=True)
    subprocess.run(["git", "push", "origin", branch_name, "--force"], check=True)

    return f"SUCCESS: pushed to {branch_name}"

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
    client = genai.Client(api_key=gemini_api_key)

    selected_model = get_best_available_model(client)

    # 2. בניית היסטוריית השיחה מה-Issue
    files_list = get_repo_structure()
    context_prefix = f"[System Context: Existing project files: {json.dumps(files_list)}]\n\n"
    
    initial_user_msg = context_prefix + f"Issue #{issue.number} Title: {issue.title}\n\n{issue.body or ''}"
    conversation = [
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=initial_user_msg)]
        )
    ]
    
    for comment in issue.get_comments():
        role = "model" if comment.user.login.endswith("[bot]") or comment.user.login == "github-actions[bot]" else "user"
        conversation.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=comment.body or "")]
            )
        )

    # 3. הנחיות מערכת
    system_instruction = """
    You are Gemini, an autonomous software engineer collaborating with the user directly inside a GitHub repository.
    
    BEHAVIOR:
    - You communicate in fluent, natural markdown text (in Hebrew as requested).
    - You brainstorm, explain concepts, ask questions, and share code snippets naturally.
    - ONLY when the user asks to execute/apply the changes (e.g., 'תיישם', 'בצע', 'apply', 'commit'), call the `apply_changes` function with the complete file contents and a clear commit message.
    """

    # 4. קריאה ל-Gemini עם הכלי apply_changes
    try:
        response = client.models.generate_content(
            model=selected_model,
            contents=conversation,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=[apply_changes],
                temperature=0.3
            )
        )
    except Exception as e:
        issue.create_comment(f"⚠️ חלה שגיאה בתקשורת עם המודל ({selected_model}): {str(e)}")
        return

    # 5. בדיקה האם Gemini החליט להפעיל את הכלי (לבצע Commit)
    if response.function_calls:
        for call in response.function_calls:
            if call.name == "apply_changes":
                args = call.args
                commit_msg = args.get("commit_message", "AI code update")
                branch = args.get("branch_name", f"ai-patch-issue-{issue_number}")
                files_up = args.get("files_to_update", [])
                files_del = args.get("files_to_delete", [])

                try:
                    # הפעלת הפונקציה
                    apply_changes(commit_msg, branch, files_up, files_del)

                    # פתיחת PR
                    pr_title = f"🤖 Gemini: {commit_msg}"
                    pr_body = f"Closes #{issue_number}\n\nApplied changes automatically based on issue conversation."
                    try:
                        pr = repo.create_pull(title=pr_title, body=pr_body, head=branch, base="main")
                        pr_url = pr.html_url
                    except Exception:
                        pr_url = f"https://github.com/{repo_name}/tree/{branch}"

                    explanation = response.text if response.text else "השינויים יושמו בהצלחה בקוד הפרויקט."
                    issue.create_comment(f"✨ **השינויים יושמו בהצלחה!**\n\n{explanation}\n\n🔗 **Pull Request מוכן:** {pr_url}")
                    return
                except Exception as e:
                    issue.create_comment(f"⚠️ חלה שגיאה בעת ביצוע ה-Commit: {str(e)}")
                    return

    # 6. אם זו הייתה שיחת צ'אט רגילה - פרסם את התגובה הטבעית כרגיל
    if response.text:
        issue.create_comment(response.text)
    else:
        issue.create_comment("קיבלתי את ההודעה, איך תרצה להתקדם?")

if __name__ == "__main__":
    main()
