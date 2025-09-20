import asyncio
import os

from agent_system.mcp import status


async def main():
    # Set strict rate limit to exercise suppression
    os.environ['AGENT_STATUS_MAX_RPS'] = '1'

    q = await status.status_bus.subscribe()

    async def publisher():
        # publish several progress events rapidly
        for i in range(5):
            await status.publish_status('duckduckgo_search', f'progress {i}', request_id='rid-1', phase=status.PHASE_PROGRESS)
        # publish end event
        await status.publish_status('duckduckgo_search', 'done', request_id='rid-1', phase=status.PHASE_END)

    async def consumer():
        received = []
        try:
            while True:
                ev = await asyncio.wait_for(q.get(), timeout=2)
                received.append((ev.phase, ev.message))
                if ev.phase == status.PHASE_END or ev.phase == status.PHASE_ERROR:
                    break
        except asyncio.TimeoutError:
            print('Consumer timed out waiting for events')

        print('Received events:', received)

    await asyncio.gather(publisher(), consumer())


if __name__ == '__main__':
    asyncio.run(main())
