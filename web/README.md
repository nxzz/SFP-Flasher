# SFP Flash Web

ブラウザ（WebHID）から RP2040(Pico) I2C ゲートウェイ経由で SFP の EEPROM を扱う Web アプリ。
スマートフォンにも対応しています。`device/index.html` の WebHID ⇄ I2C プロトコルがベースです。

## 機能

1. **ダンプ / 解析** — SFP(0x50)から 128B を読み、SFF-8472 として解析表示し、`.bin` で保存（`dump.sh` 相当）
2. **クローン書込（実 SN 維持）** — プリセット or アップロードした `.bin` でモジュールを上書きしつつ、
   そのモジュール本来のシリアル番号(0x44–0x53)を維持。チェックサム自動再計算・アンロック・ベリファイ込み（`modsn.sh` 相当）
   - **自動連続書込** — 挿入待ち→書込→抜去待ちを反復し、差し替えるだけで連続クローン（`loop.sh` 相当）
3. **DWDM 波長設定** — チューナブル SFP の ITU グリッドチャンネルを設定（`set-dwdm-ch.py` 相当）
4. **Avago ISP 書換** — `device-avago-isp` ボード（FW v2.x）で Avago/Broadcom 内蔵 ATmega328 を
   ISP(SPI) 経由で直接読み書きする専用タブ（下記参照）

## Avago ISP 書換（device-avago-isp 専用ボード）

Avago/Broadcom 系モジュールは内部 ATmega が EEPROM を読取専用にエミュレートしており、I2C(0x50)からは
書き込めません（隠し mailbox もプログラミングゲートも存在しない個体がある）。そこで本タブは、
`device-avago-isp` ボード（FW v2.x）を使い、内蔵 ATmega328 の EEPROM を **ISP(SPI シリアルプログラミング)** で
**直接**書き換えます。

- **必須**: 専用ファーム `device-avago-isp`（FW v2.x）＋専用配線
  - MOSI=pin4/GP4, SCK=pin5/GP5, MISO=pin9/GP9, /RESET=pin15/GP15, VCC=pin16/3V3, GND=pin1
- HID コマンド: `0x10` ISP_BEGIN（RESET Low→Programming Enable, 応答=signature） /
  `0x11` ISP_XFER（4バイト ISP 命令を全二重転送） / `0x12` ISP_END（RESET 解放→I2C 復帰）
- タブの操作: ① ISP 接続/デバイス確認（signature・lock・fuse） / ② EEPROM 1KB バックアップ /
  ③ ファーム(フラッシュ)32KB ダンプ（読み出しのみ） /
  ④ A0 クローン書込（内部 EEPROM `0x300–0x37F`, 実SN維持・checksum再計算・verify）
- 書込元像の選び方（プリセット / アップロード / 別バスコピー）は **クローン書込タブと同じ操作感**で、
  ISP タブ内に独立して用意してあります。別バスコピーは I2C を使うので **ISP 開始前**に実行してください。
- 内部 EEPROM レイアウト: `0x300–0x37F` = A0(0x50) ページ（実測確定）
- **Chip Erase は使いません**（フラッシュ＝ファームを消さない）。書込前に必ず ② でバックアップを取得。
  ホスト側 CLI 版は `tools/avr_isp.py`、設計詳細は `AVAGO_ISP_DESIGN.md` を参照。

## プリセットの定義（presets.json）

書き込み用 EEPROM 像は [`presets.json`](presets.json) で定義します。`bin/` 配下に `.bin` を置き、
エントリを追記するだけで選択肢が増えます（実 SN は書込時に自動維持されるため、ファイルの SN は問いません）。

```json
{
  "presets": [
    { "name": "Finisar FTLX8571D3BCL (10G SR)", "file": "bin/FTLX8571D3BCL.bin", "note": "10G 850nm SR" }
  ]
}
```

> プリセットは `fetch` で読み込むため `https://` / `http://localhost` が必要です。
> `file://` で直接開いた場合は「アップロード」を使うか、ローカルサーバを起動してください。

## 動作要件

- **Chrome / Edge など Chromium 系ブラウザ**（WebHID 対応。iOS Safari は非対応）
- RP2040(Pico) ゲートウェイ（ファーム `device/sfp-gateway.uf2`、プロトコル `device/USB_PROTOCOL.md`）

## ローカル実行

```bash
npx serve .
#  → 表示された http://localhost:xxxx を Chrome で開く
```

## Firebase Hosting へのデプロイ

このディレクトリ（`web/`）がそのままデプロイ単位です。

```bash
npm install -g firebase-tools
firebase login
# .firebaserc の "YOUR_FIREBASE_PROJECT_ID" を実プロジェクト ID に置換してから:
cd web
firebase deploy --only hosting
```

## 注意

- クローン書込は EEPROM を上書きします。先に「ダンプ / 解析」でバックアップを取得してください。
- WRITE は 1 回最大 56 バイト、8 バイトページ境界を跨がないよう自動分割します（24Cxx 系 EEPROM 対策）。
