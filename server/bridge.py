import os


def openai_realtime_url() -> str:
    model = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2")
    return f"wss://api.openai.com/v1/realtime?model={model}"


def openai_headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
