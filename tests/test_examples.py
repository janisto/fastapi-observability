from examples.basic import app
from tests._client import asgi_client


async def test_basic_example_is_runnable():
    async with asgi_client(app) as client:
        response = await client.get("/items/42", headers={"X-Request-ID": "example"})
    assert response.status_code == 200
    assert response.json() == {"item_id": "42"}
    assert response.headers["X-Request-ID"] == "example"
