from callbacks.trace_callback import proxy_handler_instance  # noqa: F401

# Native Anthropic streams are SSE bytes yielded by this SDK processor. They
# bypass the ModelResponse iterator callback, and the proxy-level generator no
# longer owns the call's logging object. Observe here, without changing bytes.
import json
from datetime import datetime, timezone
from litellm.proxy.pass_through_endpoints.streaming_handler import PassThroughStreamingHandler

_native_chunk_processor = PassThroughStreamingHandler.chunk_processor


async def _observed_native_chunks(response, request_body, litellm_logging_obj,
                                  endpoint_type, start_time,
                                  passthrough_success_handler_obj, url_route):
    pending = b''
    details = getattr(litellm_logging_obj, 'model_call_details', {})
    async for chunk in _native_chunk_processor(
            response, request_body, litellm_logging_obj, endpoint_type, start_time,
            passthrough_success_handler_obj, url_route):
        if url_route == '/v1/messages' and details.get('_eval_first_frame_at') is None:
            arrived = datetime.now(timezone.utc)
            pending += chunk
            # SSE records can be split across network chunks. Interpret complete
            # data frames for timing only; always forward the original bytes.
            while b'\n\n' in pending or b'\r\n\r\n' in pending:
                lf = pending.find(b'\n\n')
                crlf = pending.find(b'\r\n\r\n')
                cut, width = (crlf, 4) if crlf >= 0 and (lf < 0 or crlf < lf) else (lf, 2)
                frame, pending = pending[:cut], pending[cut + width:]
                data = b'\n'.join(line[5:].lstrip(b' ') for line in frame.splitlines()
                                  if line.startswith(b'data:'))
                try:
                    parsed = json.loads(data)
                except (ValueError, UnicodeError):
                    continue
                proxy_handler_instance.log_stream_event(details, parsed, None, arrived)
            if details.get('_eval_first_frame_at') is not None:
                pending = b''
        yield chunk


PassThroughStreamingHandler.chunk_processor = staticmethod(_observed_native_chunks)
