# radio-downloader

NHKのラジオ番組をHLSストリームからffmpegで保存するための非同期ツールです。番組の放送予定をNHKの公開APIから取得し、指定した番組シリーズの放送開始時刻に合わせてffmpegの実行を予約します。

## 必要要件

- Python 3.11 以降
- ffmpeg

依存パッケージは `pip install -r requirements.txt` でインストールできます。

## 使い方

```
python -m radio_downloader <シリーズID> [オプション]
```

### 主なオプション

| オプション | 説明 | 既定値 |
| --- | --- | --- |
| `--area` | 放送地域のキーまたはスラッグ。例: `130` (東京), `osaka` | `130` |
| `--output-dir` | 録音ファイルの保存先ディレクトリ | `recordings` |
| `--lead-in` | 放送開始前に録音を開始する秒数 | `60` |
| `--tail-out` | 放送終了後も録音を継続する秒数 | `120` |
| `--default-duration` | 放送終了時刻が取得できない場合に使う分単位の長さ | 指定なし |
| `--max-events` | 今後の放送予定から予約する件数 | `1` |
| `--start-after` | 指定したISO形式時刻より前に始まる番組を除外 | 指定なし |
| `--dry-run` | ffmpegを実行せずに予約内容のみ表示 | - |
| `--verbose` | 詳細ログを表示 | - |
| `--poll-interval` | 放送予定の再取得を行う間隔（秒） | `900` |

### 例

次回放送予定の「ベストオブクラシック」（シリーズID: `Z9L1V2M24L`）を東京エリアのFMで録音し、開始60秒前から終了後3分まで保存する例:

```
python -m radio_downloader Z9L1V2M24L --area tokyo --lead-in 60 --tail-out 180
```

録音ファイルは `recordings` ディレクトリに `YYYYMMDDTHHMMSSZ_タイトル.m4a` 形式で保存されます。`--dry-run` を付けるとffmpegは起動せず計画のみを表示します。スケジューラは既定で15分ごとに放送予定を再取得し、新しい番組が見つかれば自動的に予約へ追加します。
