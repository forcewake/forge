"""Drain durable commands and the sole engine with PostgreSQL wake-up hints."""

import asyncio
import signal

from forge.runtime.assembly import Runtime


async def serve(runtime: Runtime, stop: asyncio.Event) -> None:
    url = runtime.engine.url.render_as_string(hide_password=False)
    async with runtime.wakeups.listen(url):
        while not stop.is_set():
            try:
                reactive = await runtime.reactive.tick()
                commands = await runtime.commands.tick(limit=20)
                execution = await runtime.worker.tick()
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                await asyncio.sleep(1)
                continue
            await runtime.lane.reap_terminal_blobs()
            if reactive == commands == 0 and execution.execution.claimed == 0:
                waiter = asyncio.create_task(runtime.wakeups.wait())
                stopped = asyncio.create_task(stop.wait())
                try:
                    await asyncio.wait((waiter, stopped), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in (waiter, stopped):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(waiter, stopped, return_exceptions=True)


def signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for value in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(value, stop.set)
