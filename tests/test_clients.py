"""rc.clients against the real Anthropic SDK, with HTTP served by httpx.MockTransport.

These exercise the SDK's own response, Stream and MessageStream classes, so a change
in how the SDK exposes usage or streams shows up here rather than in production.
"""
import asyncio
import json

import pytest

anthropic = pytest.importorskip("anthropic")
try:  # anthropic 2.x ships its own fork, httpx2; 0.x and 1.x use httpx
    import httpx2 as httpx
except ImportError:
    import httpx

import recursa as rc  # noqa: E402
from recursa.clients import EARLY_CLOSE  # noqa: E402

USAGE = {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 500,
         "cache_creation_input_tokens": 0}
EXPECTED_COST = (1000 * 4 + 200 * 20 + 500 * 4 * 0.05) / 1e6  # claude-opus-5-5 prices
TEXT = '{"category": "billing", "priority": "normal"}'


def _message(model="claude-opus-5-5", stop="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": TEXT}], "stop_reason": stop, "stop_sequence": None,
            "usage": USAGE}


def _sse(model="claude-opus-5-5", complete=True):
    start = dict(_message(model), content=[], stop_reason=None, usage=dict(USAGE, output_tokens=1))
    events = [("message_start", {"type": "message_start", "message": start}),
              ("content_block_start", {"type": "content_block_start", "index": 0,
                                       "content_block": {"type": "text", "text": ""}})]
    for i in range(0, len(TEXT), 10):
        events.append(("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": TEXT[i:i + 10]}}))
    if complete:
        events += [("content_block_stop", {"type": "content_block_stop", "index": 0}),
                   ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn",
                                      "stop_sequence": None}, "usage": {"output_tokens": 200}}),
                   ("message_stop", {"type": "message_stop"})]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def _handler(status=200, served="claude-opus-5-5", complete=True):
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if status != 200:
            return httpx.Response(status, json={"type": "error", "error": {"type": "api_error", "message": "boom"}})
        if body.get("stream"):
            return httpx.Response(200, content=_sse(served, complete),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=_message(served))
    return handle


def _sync(**kw):
    http = httpx.Client(transport=httpx.MockTransport(_handler(**kw)))
    return rc.clients.Anthropic(api_key="test", http_client=http, max_retries=0)


def _async(**kw):
    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler(**kw)))
    return rc.clients.AsyncAnthropic(api_key="test", http_client=http, max_retries=0)


REQ = dict(model="claude-opus-5-5", max_tokens=100, messages=[{"role": "user", "content": "hi"}])


def _run(call):
    """Run ``call`` (sync or async, returning the agent output) inside a traced agent run."""
    async def agent(task, ctx):
        out = call()
        if asyncio.iscoroutine(out):
            out = await out
        return json.loads(out)

    a = rc.wrap(agent, "m", {"system_prompt": "p"})
    return asyncio.run(a.run({}, a.default_config(), "v1", mode="eval", example_id="e", sample=0))


def _assert_measured(run, streamed):
    (step,) = run.steps
    assert run.status == "ok" and run.telemetry == "measured" and step.measured
    assert (step.input_tokens, step.output_tokens) == (1000, 200)
    assert step.cost_usd == pytest.approx(EXPECTED_COST)
    assert step.output == [TEXT] and step.provider_metadata["stop_reason"] == "end_turn"
    if streamed:
        md = step.provider_metadata
        assert md["streamed"] and md["ttft_ms"] is not None and md["ttft_ms"] <= step.latency_ms


def test_create_sync_and_async():
    c = _sync()
    _assert_measured(_run(lambda: c.messages.create(**REQ).content[0].text), streamed=False)
    ac = _async()

    async def call():
        return (await ac.beta.messages.create(**REQ)).content[0].text

    _assert_measured(_run(call), streamed=False)


def test_create_prices_the_served_model_after_fallback():
    c = _sync(served="claude-sonnet-5-5")
    (step,) = _run(lambda: c.messages.create(**REQ).content[0].text).steps
    assert step.name == "claude-sonnet-5-5" and step.provider_metadata["requested_model"] == "claude-opus-5-5"
    assert step.cost_usd == pytest.approx((1000 * 2 + 200 * 10 + 500 * 2 * 0.05) / 1e6)


def test_stream_helper_sync_text_stream_and_final_message():
    c = _sync()

    def via_text_stream():
        with c.messages.stream(**REQ) as s:
            return "".join(s.text_stream)

    def via_final_message():
        with c.beta.messages.stream(**REQ) as s:
            return s.get_final_message().content[0].text

    _assert_measured(_run(via_text_stream), streamed=True)
    _assert_measured(_run(via_final_message), streamed=True)


def test_stream_helper_async():
    ac = _async()

    async def call():
        async with ac.messages.stream(**REQ) as s:
            return "".join([t async for t in s.text_stream])

    _assert_measured(_run(call), streamed=True)


def test_raw_event_stream_sync_and_async():
    c = _sync()

    def sync_call():
        text = ""
        for event in c.messages.create(stream=True, **REQ):
            if event.type == "content_block_delta":
                text += event.delta.text
        return text

    _assert_measured(_run(sync_call), streamed=True)
    ac = _async()

    async def async_call():
        text = ""
        async for event in await ac.beta.messages.create(stream=True, **REQ):
            if event.type == "content_block_delta":
                text += event.delta.text
        return text

    _assert_measured(_run(async_call), streamed=True)


def test_stream_closed_early_is_recorded_with_partial_usage():
    c = _sync()

    def leave_early():
        with c.messages.stream(**REQ) as s:
            next(iter(s.text_stream))
        return TEXT

    (step,) = _run(leave_early).steps
    assert step.error == EARLY_CLOSE and step.input_tokens == 1000 and step.measured

    truncated = _sync(complete=False)

    def server_cut_off():
        with truncated.messages.create(stream=True, **REQ) as s:
            for _ in s:
                pass
        return TEXT

    (step,) = _run(server_cut_off).steps
    assert step.error == EARLY_CLOSE and step.input_tokens == 1000


def test_http_errors_are_recorded_and_raised():
    c = _sync(status=500)
    run = _run(lambda: c.messages.create(**REQ).content[0].text)
    (step,) = run.steps
    assert run.status == "error" and step.error.startswith("InternalServerError") and not step.measured


def test_other_messages_methods_pass_through():
    c = _sync()
    assert callable(c.messages.count_tokens) and callable(c.beta.messages.count_tokens)


def test_example_models_are_priced():
    """Every model id the code base defaults to must have a price, or its runs read as self-reported."""
    import re
    from pathlib import Path

    from recursa.llm import PRICES

    root = Path(__file__).resolve().parents[1]
    ids = set()
    for path in [*root.glob("recursa/**/*.py"), *root.glob("examples/**/*.py"), *root.glob("examples/**/*.yaml")]:
        ids |= set(re.findall(r"claude-[a-z]+-\d+-\d+", path.read_text()))
    assert ids and ids <= set(PRICES), ids - set(PRICES)
