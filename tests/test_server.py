from narou_match_mcp.server import TOOL_DESCRIPTION, _build_request, _tool_schema


def test_build_request_serializes_exclude_words_for_search_api() -> None:
    request = _build_request(
        {
            "query": "男主人公 成長",
            "exclude_words": ["チート", "無双", "俺TUEEE"],
        }
    )

    assert request["query"] == "男主人公 成長"
    assert request["filters"]["exclude_words"] == ["チート", "無双", "俺TUEEE"]


def test_tool_guides_external_llm_to_expand_semantic_preferences() -> None:
    schema = _tool_schema()

    assert "外部のLLM" in TOOL_DESCRIPTION
    assert "LLMを呼び出しません" in TOOL_DESCRIPTION
    assert "自然文の意味" in schema["properties"]["query"]["description"]
    assert "明示された検索語を全て保持" in schema["properties"]["query"]["description"]
    assert "同義語・関連語・未指定の条件" in schema["properties"]["query"]["description"]
    assert "同義語・関連語・未指定の条件" in schema["properties"]["query"]["description"]
    assert "否定的な希望" in schema["properties"]["exclude_words"]["description"]
    assert "俺TUEEE" in schema["properties"]["exclude_words"]["description"]
