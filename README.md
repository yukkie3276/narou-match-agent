# Narou Match Agent

小説を読もう！の作品を探すための、ローカルMCPサーバーとAgent Skillです。

利用者が「完結済みの長編ファンタジー」「主人公が成長する作品。評価の少ない作品は避けたい」のように伝えると、接続先のAIが希望を検索語と条件に変換し、Narou Matchの公開検索APIから作品情報を取得します。

このパッケージは、検索結果をAIに学習させるものではありません。小説本文は取得せず、公式APIの作品情報（タイトル、作者、あらすじ、キーワード、評価、状態など）だけを検索します。サーバー側でLLM、embedding、データベースへの作品カタログ保存は行いません。

## セットアップ

Python 3.10以上が必要です。依存パッケージはありません。

```bash
git clone https://github.com/yukkie3276/narou-match-agent.git
cd narou-match-agent
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

## MCPクライアントへの登録

インストール後の実行ファイルは `.venv/bin/narou-match-mcp` です。クライアントから標準入出力のMCPサーバーとして起動してください。

Claude Desktopの設定例（`claude_desktop_config.json`）:

```json
{
  "mcpServers": {
    "narou-match": {
      "command": "/absolute/path/narou-match-agent/.venv/bin/narou-match-mcp"
    }
  }
}
```

Claude Codeの例:

```bash
claude mcp add narou-match -- /absolute/path/narou-match-agent/.venv/bin/narou-match-mcp
```

VS Codeの `.vscode/mcp.json` の例:

```json
{
  "servers": {
    "narou-match": {
      "type": "stdio",
      "command": "/absolute/path/narou-match-agent/.venv/bin/narou-match-mcp"
    }
  }
}
```

その他のMCP対応クライアントでは、次のコマンドをstdioサーバーとして登録してください。

```text
/absolute/path/narou-match-agent/.venv/bin/narou-match-mcp
```

設定後、AIに「小説を読もう！から、完結済みの長編ファンタジーを探して」と依頼すると、`search_narou` ツールが候補を検索します。クライアントがAgent Skillを読み込める場合は、`skills/narou-match-search/SKILL.md` をそのクライアントのSkillディレクトリへコピーしてください。

検索条件の整理は、接続した外部AIが担当します。小説家になろう公式APIの`word`は空白区切りのAND検索なので、AIは利用者の自然文から作品名・あらすじ・キーワードに現れそうな、文章に明示された検索語を全て保持します。検索語が多い場合も明示条件を削らず、正の検索語へ同義語・関連語・未指定の条件を追加しません。MCPの`query`は最大200文字です。肯定検索語とは異なり、否定条件は意味を解釈して確度の高い関連除外語へ慎重に広げます。[公式APIの仕様](https://dev.syosetu.com/man/api/)に従い、必要ならタグやジャンルを併用します。「俺つええ系以外で男主人公が着実に強くなる物語」なら、`男主人公`と`成長`を検索語にし、文脈に応じて`俺TUEEE`、`チート`、`無双`、`主人公最強`、`無敵`などを`exclude_words`へ設定します。MCPサーバー自体はLLMを呼び出さず、検索条件をそのまま公開APIへ渡します。

除外語はNarou公式APIの `notword` として検索され、作品名・あらすじ・キーワードなどへの部分一致で除外されます。そのため「チート」を指定すると「チートなし」や「知識チート」も除外される場合があります。否定されたトロープの関連語は慎重に複数へ広げ、結果が少ない場合は除外語を減らしてください。正の中心語を変える別検索が必要な場合も、呼び出しは最大2〜3回に抑え、`ncode`で重複を除きます。

## 直接実行

インストールせずに、リポジトリのルートから次のように実行することもできます。

```bash
PYTHONPATH=src python -m narou_match_mcp.server
```

標準入力と標準出力はMCPのJSON-RPC通信に使うため、ログは標準エラー出力へ出ます。

## API接続先の変更

既定の接続先は `https://api.novel-match.com/narou/search` です。開発用または自分で運用する互換APIを使う場合だけ、環境変数で変更できます。

```bash
export NAROU_MATCH_API_URL="https://api.example.com/narou/search"
```

APIキーは要求されません。接続先が公開APIであることと、接続先の利用条件を確認して使ってください。MCPサーバーは一回の検索で最大20件、12秒のタイムアウトを設定しています。検索APIには1分あたり20回の利用制限があるため、同じ条件での連続検索や自動巡回、大量のページ取得には使わないでください。

## 公開時の注意

GitHubに公開しても、AIサービスが自動的に見つけて利用するわけではありません。利用者がMCPサーバーをクライアントへ登録し、対応クライアントでSkillを読み込む必要があります。OpenAIの公式ディレクトリへの申請や、リモートMCPサーバーの公開はこのパッケージの範囲に含みません。

## English summary

This repository provides a dependency-free local MCP server and an Agent Skill for searching Narou Match. The server exposes one read-only `search_narou` tool, calls `https://api.novel-match.com/narou/search`, and returns metadata from the official Narou search flow. It does not fetch novel text, run an LLM, train a model, or store a bulk catalogue. Configure the executable as a stdio MCP server in a compatible client, then copy the Skill directory if the client supports Agent Skills.

## License

MIT License. See [LICENSE](LICENSE).
