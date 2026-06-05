# SFP Flash Web

ブラウザ（WebHID）から RP2040(Pico) I2C ゲートウェイ経由で SFP の EEPROM を扱う Web アプリ。
スマートフォンにも対応しています。`device/index.html` の WebHID ⇄ I2C プロトコルがベースです。

## 機能

1. **ダンプ / 解析** — SFP(0x50)から 128B を読み、SFF-8472 として解析表示し、`.bin` で保存（`dump.sh` 相当）
2. **クローン書込（実 SN 維持）** — プリセット or アップロードした `.bin` でモジュールを上書きしつつ、
   そのモジュール本来のシリアル番号(0x44–0x53)を維持。チェックサム自動再計算・アンロック・ベリファイ込み（`modsn.sh` 相当）
   - **自動連続書込** — 挿入待ち→書込→抜去待ちを反復し、差し替えるだけで連続クローン（`loop.sh` 相当）
3. **DWDM 波長設定** — チューナブル SFP の ITU グリッドチャンネルを設定（`set-dwdm-ch.py` 相当）
4. **Avago Mailbox（検証用）** — Avago/Broadcom 系の hidden mailbox（B0 ページ = `0x58`）経由で
   内部 EEPROM を読み書きする低レベル検証ツール（下記参照）

## Avago 隠し Mailbox（検証用タブ）

Avago/Broadcom 系モジュールは通常の `0x51` アンロックとは別経路で、hidden mailbox 経由で内部
EEPROM を書き換えます。本タブはその手順を GUI から検証するためのものです。

- アドレス（7bit）: A2 = `0x51` / B0 = `0x58` / B4 = `0x5A`
- **前提**: 内部 EEPROM `0x01FF == 0x55`。ファームが起動時に読み `0x0336` に入れ、`0x55` のときだけ
  hidden command handler を有効化します。外部から `0x58` が見えない個体はホストから叩けません。
- コマンド mailbox: `B0:0x76`=command / `0x77`=addr_hi / `0x78`=addr_lo / `0x79`=value・result / `0x7A`=status
- コマンド: `0x51` enable / `0x52` 1byte read / `0x53` 1byte write / `0x5B` EEPROM 128B→`A2:0x80–0xFF` /
  `0x5C` `A2:0x80–0xFF`→EEPROM 128B / `0x50` disable
- status: `0x00` 成功 / `0xFF` エラー / `0xFE` I2C 下位失敗

> mailbox コマンドは `0x76` から 5 バイトを **1 トランザクション**で送る必要があります
> （`0x78` のページ境界を跨ぐため通常のページ分割書込は使えず、本タブは分割しない専用 WRITE を使用）。

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
