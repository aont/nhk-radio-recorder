# radio-downloader

NHKラジオの放送予定JSONとHLSストリームを利用して、ffmpegで自動録音するためのスクリプトです。asyncioと`aiohttp`を用いて非同期で複数番組を予約できます。

## 主な特徴

- NHKが公開している`config_web.xml`からエリアごとのHLS URLを自動取得
- 放送予定（BroadcastEvent）JSONを柔軟に解析し、番組情報を抽出
- 録音失敗時は自動的にMP3へフォールバックエンコード
- `--dry-run`で予約内容だけを確認可能

## 必要環境

- Python 3.10 以上
- `ffmpeg` コマンドが利用可能であること

## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate  # Windowsでは .venv\Scripts\activate
pip install -r requirements.txt
```

`sleep-absolute` パッケージは対応環境での高精度スリープを提供しますが、未インストールでも動作します。

## 使い方

```bash
python main.py \
  --event-url "https://example.com/schedule.json" \
  --area tokyo \
  --service r2 \
  --outdir ./recordings
```

主なオプション:

- `--event-url` (複数可): 放送予定JSONのURL。
- `--area`: config_web.xmlの`<area>`値。例: `tokyo`, `osaka`。
- `--service`: `r1`, `r2`, `fm` のいずれか。JSONから判別できない場合に指定。
- `--variant`: `auto`, `master`, `master48k`のいずれか。HLSプレイリストのバリアント選択。
- `--dry-run`: 録音せず予約内容のみ表示。

詳細は `python main.py --help` で確認できます。

## ディレクトリ構成

```
.
├── README.md
├── requirements.txt
├── main.py
├── docs/
│   └── memo.md
└── src/
    └── radio_downloader/
        ├── __init__.py
        ├── cli.py
        ├── events.py
        ├── ffmpeg.py
        ├── hls.py
        ├── models.py
        ├── recorder.py
        └── timing.py
```

## 開発メモ

補足資料やアイデアは `docs/memo.md` にまとめています。
