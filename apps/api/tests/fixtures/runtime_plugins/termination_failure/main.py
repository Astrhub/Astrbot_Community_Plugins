from astrbot.api import star


class RuntimeTerminationFailurePlugin(star.Star):
    async def terminate(self) -> None:
        raise RuntimeError("runtime fixture termination failure")
