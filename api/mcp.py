from __future__ import annotations

import inspect
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.params import Param
from fastapi.routing import APIRoute
from pydantic import BaseModel

from api.main import app


MCP_PROTOCOL_VERSION = "2024-11-05"


@dataclass(frozen=True)
class ToolParameter:
    name: str
    annotation: Any
    required: bool
    default: Any = None


@dataclass(frozen=True)
class RouteTool:
    name: str
    route: APIRoute
    path: str
    methods: tuple[str, ...]
    endpoint: Any
    body_param: ToolParameter | None
    parameters: tuple[ToolParameter, ...]
    input_schema: dict[str, Any]

    @property
    def description(self) -> str:
        method = ",".join(self.methods)
        summary = self.route.summary or self.route.description
        if summary:
            return f"{summary}\n\nHTTP {method} {self.path}"
        return f"Arcana API endpoint: HTTP {method} {self.path}"


def build_tools() -> dict[str, RouteTool]:
    routes = list(_iter_api_routes(app.routes))
    base_names = [_tool_name_for_route(route) for route in routes]
    duplicate_names = {name for name in base_names if base_names.count(name) > 1}

    tools: dict[str, RouteTool] = {}
    for route, base_name in zip(routes, base_names):
        name = base_name
        if name in duplicate_names:
            name = _sanitize_tool_name(f"{base_name}_{route.path}")
        tools[name] = _route_to_tool(name, route)
    return tools


def call_tool(tool: RouteTool, arguments: dict[str, Any] | None = None) -> Any:
    arguments = arguments or {}
    kwargs: dict[str, Any] = {}

    if tool.body_param is not None:
        raw_body = arguments.get(tool.body_param.name, arguments)
        kwargs[tool.body_param.name] = _parse_value(raw_body, tool.body_param.annotation)

    for parameter in tool.parameters:
        if parameter.name in arguments:
            raw_value = arguments[parameter.name]
        elif parameter.required:
            raise ValueError(f"Missing required argument: {parameter.name}")
        else:
            raw_value = parameter.default
        kwargs[parameter.name] = _parse_value(raw_value, parameter.annotation)

    return tool.endpoint(**kwargs)


def handle_http_message(message: dict[str, Any]) -> dict[str, Any] | None:
    server = McpServer(build_tools())
    return server.handle_message(message)


def main() -> None:
    server = McpServer(build_tools())
    server.serve()


class McpServer:
    def __init__(self, tools: dict[str, RouteTool]) -> None:
        self._tools = tools

    def serve(self) -> None:
        while True:
            message = _read_message(sys.stdin.buffer)
            if message is None:
                break
            response = self.handle_message(message)
            if response is not None:
                _write_message(response)

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")

        if request_id is None or str(method).startswith("notifications/"):
            return None

        try:
            result = self._dispatch(method, message.get("params") or {})
        except Exception as exc:  # pragma: no cover - defensive protocol boundary
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": str(exc),
                },
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            requested_version = params.get("protocolVersion") or MCP_PROTOCOL_VERSION
            return {
                "protocolVersion": requested_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "arcana-api", "version": "0.1.0"},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [self._tool_metadata(tool) for tool in self._tools.values()]}
        if method == "tools/call":
            name = params.get("name")
            if name not in self._tools:
                raise ValueError(f"Unknown tool: {name}")
            return self._call_tool(self._tools[name], params.get("arguments") or {})
        raise ValueError(f"Unsupported MCP method: {method}")

    def _tool_metadata(self, tool: RouteTool) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
        }

    def _call_tool(self, tool: RouteTool, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = call_tool(tool, arguments)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(jsonable_encoder(result), ensure_ascii=False),
                    }
                ],
                "isError": False,
            }
        except HTTPException as exc:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"status_code": exc.status_code, "detail": exc.detail},
                            ensure_ascii=False,
                        ),
                    }
                ],
                "isError": True,
            }
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }


def _route_to_tool(name: str, route: APIRoute) -> RouteTool:
    signature = inspect.signature(route.endpoint)
    type_hints = get_type_hints(route.endpoint)
    body_param: ToolParameter | None = None
    parameters: list[ToolParameter] = []

    for parameter_name, signature_parameter in signature.parameters.items():
        annotation = type_hints.get(parameter_name, signature_parameter.annotation)
        if _is_pydantic_model(annotation):
            body_param = ToolParameter(
                name=parameter_name,
                annotation=annotation,
                required=True,
            )
            continue

        required, default = _parameter_default(signature_parameter)
        parameters.append(
            ToolParameter(
                name=parameter_name,
                annotation=annotation,
                required=required,
                default=default,
            )
        )

    input_schema = _input_schema(body_param, parameters)
    return RouteTool(
        name=name,
        route=route,
        path=route.path,
        methods=tuple(sorted(route.methods)),
        endpoint=route.endpoint,
        body_param=body_param,
        parameters=tuple(parameters),
        input_schema=input_schema,
    )


def _iter_api_routes(routes: list[Any]) -> Any:
    for route in routes:
        if isinstance(route, APIRoute) and route.include_in_schema:
            yield route
            continue

        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from _iter_api_routes(original_router.routes)

def _input_schema(
    body_param: ToolParameter | None,
    parameters: list[ToolParameter],
) -> dict[str, Any]:
    if body_param is not None and not parameters:
        return _schema_for_model(body_param.annotation)

    properties: dict[str, Any] = {}
    required: list[str] = []

    if body_param is not None:
        properties[body_param.name] = _schema_for_model(body_param.annotation)
        required.append(body_param.name)

    for parameter in parameters:
        properties[parameter.name] = _schema_for_annotation(parameter.annotation)
        if not parameter.required:
            properties[parameter.name]["default"] = parameter.default
        if parameter.required:
            required.append(parameter.name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _schema_for_model(model: type[BaseModel]) -> dict[str, Any]:
    if hasattr(model, "model_json_schema"):
        return model.model_json_schema()
    return model.schema()


def _schema_for_annotation(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Signature.empty or annotation is Any:
        return {}

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is Literal:
        values = list(args)
        schema: dict[str, Any] = {"enum": values}
        value_types = {type(value) for value in values}
        if value_types == {str}:
            schema["type"] = "string"
        elif value_types <= {int}:
            schema["type"] = "integer"
        elif value_types <= {int, float}:
            schema["type"] = "number"
        elif value_types == {bool}:
            schema["type"] = "boolean"
        return schema

    if origin in (Union, UnionType):
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1:
            schema = _schema_for_annotation(non_none[0])
            schema["nullable"] = True
            return schema
        return {"anyOf": [_schema_for_annotation(arg) for arg in non_none]}

    if origin in (list, tuple, set):
        item_type = args[0] if args else Any
        return {"type": "array", "items": _schema_for_annotation(item_type)}

    if origin is dict:
        return {"type": "object"}

    if _is_pydantic_model(annotation):
        return _schema_for_model(annotation)

    if inspect.isclass(annotation) and issubclass(annotation, Enum):
        values = [member.value for member in annotation]
        return {"type": "string", "enum": values}

    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is date:
        return {"type": "string", "format": "date"}
    if annotation is datetime:
        return {"type": "string", "format": "date-time"}
    return {}


def _parse_value(value: Any, annotation: Any) -> Any:
    if value is None or annotation is inspect.Signature.empty or annotation is Any:
        return value

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in (Union, UnionType):
        non_none = [arg for arg in args if arg is not type(None)]
        if not non_none:
            return value
        last_error: Exception | None = None
        for arg in non_none:
            try:
                return _parse_value(value, arg)
            except Exception as exc:  # pragma: no cover - best-effort coercion
                last_error = exc
        if last_error is not None:
            raise last_error

    if origin is Literal:
        if value in args:
            return value
        raise ValueError(f"Expected one of {list(args)}, got {value!r}")

    if origin is list:
        item_type = args[0] if args else Any
        values = value.split(",") if isinstance(value, str) else value
        return [_parse_value(item, item_type) for item in values]

    if _is_pydantic_model(annotation):
        if isinstance(value, annotation):
            return value
        return annotation(**value)

    if annotation is date and isinstance(value, str):
        return date.fromisoformat(value)
    if annotation is datetime and isinstance(value, str):
        return datetime.fromisoformat(value)
    if inspect.isclass(annotation) and issubclass(annotation, Enum):
        return annotation(value)
    if annotation in (str, int, float, bool):
        return annotation(value)
    return value


def _parameter_default(parameter: inspect.Parameter) -> tuple[bool, Any]:
    default = parameter.default
    if default is inspect.Signature.empty:
        return True, None
    if isinstance(default, Param):
        if default.default is Ellipsis:
            return True, None
        return False, default.default
    return False, default


def _is_pydantic_model(annotation: Any) -> bool:
    return inspect.isclass(annotation) and issubclass(annotation, BaseModel)


def _tool_name_for_route(route: APIRoute) -> str:
    return _sanitize_tool_name(route.name or f"{next(iter(route.methods))}_{route.path}")


def _sanitize_tool_name(value: str) -> str:
    result = "".join(character if character.isalnum() else "_" for character in value)
    result = "_".join(part for part in result.split("_") if part)
    return result.lower()


def _read_message(stream: Any) -> dict[str, Any] | None:
    first_line = stream.readline()
    if not first_line:
        return None

    stripped = first_line.strip()
    if not stripped:
        return _read_message(stream)

    if stripped.lower().startswith(b"content-length:"):
        content_length = int(stripped.split(b":", 1)[1].strip())
        while True:
            header_line = stream.readline()
            if header_line in (b"\r\n", b"\n", b""):
                break
        payload = stream.read(content_length)
        return json.loads(payload.decode("utf-8"))

    return json.loads(stripped.decode("utf-8"))


def _write_message(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()


