"""Current authenticated native hook envelope, with no executable command by default."""

from tests.test_gitlab_engine_ingress import payload, signed


async def post(environment, document=None, *, headers=None, connection="ce-1"):
    if document is None:
        document = payload()
        document["object_attributes"]["note"] = "Read-only native context"
    native_headers, body = signed(document)
    return await environment.client.post(
        "/hooks/" + connection,
        headers=native_headers if headers is None else headers,
        content=body,
    )
