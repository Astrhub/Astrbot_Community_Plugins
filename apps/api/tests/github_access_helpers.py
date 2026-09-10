"""OAuth boundary doubles for tests concerned with market and artifact behavior."""

from fastapi import HTTPException
from app.main import validate_github_repo


def install_github_access_double(app):
    class PublicationAccess:
        async def verify(self, artifact):
            plugin = app.state.store.get_plugin(artifact["plugin_id"])
            return {"repo": plugin["repo"], "owner_user_id": str(plugin["owner_user_id"])}

    class RepositoryAccess:
        def __init__(self, store, settings, user, reference):
            self.user = user

        async def verify(self, repo, expected_repository_id=""):
            owner = validate_github_repo(repo).group("owner")
            if owner.lower() != str(self.user.get("github_login") or "").lower():
                raise HTTPException(403, "GitHub account must own the repository")
            runner = app.state.artifact_runtime.job_runner
            if runner is not None:
                runner.publication_authorizer = PublicationAccess()
            return {
                "repo": repo,
                "id": expected_repository_id or "repo-fixture",
                "owner_id": self.user["id"],
                "github_user_id": self.user["id"],
                "user_id": self.user["id"],
                "auth_reference": "fixture",
            }

    app.state.github_access_factory = RepositoryAccess
