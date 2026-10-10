"""Agno 持久化配置；数据库连接属于重启生效资源。"""
from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.conf.runtime import load_model


class MysqlConnection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    database: str = Field(validation_alias=AliasChoices("database", "datatbase"), min_length=1)
    user: str = Field(min_length=1)
    password: SecretStr
    host: str = Field(min_length=1)
    port: int = Field(default=3306, ge=1, le=65535)

    @field_validator("database", "user", "host")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("数据库连接字段不能为空白")
        return value


class DatabaseConf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["sqlite", "mysql"] = "sqlite"
    db_file: str = Field(default="insurance_qa_agent.db", min_length=1)
    mysql_connection: MysqlConnection | None = None

    @field_validator("db_file")
    @classmethod
    def nonblank_file(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("SQLite 路径不能为空白")
        return value

    @model_validator(mode="after")
    def require_mysql_connection(self) -> DatabaseConf:
        if self.backend == "mysql" and (
            self.mysql_connection is None or not self.mysql_connection.password.get_secret_value()
        ):
            raise ValueError("MySQL 连接及密码必须配置")
        return self


def database_conf() -> DatabaseConf:
    return load_model("database", DatabaseConf, common=True, environment_only=("mysql_connection",))
