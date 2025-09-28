`asyncio`で動く「NHKラジオのHLSをffmpegで録音予約する」ツールのメモです。放送予定（BroadcastEvent）JSONから番組情報を取得し、`config_web.xml`に記載されたHLSプレイリストを利用してffmpegで保存します。

---

## ざっくり仕様

* 入力：
  * **放送予定JSONのURL**（例：`https://api.nhk.jp/r7/f/broadcastevent/rs/Z9L1V2M24L.json`）
    → JSONから `start` / `end`（始終時刻）、タイトル等を抽出
  * **地域（area）**と**サービス（r1 / r2 / fm）**はJSONから推測できなければCLIで指定可能（デフォルトは `tokyo` & `r2`）
* HLS URL：
  * `https://www.nhk.or.jp/radio/config/config_web.xml` を取得して、各地域の `r1hls` / `r2hls` / `fmhls` を解析して使用。最近は `radio-stream.nhk.jp/.../master.m3u8` のマスタープレイリストが配布されています。必要なら `master48k.m3u8` に差し替え可能。([Zenn][1])
* スケジューリング：
  * `python-sleep-absolute` の `wait_until()` で**絶対時刻**まで非ブロッキング待機（Linux/Windows対応、他OSはフォールバックで`asyncio.sleep`）。([GitHub][2])
* 録音：
  * `ffmpeg` を `-c copy` で**無再エンコード**保存（`.m4a`）、必要に応じて `-bsf:a aac_adtstoasc` を付与。HLS録音に関するオプション例は技評記事が参考になります。([gihyo.jp][3])
* 複数イベントがJSONに含まれていれば全部並列予約（`asyncio`タスク）
* ネットワーク瞬断に備えて `-reconnect` 系オプションを付与
* `--prepad / --postpad` で前後余裕秒を加算
* `--dry-run` で予約内容だけ確認

> **注意**：録音した音声の権利はNHKにあります。**私的複製の範囲内**でご利用ください。([Zenn][1])

---

## 使い方メモ

```bash
# 依存パッケージを入れる（Python 3.11+ 推奨）
pip install -r requirements.txt

# 例: 放送予定JSON URLを指定して、東京のR2を録音
python main.py \
  --event-url "https://api.nhk.jp/r7/f/broadcastevent/rs/Z9L1V2M24L.json" \
  --area tokyo --service r2 \
  --outdir ./recordings --postpad 30 --prepad 5

# 予約内容だけ確認
python main.py \
  --event-url "..." --area tokyo --service r2 --dry-run
```

> 地域・サービスは放送予定JSONから判別できなかったときの**上書き用**です。
> HLSは `config_web.xml` から実際のURLを引きます（例：`.../nhkradiruakr2/master.m3u8`）。([Zenn][1])

---

## 実装ノート

現在は `src/radio_downloader/` 以下でモジュールごとに分割し、`main.py` からCLIを呼び出す構成にしています。主な役割は以下の通りです。

- `cli.py`: 引数解析と全体のフロー制御
- `events.py`: 放送予定JSONの解析
- `hls.py`: config_web.xmlの取得とHLS URL解決
- `recorder.py`: 録音タスク本体
- `ffmpeg.py`: ffmpegコマンドの組み立てと実行
- `timing.py`: 絶対時刻スリープのラッパー

元のワンファイル実装からの移植時に、主要な関数は役割ごとのモジュールへ移動させています。挙動は従来と同じになるように調整済みです。

[1]: https://zenn.dev/articles/nhk-radio-hls
[2]: https://github.com/aont/python-sleep-absolute
[3]: https://gihyo.jp/article/2020/ffmpeg-hls
