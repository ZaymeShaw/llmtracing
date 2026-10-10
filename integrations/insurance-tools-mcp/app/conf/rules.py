"""运行规则的完整候选校验；跨文件约束通过后才发布同一轮策略。"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.conf.capabilities import (
    CAPABILITIES_DIR, CapabilityPolicies, capability_paths, parse_capabilities,
)
from app.conf.runtime import current_source, load_bundle, layer_paths, layer_payload
from app.core.field_dict import FieldDict
from app.orchestration.gate import GateConf


class FieldsConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    fields: dict[str, list[str]] = Field(min_length=1)

    non_detail_fields: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dictionary(self) -> FieldsConf:
        for code, aliases in self.fields.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", code):
                raise ValueError("字段码必须是小写规范标识")
            for alias in aliases:
                normalized = alias.strip().lower()
                if not normalized or (normalized in self.fields and normalized != code):
                    raise ValueError("字段别名不得为空或指向其他字段码")
        FieldDict(self.fields, version=self.version)
        return self


class RulesConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fields: FieldsConf
    gate: GateConf
    capabilities: CapabilityPolicies


def rules_conf() -> RulesConf:
    source = current_source()
    root = source.capabilities_directory or CAPABILITIES_DIR
    fields_path, gate_path = source.path("fields"), source.path("gate")
    paths = tuple(p for file in (fields_path, gate_path, *capability_paths(root, source.env_type)) for p in layer_paths(file))

    def prepare(content: dict[Path, bytes]) -> RulesConf:
        fields = FieldsConf.model_validate(layer_payload(content, fields_path), strict=True)
        gate = GateConf.model_validate(layer_payload(content, gate_path), strict=True)
        capabilities = parse_capabilities(
            content, root=root, env_type=source.env_type, allowed_risk_levels=set(gate.risk_levels),
        )
        return RulesConf(fields=fields, gate=gate, capabilities=capabilities)

    return load_bundle("rules", RulesConf, paths, prepare)
