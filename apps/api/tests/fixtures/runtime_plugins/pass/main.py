from astrbot.api import llm_tool, star
from astrbot.api.event import AstrMessageEvent, filter


class RuntimeFixturePlugin(star.Star):
    async def initialize(self) -> None:
        self.ready = True

    @filter.command("runtime_fixture")
    async def runtime_fixture(self, event: AstrMessageEvent) -> None:
        return None

    @filter.on_astrbot_loaded()
    async def on_loaded(self) -> None:
        self.loaded = True

    @llm_tool("runtime_fixture_tool")
    async def runtime_fixture_tool(self, query: str) -> str:
        return query

    async def terminate(self) -> None:
        self.ready = False
