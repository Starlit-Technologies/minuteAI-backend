import requests


_LINEAR_API = "https://api.linear.app/graphql"


def _gql(access_token: str, query: str, variables: dict = None) -> dict:
    resp = requests.post(
        _LINEAR_API,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=10,
    )
    return resp.json()


def list_teams(access_token: str) -> list[dict]:
    """Return list of Linear teams the user has access to: [{"id": ..., "name": ...}]"""
    result = _gql(access_token, "{ teams { nodes { id name } } }")
    nodes = result.get("data", {}).get("teams", {}).get("nodes", [])
    return [{"id": n["id"], "name": n["name"]} for n in nodes]


def create_issues(access_token: str, settings: dict, job_id: str, minutes: dict, app_url: str) -> None:
    """Create Linear issues from action items in the meeting minutes."""
    action_items = minutes.get("actionItems", [])
    if not action_items:
        return

    team_id = settings.get("team_id")
    if not team_id:
        teams = list_teams(access_token)
        if not teams:
            return
        team_id = teams[0]["id"]

    title = minutes.get("title", "Meeting")
    results_url = f"{app_url}/results/{job_id}"
    description = f"From meeting: [{title}]({results_url})"

    create_mutation = """
    mutation CreateIssue($title: String!, $teamId: String!, $description: String) {
        issueCreate(input: { title: $title, teamId: $teamId, description: $description }) {
            success
            issue { id identifier }
        }
    }
    """

    for item in action_items:
        task = item.get("task", "").strip()
        if not task:
            continue
        _gql(access_token, create_mutation, {
            "title": task,
            "teamId": team_id,
            "description": description,
        })
