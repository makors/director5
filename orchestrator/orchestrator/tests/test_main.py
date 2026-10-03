import asyncio

import httpx

from orchestrator.main import app


def test_ping() -> None:
    async def request_ping() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/ping")
            assert response.status_code == 200
            assert response.json() == {"message": "pong"}

    asyncio.run(request_ping())
