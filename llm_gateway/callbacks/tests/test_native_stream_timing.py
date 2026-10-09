"""Run the installed native provider path with real SDK Logging and SSE bytes."""
import asyncio
import json
from datetime import datetime, timezone

import httpx
import litellm
from callbacks import trace_callback as trace
import custom_callbacks
from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.llms.anthropic.experimental_pass_through.messages.transformation import AnthropicMessagesConfig
from litellm.proxy.pass_through_endpoints.streaming_handler import PassThroughStreamingHandler


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


def frame(delta, sep=b'\n'):
    return b'event: content_block_delta' + sep + b'data: ' + json.dumps({
        'type': 'content_block_delta', 'delta': delta}).encode() + sep + sep


def test_real_native_provider_and_logging_split_frames_parallel_identity(monkeypatch):
    records = []
    observer = trace.EvalTraceLogger()
    monkeypatch.setattr(trace, '_append', records.append)
    monkeypatch.setattr(custom_callbacks, 'proxy_handler_instance', observer)
    monkeypatch.setattr(litellm, 'input_callback', [observer])
    monkeypatch.setattr(trace, '_attribution_snapshot', lambda messages, kwargs: (
        {'case_id': kwargs['litellm_call_id'], 'execution_id': kwargs['litellm_call_id'] + '-execution'},
        'anthropic_messages', []))

    async def no_post_logging(**kwargs):
        pass
    monkeypatch.setattr(PassThroughStreamingHandler, '_route_streaming_logging_to_handler', no_post_logging)

    async def run(cid, delta):
        logging = Logging('synthetic', [], True, 'anthropic_messages',
                          datetime.now(), cid, cid)
        logging.update_environment_variables({}, {})
        logging.pre_call([], None)
        assert logging.model_call_details['_eval_trace_t0']
        payload = frame({'type': 'input_json_delta', 'partial_json': '{}'}) + frame(delta, b'\r\n')
        # Even UTF-8 code points and CRLF delimiters can straddle byte chunks.
        chunks = [payload[i:i + 7] for i in range(0, len(payload), 7)]
        response = httpx.Response(200, stream=Chunks(chunks))
        stream = AnthropicMessagesConfig().get_async_streaming_response_iterator(
            'synthetic', response, {}, logging)
        received = [c async for c in stream]
        assert received == chunks

    async def together():
        await asyncio.gather(run('a', {'type': 'text_delta', 'text': '回答'}),
                             run('b', {'type': 'thinking_delta', 'thinking': 'reason'}))
        await asyncio.sleep(0)
    asyncio.run(together())
    frames = [r for r in records if r.get('event') == 'first_frame']
    assert {(r['call_id'], r['case_id'], r['execution_id'], r['first_frame_kind']) for r in frames} == {
        ('a', 'a', 'a-execution', 'text'), ('b', 'b', 'b-execution', 'thinking')}
    assert len(frames) == 2 and all(r['first_frame_ms'] >= 0 for r in frames)


def test_native_empty_or_unknown_logging_does_not_invent_timing(monkeypatch):
    records = []
    monkeypatch.setattr(trace, '_append', records.append)
    async def no_post_logging(**kwargs):
        pass
    monkeypatch.setattr(PassThroughStreamingHandler, '_route_streaming_logging_to_handler', no_post_logging)
    async def run():
        logging = Logging('synthetic', [], True, 'anthropic_messages', datetime.now(), 'unknown', 'unknown')
        chunks = [frame({'type': 'text_delta', 'text': '  '}), b'data: malformed\n\n']
        response = httpx.Response(200, stream=Chunks(chunks))
        output = [c async for c in AnthropicMessagesConfig().get_async_streaming_response_iterator(
            'synthetic', response, {}, logging)]
        assert output == chunks
        await asyncio.sleep(0)
    asyncio.run(run())
    assert not records
