from astrbot.api import star


class RuntimeInitializeFailurePlugin(star.Star):
    async def initialize(self) -> None:
        raise RuntimeError("runtime fixture initialize failure")
