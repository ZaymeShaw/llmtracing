"""Single entry for tool content delivered to the model, including terminal hooks."""
from __future__ import annotations

import logging
import asyncio
import hashlib
import json
from time import perf_counter
from copy import deepcopy

from app.conf import harness_conf
from app.conf.runtime import read_once
from app.schemas import ToolEnvelope
from app.core.harness_config import HarnessConf
from app.core.model_budget import current_model_budget
from app.tools.model_output import project_business_tool_result
from app.tools.model_projection import project_model_output

logger = logging.getLogger(__name__)


def _compact_notice(projected: str) -> str:
    value = json.loads(projected)
    value.setdefault('execution', {}).setdefault('notes', []).append('完整字段视图超过展示上限，已采用默认精简字段；原始记录和卡片仍保留。')
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class ModelView:
    @staticmethod
    async def async_tool_result(tool: str, raw: str, args: dict, state: dict | None, **kwargs) -> str:
        """Only pure source projection runs on a worker; reference publication stays on the loop."""
        config = kwargs.get('config') or read_once('harness', harness_conf)
        query = '\n'.join(str(v or '') for v in ((state or {}).get('route', {}).get('user_text'), args.get('query')))
        if len(raw) > 16384:
            def project():
                projected = project_business_tool_result(tool, raw,
                    compact=args.get('compact', True), fields=args.get('fields'), query=query, envelope=kwargs.get('envelope'))
                if not args.get('compact', True) and len(projected) > config.output_shaping.max_chars:
                    compacted = project_business_tool_result(tool, raw, compact=True, fields=args.get('fields'), query=query, envelope=kwargs.get('envelope'))
                    if len(compacted) < len(projected):
                        projected = _compact_notice(compacted)
                return projected
            try:
                projected = await asyncio.to_thread(project)
            except Exception:
                # Keep the same usable failure view as the synchronous boundary;
                # projection errors must not discard already published raw data.
                projected = '{"result":{},"projection":{"incomplete":true,"reason":"模型视图生成失败，原始结果已保留"}}'
                kwargs['_projection_failed'] = True
            kwargs['_projected'] = projected
        return ModelView.tool_result(tool, raw, args, state, **kwargs)

    @staticmethod
    def tool_result(tool: str, raw: str, args: dict, state: dict | None, *,
                    source: str = 'tool', max_chars: int | None = None, remember: bool = True,
                    envelope: ToolEnvelope | None = None, config: HarnessConf | None = None, _projected: str | None = None,
                    _projection_failed: bool = False) -> str:
        """Only the view changes. Raw inputs and published facts/cards are authoritative.

        Retained raw content is request-local, bounded to 4194304 characters/64 calls, and used only
        to reproject exact known message segments on overflow. No text is stored in logs.
        """
        started = perf_counter()
        config = config or read_once('harness', harness_conf)
        policy = config.output_shaping
        query = '\n'.join(str(v or '') for v in ((state or {}).get('route', {}).get('user_text'), args.get('query')))
        try:
            if _projection_failed:
                raise ValueError('source projection unavailable')
            projected = _projected or project_business_tool_result(tool, raw, compact=args.get('compact', True),
                fields=args.get('fields'), query=query, envelope=envelope)
            if _projected is None and not args.get('compact', True) and len(projected) > policy.max_chars:
                compacted = project_business_tool_result(tool, raw, compact=True, fields=args.get('fields'), query=query, envelope=envelope)
                if len(compacted) < len(projected):
                    projected = _compact_notice(compacted)
            visible = project_model_output(tool, projected, state, original=raw, query=query,
                                           max_chars=max_chars, config=config, envelope=envelope)
            material = json.loads(visible).get('result', {})
            if state is not None and tool in {'policy_search', 'customer_search', 'policy_detail', 'customer_detail'} and material.get('data') is not None:
                # View changes and narrower searches are useful progress even if
                # their raw facts were already present. Identical replays are not.
                fingerprint = hashlib.sha256(json.dumps([tool, str(args.get('query') or '').strip(), material['data']],
                    ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                views = state.setdefault('budget', {}).setdefault('progress_views', [])
                if fingerprint not in views and len(views) < policy.max_records:
                    views.append(fingerprint)
        except Exception:
            visible = '{"result":{},"projection":{"incomplete":true,"reason":"模型视图生成失败，原始结果已保留"}}'
        budget = current_model_budget()
        if (remember and budget is not None and len(budget.tool_views) < policy.max_records
                and budget.tool_view_chars + len(raw) + len(visible) <= 4194304):
            scope = tuple((state or {}).get('route', {}).get(k) for k in ('session_id', 'trace_id', 'identity_scope'))
            budget.tool_views.append({'tool': tool, 'raw': raw, 'args': deepcopy(args), 'visible': visible, 'scope': scope, 'source': source})
            budget.tool_view_chars += len(raw) + len(visible)
        try:
            logger.info('model_tool_view', extra={'observability': {'tool': tool, 'source': source,
                'raw_chars': len(raw), 'visible_chars': len(visible), 'view_ms': round((perf_counter() - started) * 1000, 3)}})
        except Exception:
            pass
        return visible
