import asyncio
import os
import uuid

import asyncpg
import pytest

from app.auth import can_edit_plugin
from app.main import build_astrbot_plugin_source
from app.plugin_identity import PluginIdentityConflict, plugin_canonical_path
from app.schema_migrations import apply_schema_migrations
from app.store import GithubIdentityConflict, InMemoryMarketStore, PgRedisMarketStore, SCHEMA_SQL


def test_github_username_changes_keep_market_identity_and_reuse_cannot_take_ownership():
    store = InMemoryMarketStore()
    alice = store.upsert_github_user({"id": "101", "login": "Alice"})
    plugin = store.register_plugin(alice, payload("organization"))
    with pytest.raises(GithubIdentityConflict):
        store.upsert_github_user({"id": "999", "login": "Alice"})
    renamed = store.upsert_github_user({"id": "101", "login": "RenamedAlice"})
    assert renamed["id"] == alice["id"]
    assert len(store.list_users()) == 1
    assert (
        plugin_canonical_path(store.get_plugin(plugin["id"])) == "/plugin/Alice/astrbot_plugin_same"
    )
    assert can_edit_plugin(renamed, plugin)


def payload(owner, name="astrbot_plugin_same"):
    return {
        "name": name,
        "display_name": "同名插件",
        "desc": "描述",
        "author": owner,
        "repo": f"https://github.com/{owner}/{name}",
        "tags": [],
    }


def test_namespaces_use_oauth_username_and_preserve_interactions():
    store = InMemoryMarketStore()
    alice = store.upsert_github_user({"id": "101", "login": "Alice"})
    bob = store.upsert_github_user({"id": "102", "login": "Bob"})
    first = store.register_plugin(alice, payload("organization"))
    comment = store.add_comment(first["id"], bob["id"], "hello")
    store.like_plugin(first["id"], bob["id"])
    second = store.register_plugin(bob, payload("Bob"))
    assert first["id"] != second["id"]
    assert first["name"] == second["name"]
    assert plugin_canonical_path(first) == "/plugin/Alice/astrbot_plugin_same"
    assert plugin_canonical_path(second) == "/plugin/Bob/astrbot_plugin_same"
    assert store.get_plugin_by_author("alice", first["name"])["id"] == first["id"]
    assert store.get_comment(comment["id"])["plugin_id"] == first["id"]
    assert store.get_plugin(first["id"])["likes"] == 1
    assert store.register_plugin(bob, payload("Bob"))["id"] == second["id"]
    assert len(build_astrbot_plugin_source([first, second])) == 2
    assert not can_edit_plugin({"id": "103", "github_login": "Alice"}, first)
    assert not store.list_user_plugins("103", "Alice")
    with pytest.raises(PluginIdentityConflict):
        store.register_plugin(alice, payload("another-organization"))
    with pytest.raises(PluginIdentityConflict):
        store.merge_user_into_user(bob["id"], alice["id"])
    assert store.get_plugin(second["id"])["owner_user_id"] == bob["id"]
    third = store.register_plugin(alice, payload("organization", "astrbot_plugin_other"))
    with pytest.raises(PluginIdentityConflict):
        store.update_plugin_metadata(third["id"], {"name": first["name"]})
    assert store.get_plugin(third["id"])["name"] == "astrbot_plugin_other"


def test_postgres_namespace_migration_and_concurrent_registration():
    url = os.getenv("ASTRBOT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set ASTRBOT_TEST_DATABASE_URL for PostgreSQL tests")
    asyncio.run(run_postgres_namespace(url))


async def run_postgres_namespace(url):
    schema = "namespace_test_" + uuid.uuid4().hex
    control = await asyncpg.connect(url)
    store = PgRedisMarketStore(url, "redis://127.0.0.1:6379/15", 3600)
    try:
        await control.execute(f"CREATE SCHEMA {schema}")
        await control.execute(f"SET search_path TO {schema}")
        await control.execute(SCHEMA_SQL)
        await control.execute(
            "ALTER TABLE market_plugins ADD CONSTRAINT market_plugins_name_key UNIQUE(name)"
        )
        await apply_schema_migrations(control)
        store.pool = await asyncpg.create_pool(
            url,
            min_size=1,
            max_size=4,
            server_settings={"search_path": schema},
            init=store._init_connection,
        )
        alice = await store.upsert_github_user({"id": "101", "login": "Alice"})
        bob = await store.upsert_github_user({"id": "102", "login": "Bob"})
        first, second = await asyncio.gather(
            store.register_plugin(alice, payload("org")), store.register_plugin(bob, payload("Bob"))
        )
        assert first["id"] != second["id"]
        assert (await store.get_plugin_by_author("ALICE", first["name"]))["id"] == first["id"]
        repeated = await asyncio.gather(
            *(store.register_plugin(bob, payload("Bob")) for _ in range(3))
        )
        assert {p["id"] for p in repeated} == {second["id"]}
        assert (await store.submit_plugin(bob, payload("Bob")))["id"] == second["id"]
        assert len(await store.list_plugins()) == 2
        with pytest.raises(PluginIdentityConflict):
            await store.register_plugin(alice, payload("other-org"))
        with pytest.raises(PluginIdentityConflict):
            await store.merge_user_into_user(bob["id"], alice["id"])
        assert (await store.get_plugin(second["id"]))["owner_user_id"] == bob["id"]
        optional_name = payload("org", "astrbot_plugin_other")
        optional_name.pop("display_name")
        third = await store.register_plugin(alice, optional_name)
        assert third["display_name"] == ""
        assert (await store.submit_plugin(alice, optional_name))["display_name"] == ""
        with pytest.raises(PluginIdentityConflict):
            await store.update_plugin_metadata(third["id"], {"name": first["name"]})
        assert (await store.get_plugin(third["id"]))["name"] == "astrbot_plugin_other"
    finally:
        if store.pool:
            await store.pool.close()
        await control.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await control.close()
