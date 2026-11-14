"""A dependency-free local MCP server for Narou Match.

The server speaks the MCP JSON-RPC stdio transport directly so it can be
installed in clients without bringing a web server or an API key. It only
forwards search metadata to the public Narou Match endpoint; it does not fetch
novel text, call an LLM, or persist results.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from http.client import HTTPException as HttpClientException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("narou_match_mcp")
DEFAULT_API_URL = "https://api.novel-match.com/narou/search"
API_TIMEOUT_SECONDS = 12
MAX_QUERY_LENGTH = 200
MAX_RESULTS = 20
MAX_OFFSET = 1000
MAX_TAGS = 10
MAX_TAG_LENGTH = 80
MAX_EXCLUDE_WORDS = 10
MAX_EXCLUDE_WORD_LENGTH = 80
MAX_GENRES = 5
MAX_SYNOPSIS_LENGTH = 1200
MAX_KEYWORD_LENGTH = 600
MAX_OUTPUT_BYTES = 90_000

_ENUMS = {
    "status": {"all", "ongoing", "completed", "short"},
    "length_bucket": {"all", "tanpen", "short", "medium", "long"},
    "sort": {"relevance", "quality", "popular", "updated", "new"},
}

TOOL_NAME = "search_narou"
TOOL_DESCRIPTION = (
    "小説を読もう！の作品を検索します。外部のLLMが利用者の自然文の意味を"
    "検索語・フィルター・除外語に整理してから呼び出してください。"
    "word検索は空白区切りのAND検索なので、利用者の文章に明示された正の検索語は"
    "全て保持し、同義語・関連語・未指定の条件を追加しないでください。"
    "検索語が多い場合も明示条件を削らず、追加の語を作らないでください。必要なら中心語を変えた検索を少数回行い、"
    "結果のncodeで重複を除いてください。"
    "肯定検索語とは異なり、避けたい要素は意味を解釈して確度の高い関連除外語へ"
    "慎重に広げ、exclude_wordsで公式APIの部分一致除外にできます。"
    "このMCPサーバーはLLMを呼び出しません。"
    "Narou Matchの公開検索APIが返す作品情報だけを使い、本文は取得しません。"
    "結果にはタイトル、作者、あらすじ、キーワード、評価、完結状態、公式URLなどを含みます。"
)


def _tool_schema() -> dict[str, Any]:
    """Return the JSON Schema advertised to MCP clients."""

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string",
                "maxLength": MAX_QUERY_LENGTH,
                "description": (
                    "外部のLLMが自然文の意味を整理して作る検索語。作品名・あらすじ・"
                    "キーワードに現れそうな、利用者の文章に明示された検索語を全て保持する。"
                    "検索語が多い場合も明示条件を削らず、単なる依頼文のコピーや、"
                    "同義語・関連語・未指定の条件を正の検索語へ追加しない。wordは空白区切りのAND検索である。"
                    "queryは最大200文字である。例:"
                    "「俺つええ系以外で男主人公が着実に強くなる物語」なら「男主人公 成長」など。"
                    "空文字なら条件検索。"
                ),
            },
            "status": {
                "type": "string",
                "enum": ["all", "ongoing", "completed", "short"],
                "default": "all",
                "description": "作品の状態。shortは短編作品。",
            },
            "length_bucket": {
                "type": "string",
                "enum": ["all", "tanpen", "short", "medium", "long"],
                "default": "all",
                "description": "作品の文字数。tanpenは掌編、shortは短編。",
            },
            "biggenre": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100000,
                "description": "大ジャンルコード。恋愛1、ファンタジー2、文芸3、SF4など。",
            },
            "genre": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100000,
                "description": "小ジャンルコード。例: ハイファンタジー201。",
            },
            "genres": {
                "type": "array",
                "maxItems": MAX_GENRES,
                "items": {"type": "integer", "minimum": 0, "maximum": 100000},
                "description": "小ジャンルコードの配列。",
            },
            "tags": {
                "type": "array",
                "maxItems": MAX_TAGS,
                "items": {"type": "string", "maxLength": MAX_TAG_LENGTH},
                "description": "なろう公式キーワード。最大10件。",
            },
            "exclude_words": {
                "type": "array",
                "maxItems": MAX_EXCLUDE_WORDS,
                "items": {"type": "string", "maxLength": MAX_EXCLUDE_WORD_LENGTH},
                "description": (
                    "外部のLLMが利用者の否定的な希望の意味を整理して選ぶ除外語。公式APIの"
                    "notwordで作品名・あらすじ・キーワードなどへの部分一致で除外される。"
                    "例えば「俺つええ系以外」なら、文脈に応じて「俺TUEEE」「チート」「無双」"
                    "「主人公最強」「無敵」などを候補にする。ただし完全な意味判定ではないため、"
                    "高い確度の語を最大10件まで指定する。"
                ),
            },
            "min_rating_count": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1000000,
                "description": "最低評価者数。未指定ならNarou Matchの既定値。",
            },
            "include_unrated": {
                "type": "boolean",
                "default": False,
                "description": "評価者がいない作品を含めるか。",
            },
            "include_stalled": {
                "type": "boolean",
                "default": True,
                "description": "更新が長期間止まった未完作品を候補に残すか。",
            },
            "has_book": {
                "type": "boolean",
                "description": "書籍化が確認された作品だけに絞るか。",
            },
            "has_manga": {
                "type": "boolean",
                "description": "漫画化が確認された作品だけに絞るか。",
            },
            "has_anime": {
                "type": "boolean",
                "description": "アニメ化が確認された作品だけに絞るか。",
            },
            "is_r15": {
                "type": "boolean",
                "description": "R15作品を含めるか。falseならR15を除外。",
            },
            "sort": {
                "type": "string",
                "enum": ["relevance", "quality", "popular", "updated", "new"],
                "default": "relevance",
                "description": "並び順。",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RESULTS,
                "default": 10,
                "description": "返す作品数。負荷を抑えるため最大20件。",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_OFFSET,
                "default": 0,
                "description": "検索結果の開始位置。大量取得には使わない。",
            },
        },
    }


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name}はオブジェクトで指定してください")
    return value


def _string_arg(args: dict[str, Any], name: str, *, default: str = "", max_length: int) -> str:
    value = args.get(name, default)
    if not isinstance(value, str):
        raise ValueError(f"{name}は文字列で指定してください")
    if len(value) > max_length:
        raise ValueError(f"{name}は{max_length}文字以内で指定してください")
    return value.strip()


def _int_arg(
    args: dict[str, Any],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name}は整数で指定してください")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}は{minimum}以上{maximum}以下で指定してください")
    return value


def _optional_int_arg(
    args: dict[str, Any], name: str, *, minimum: int, maximum: int
) -> int | None:
    if name not in args or args[name] is None:
        return None
    return _int_arg(args, name, default=0, minimum=minimum, maximum=maximum)


def _bool_arg(args: dict[str, Any], name: str, *, default: bool) -> bool:
    value = args.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name}はtrueまたはfalseで指定してください")
    return value


def _enum_arg(args: dict[str, Any], name: str, *, default: str) -> str:
    value = args.get(name, default)
    if not isinstance(value, str) or value not in _ENUMS[name]:
        choices = ", ".join(sorted(_ENUMS[name]))
        raise ValueError(f"{name}は{choices}のいずれかで指定してください")
    return value


def _list_arg(
    args: dict[str, Any],
    name: str,
    *,
    max_items: int,
    item_kind: type,
    item_max_length: int | None = None,
) -> list[Any]:
    value = args.get(name, [])
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError(f"{name}は{max_items}件以内の配列で指定してください")
    for item in value:
        if isinstance(item, bool) or not isinstance(item, item_kind):
            raise ValueError(f"{name}の値の型が正しくありません")
        if item_max_length is not None and len(item) > item_max_length:
            raise ValueError(f"{name}の各値は{item_max_length}文字以内で指定してください")
    return value


def _build_request(args: dict[str, Any]) -> dict[str, Any]:
    query = _string_arg(args, "query", max_length=MAX_QUERY_LENGTH)
    filters: dict[str, Any] = {
        "status": _enum_arg(args, "status", default="all"),
        "length_bucket": _enum_arg(args, "length_bucket", default="all"),
        "genres": _list_arg(args, "genres", max_items=MAX_GENRES, item_kind=int),
        "tags": _list_arg(
            args,
            "tags",
            max_items=MAX_TAGS,
            item_kind=str,
            item_max_length=MAX_TAG_LENGTH,
        ),
        "exclude_words": _list_arg(
            args,
            "exclude_words",
            max_items=MAX_EXCLUDE_WORDS,
            item_kind=str,
            item_max_length=MAX_EXCLUDE_WORD_LENGTH,
        ),
        "include_unrated": _bool_arg(args, "include_unrated", default=False),
        "include_stalled": _bool_arg(args, "include_stalled", default=True),
        "sort": _enum_arg(args, "sort", default="relevance"),
    }
    for name in (
        "biggenre",
        "genre",
        "has_book",
        "has_manga",
        "has_anime",
        "is_r15",
    ):
        if name in args and args[name] is not None:
            if name in {"biggenre", "genre"}:
                filters[name] = _int_arg(
                    args, name, default=0, minimum=0, maximum=100000
                )
            else:
                filters[name] = _bool_arg(args, name, default=False)
    min_rating_count = _optional_int_arg(
        args, "min_rating_count", minimum=0, maximum=1_000_000
    )
    if min_rating_count is not None:
        filters["min_rating_count"] = min_rating_count
    return {
        "query": query,
        "filters": filters,
        "limit": _int_arg(args, "limit", default=10, minimum=1, maximum=MAX_RESULTS),
        "offset": _int_arg(args, "offset", default=0, minimum=0, maximum=MAX_OFFSET),
    }


def _api_url() -> str:
    value = os.environ.get("NAROU_MATCH_API_URL", DEFAULT_API_URL).strip()
    if not value.startswith(("https://", "http://")):
        raise RuntimeError("NAROU_MATCH_API_URLはhttp://またはhttps://で始めてください")
    return value


def _fetch(request_body: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        _api_url(),
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "narou-match-agent/0.1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_OUTPUT_BYTES + 1)
    except HTTPError as exc:
        LOGGER.warning("Narou Match API returned HTTP %s", exc.code)
        raise RuntimeError("Narou Match APIから検索結果を取得できませんでした") from exc
    except (URLError, TimeoutError, HttpClientException, OSError) as exc:
        LOGGER.warning("Narou Match API request failed: %s", type(exc).__name__)
        raise RuntimeError("Narou Match APIへの接続に失敗しました") from exc
    if len(raw) > MAX_OUTPUT_BYTES:
        raise RuntimeError("Narou Match APIの応答が大きすぎます")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Narou Match APIの応答形式が不正です") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise RuntimeError("Narou Match APIの応答形式が不正です")
    return payload


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else str(value or "")
    return text if len(text) <= limit else f"{text[:limit]}…"


def _public_result(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    fields = (
        "ncode",
        "title",
        "writer",
        "biggenre",
        "genre",
        "novel_type",
        "is_completed",
        "episode_count",
        "length",
        "length_bucket",
        "reading_time_minutes",
        "first_published_at",
        "last_published_at",
        "is_long_term_stopped",
        "is_r15",
        "global_point",
        "bookmark_count",
        "rating_points",
        "rating_count",
        "rating_average",
        "illustration_count",
        "score",
        "quality_label",
        "is_book_published",
        "is_manga_adapted",
        "is_anime_adapted",
        "novel_match_series_url",
        "url",
    )
    result = {key: item.get(key) for key in fields if key in item}
    result["story"] = _clip(item.get("story"), MAX_SYNOPSIS_LENGTH)
    result["keyword"] = _clip(item.get("keyword"), MAX_KEYWORD_LENGTH)
    return result


def _public_response(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": payload.get("source", "narou_official_api"),
        "query": payload.get("query", ""),
        "total": payload.get("total", 0),
        "filtered_total": payload.get("filtered_total"),
        "notice": payload.get("notice"),
        "results": [_public_result(item) for item in payload.get("results", [])],
    }


def _search(args: Any) -> dict[str, Any]:
    request_body = _build_request(_require_object(args, "arguments"))
    return _public_response(_fetch(request_body))


def _jsonrpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _handle(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request")
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        # Notifications, including initialized, do not receive a response.
        return None
    if method == "initialize":
        return _jsonrpc_result(
            request_id,
            {
                "protocolVersion": message.get("params", {}).get(
                    "protocolVersion", "2024-11-05"
                ),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "narou-match",
                    "title": "Narou Match",
                    "version": "0.1.0",
                },
                "instructions": (
                    "小説を読もう！の作品検索にsearch_narouを使用してください。"
                    "返却データは公式APIの作品情報です。"
                ),
            },
        )
    if method == "ping":
        return _jsonrpc_result(request_id, {})
    if method == "tools/list":
        return _jsonrpc_result(
            request_id,
            {
                "tools": [
                    {
                        "name": TOOL_NAME,
                        "title": "小説を読もう！を検索",
                        "description": TOOL_DESCRIPTION,
                        "inputSchema": _tool_schema(),
                    }
                ]
            },
        )
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
            return _jsonrpc_result(request_id, _error("未知のツールです"))
        try:
            response = _search(params.get("arguments", {}))
            encoded = json.dumps(response, ensure_ascii=False)
            if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
                raise RuntimeError("検索結果が大きすぎます。limitを小さくしてください")
            return _jsonrpc_result(
                request_id,
                {
                    "content": [{"type": "text", "text": encoded}],
                    "structuredContent": response,
                    "isError": False,
                },
            )
        except (RuntimeError, ValueError) as exc:
            return _jsonrpc_result(request_id, _error(str(exc)))
        except Exception:
            LOGGER.exception("Unexpected search failure")
            return _jsonrpc_result(request_id, _error("検索処理で予期しないエラーが発生しました"))
    return _jsonrpc_error(request_id, -32601, f"Method not found: {method}")


def main() -> None:
    """Run the MCP JSON-RPC server over stdin/stdout."""

    logging.basicConfig(stream=sys.stderr, level=os.environ.get("LOG_LEVEL", "WARNING"))
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            response = _handle(message)
        except json.JSONDecodeError:
            response = _jsonrpc_error(None, -32700, "Parse error")
        except Exception:
            LOGGER.exception("Failed to handle MCP message")
            response = _jsonrpc_error(None, -32603, "Internal error")
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
