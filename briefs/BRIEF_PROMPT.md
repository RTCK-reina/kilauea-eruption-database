# キラウェア火山 日次HTMLブリーフ — 生成指示（データベース版）

このファイルは Cowork のスケジュールタスクに貼る指示文の原本である。
変更したらタスク側の prompt も差し替えること。

---

## 前提となる構成

数値は**すべてデータベース由来**であり、この指示を読む側は数値を取りに行かない。

1. **Mac の cron**（毎日 06:00 JST、`scripts/daily_update.sh`）
   `python3 -m kilauea update` で episodes / hans / quakes / park / hvo_forecast を差分収集し、
   `python3 -m kilauea brief-context --record` が SQL だけを引いて
   `briefs/context_latest.json` を書き、同時に `brief_run` テーブルへ1行記録する。
2. **このタスク（Cowork クラウドセッション）**
   その JSON を読み、文章とHTMLを書き、スクリーンショットで確認して送る。

分業の理由は、Cowork のデバイスVMにはネットワークがなく収集を実行できず、
クラウドコンテナには永続DBが無いためである。

---

## 手順

### 1. コンテキストの取得

デバイス `claude-book-pro-local` の `/Users/rtck/Developer/Kilauea/briefs/` を
`mcp__remote-devices__device_list_dir` で確認し、`context_latest.json` を
`device_stage_files` でステージして読む。

`generated.utc` が現在時刻から**36時間以上前**なら古い。その場合と、
デバイスが接続されていない場合は、フォールバックに進む。

### 2. フォールバック（Macが届かない、またはJSONが古いとき）

クラウドコンテナにはネットワークがあるので、自前で作る。

```
git はない。リポジトリは device_stage_files で kilauea/ 配下の .py と .sql、
scripts/、requirements.txt をステージするか、それも無理なら下記を直接叩く。

pip install requests --break-system-packages
python3 -m kilauea init
python3 -m kilauea collect episodes hans quakes park hvo_forecast --since <60日前>
python3 -m kilauea brief-context -o context.json
```

これで同じ形の JSON が得られる。
ブリーフ冒頭に「データベースはこのセッション内で再構築した」旨を1文入れる。
どちらも不可能なら、**推測でブリーフを作らず**、何が取得できなかったかだけを報告して終える。

### 3. HTMLを書く

JSON の中身だけを使って書く。以下は絶対に守る。

- **数値・日付・固有名は JSON にある値をそのまま使う。** JSON に無い数値は書かない。
- 値が `{"value": null, "unavailable": "..."}` の項目は、
  該当欄に **「未確認」** と書き、`unavailable` の理由を1文で添える。空欄にも推測にもしない。
- `source_notice.staleness_note` を冒頭の導入文に反映する。
  当日分の日次更新が未発表なら、基準にした通知の発表日時（`sent_hst_pretty`）を必ず書く。
- `generated.date_line` をそのまま日付行に使う。
- 時刻はすべて HST。JSON の `*_hst` / `*_hst_pretty` を使い、UTC は書かない。

### 4. スクリーンショットで確認

Playwright + Chromium（`executablePath:'/opt/pw-browsers/chromium'`）で
フルページを撮り、**画像を実際に見てから**送る。確認する点：

- 図の線が途切れていないか
- ラベル同士、ラベルと罫線が重なっていないか
- 文字が欠けていないか
- 幅420pxでカラムが縦積みになるか

### 5. 送付と保存

`SendUserFile` で HTML を届け、
`device_commit_files` で `/Users/rtck/Developer/Kilauea/briefs/kilauea_brief_YYYYMMDD.html`
にも書く（Macが届く場合）。

`brief_run` への記録は cron 側が済ませている。フォールバック経路で作った場合のみ、
`python3 -m kilauea brief-context --record` を実行してから DB を書き戻す
（書き戻せないなら記録は諦め、その旨を報告に書く）。

---

## 言語・文体

- 本文はすべて日本語、だ・である調。見出しも日本語。
- 観察して手渡す口調。命令形・励まし・謝罪・作業実況・お世辞は書かない。
- 数値は一次ソースの表記どおりに引用し、必ず「いつ時点の情報か」を書く。

## 取得項目と JSON の対応

| ブリーフの項目 | JSON のキー |
|---|---|
| 火山警戒レベル / 航空カラーコード | `alert.level` / `alert.color_code` |
| 噴火中か休止中か・エピソード番号 | `episode.state_label` / `episode.number` |
| 山頂傾斜計の積算値と直近24時間 | `tilt.cumulative_urad` / `tilt.change_24h_urad` / `tilt.station` |
| 山頂傾斜の日次系列（図の実測点） | `tilt_series.points`（HVOが公表した値のみ） |
| 風向・風速 | `wind`（HVOの記載。気象APIではない） |
| 直近の航空通報 | `vona_latest`（分精度のONSET、カラーコード遷移、噴煙の流向） |
| 傾斜計の停止 | `tilt.offline_stations` / `monitoring_outage` |
| 山頂直下の地震 | `earthquakes_summit_24h_hvo`（HVO記載）と `earthquakes_catalog`（ComCat実測） |
| SO2放出量 | `so2.measured`（実測）/ `so2.typical_range`（一般値） |
| 次エピソードの予測ウィンドウ | `hvo_forecast` |
| 直前エピソードの実績 | `episode.*`（開始・終了・継続・噴泉高・噴煙高・噴出量） |
| リフトゾーン | `rift_zones` |
| 公園の閉鎖・観覧 | `park.conditions` / `park.eruption_viewing` |
| 本ブリーフの推定 | `own_forecast` |

`earthquakes_summit_24h_hvo` と `earthquakes_catalog` は数が食い違うことがある。
HVO の言う summit area の方が広いためで、これは矛盾ではない。両方書き、
`earthquakes_catalog.note` の趣旨を1文添える。

`park.*.page_last_updated` はNPSのページが自称する最終更新日で、取得日とは別物である。
公園情報を書くときはこの日付を添える。

## 予測の書き方

`own_forecast` は毎日同じ規則で計算されている。次の3つを必ず書く。

- **推定ウィンドウ** — `window_start` 〜 `window_end`
- **推定噴火日（1日決め打ち）** — `point_date`
- **早期リスク日** — `early_risk_date`（傾斜の回復が続いた場合の早い側の目安）

主推定は `own_forecast.method`（通常は deflation model = 直前エピソードの収縮量から
休止時間を回帰）である。`own_forecast.backtest` にこの手法自身の実績
（平均絶対誤差・±2日以内の回数）が入っているので、推定を書くときに1行添える。
自分の手法の誤差を書かずに日付だけ出さないこと。

根拠は `own_forecast.rationale_ja` に日本語で入っている。そのまま並べず、要約して地の文にする。
`hvo_forecast_track_record` があれば HVO 自身の的中率と平均ウィンドウ幅に触れてよいが、
HVO は噴出の約45時間前に出しており、こちらはエピソード終了直後の約2週間前予測である。
リード時間が違うので優劣として並べない。

---

## レイアウト

上下2バンドの1枚もの。カード・バッジ・影・角丸・フッターは使わない。
`@font-face` は使わない。

**上バンド（背景 `#F9F9F7`）**

1. 日付行（`generated.date_line`、小さく `ink-soft`）
2. セリフ体の見出し1行（40px、`font-family:"Hiragino Mincho ProN","Yu Mincho",serif`）
3. SVGの図（`viewBox="0 0 840 190"`）
4. 3カラム（過去=直前エピソード / 現在=最新の傾斜と観測 / 予測=次のウィンドウ）。細い縦罫で区切る。

**SVGの図**

山頂傾斜の推移を1本の線で描く。`tilt_series.points` の3点が実測アンカーである
（エピソード開始=0、終了=収縮の底、最新観測）。急落・再膨張・最新観測点のドットを描き、
点と点の間は補間であることを図の下のキャプションに書く（`tilt_series.interpolation_note`）。
最新観測点より右に線を引かない。公表値が無いためである。

予測ウィンドウはクレー色 `#C6613F` の破線で示す。**クレー色は図中1箇所だけに使う。**
線は `#2E2C27`、補助は `#B4B3A8`、罫線は `#E4E3DC`。
`tilt_series.points` が空（`unavailable` あり）なら図は描かず、
その理由を1文で置く。線の無い枠だけを出さない。

**下バンド（背景 `#FCFCFB`）**

「要注目」「平常範囲」「立入と観覧」の3リスト。
各項目は 薄いグレーの連番 + 太字タイトル（20字以内）+ 1〜2文。
出典フレーズに `primary_sources` のURLでリンクを張る。

`episode.is_erupting` が true、または `hvo_forecast.window_start` が当日か翌日なら、
その項目を「要注目」の先頭に置く。

**レスポンシブ**

640px以下でカラムは縦積み。

---

## 禁止事項の確認（送る前に自分で見る）

- JSON に無い数値を書いていないか
- `unavailable` の項目を空欄や推測で埋めていないか
- UTC 表記が混ざっていないか
- クレー色が図の外に出ていないか
- カード・影・角丸・フッター・`@font-face` が入っていないか
