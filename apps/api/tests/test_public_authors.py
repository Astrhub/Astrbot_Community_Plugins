from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from app.store import InMemoryMarketStore


def register_plugin(store, user, name):
    return store.register_plugin(
        user,
        {
            "name": name,
            "display_name": name,
            "desc": "测试插件",
            "author": user["public_username"],
            "repo": f"https://github.com/example/{name}",
            "tags": [],
        },
    )


def test_public_authors_feed_distinguishes_listed_plugins_without_private_fields():
    store = InMemoryMarketStore()
    listed = store.upsert_github_user({"id": "1", "login": "listed_author"})
    pending = store.upsert_github_user({"id": "2", "login": "pending_author"})
    store.upsert_github_user({"id": "3", "login": "empty_author"})
    listed_plugin = register_plugin(store, listed, "astrbot_plugin_listed")
    store.update_plugin_status(listed_plugin["id"], "listed", None)
    register_plugin(store, pending, "astrbot_plugin_pending")

    app = create_app(settings=load_settings({"ENABLE_DEV_AUTH": "false"}), store=store)
    with TestClient(app) as client:
        response = client.get("/v1/authors")
        admin_response = client.get("/v1/admin/users")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=60"
    assert admin_response.status_code == 401
    items = {item["username"]: item for item in response.json()["items"]}
    assert items["listed_author"]["has_listed_plugins"] is True
    assert items["pending_author"]["has_listed_plugins"] is False
    assert items["empty_author"]["has_listed_plugins"] is False
    assert all(set(item) == {"username", "has_listed_plugins"} for item in items.values())
