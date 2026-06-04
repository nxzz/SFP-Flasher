# SFP EEPROM ゲートウェイ USB / WebHID 仕様書

ブラウザ（WebHID）から Raspberry Pi Pico (RP2040) を経由して、SFP の
I2C EEPROM を読み書きするためのプロトコル仕様。

- ファーム: `c:\Users\netwing\Documents\PlatformIO\Projects\sfp-eeprom-web\src\main.cpp`
- フロント: `index.html`（同フォルダ）
- USB スタック: Adafruit TinyUSB（`-DUSE_TINYUSB`）

---

## 1. USB デバイス構成

複合デバイス（Composite）。

| インタフェース | 用途 |
|----------------|------|
| **HID**（vendor-defined） | コマンド／応答チャネル（本仕様の対象） |
| **CDC**（仮想シリアル） | デバッグログ出力（115200 bps、任意） |

### HID ディスクリプタ

- レポートディスクリプタ: `TUD_HID_REPORT_DESC_GENERIC_INOUT(64)`
- **Usage Page: `0xFF00`**（ベンダ定義）, Usage: `0x01`
- **Report ID なし**（= レポート ID `0`）
- **Input レポート 64 バイト固定**（デバイス → ホスト）
- **Output レポート 64 バイト固定**（ホスト → デバイス）
- Poll interval: 2 ms

### WebHID からの接続

Usage Page でフィルタするため VID/PID に依存しない。

```js
const devices = await navigator.hid.requestDevice({
  filters: [{ usagePage: 0xFF00 }]
});
const device = devices[0];
await device.open();
device.addEventListener("inputreport", onInputReport);
// 送信は reportId = 0、64バイト固定
await device.sendReport(0, new Uint8Array(64));
```

> WebHID は secure context が必要。`http://localhost` または `https://`、
> および Chrome では `file://` でも利用可能。

---

## 2. レポートフォーマット

すべて 64 バイト固定。未使用領域は `0` 埋め。

### 2.1 OUT（要求）ホスト → デバイス

| オフセット | 名前 | 説明 |
|-----------|------|------|
| 0 | `seq`  | シーケンス番号。応答にそのまま返る（要求と応答の対応付けに使用） |
| 1 | `cmd`  | コマンド（§3） |
| 2 | `bus`  | I2C バス: `0` or `1`（§4） |
| 3 | `addr` | 7bit I2C アドレス（例: SFP は `0x50` / `0x51`） |
| 4 | `reg`  | レジスタ／オフセット（READ/WRITE の開始位置、0–255） |
| 5 | `len`  | データ長（READ=読み出し数 / WRITE=書き込み数） |
| 6–63 | `data` | WRITE データ（最大 58 バイト） |

### 2.2 IN（応答）デバイス → ホスト

| オフセット | 名前 | 説明 |
|-----------|------|------|
| 0 | `seq`    | 要求の `seq` をエコー |
| 1 | `status` | ステータス（§5） |
| 2 | `len`    | 有効データ長 |
| 3–63 | `data` | データ（READ=読み値 / SCAN=検出アドレス列 / PING=版数） |

---

## 3. コマンド

| `cmd` | 名前 | 動作 | 応答 `data` |
|-------|------|------|-------------|
| `0x00` | **PING**  | 疎通確認 | `[0]`=FW major, `[1]`=FW minor（`len`=2） |
| `0x01` | **READ**  | `addr` の `reg` から `len` バイト読む（reg ポインタ書込→read） | 読み出しバイト列（`len` バイト） |
| `0x02` | **WRITE** | `addr` の `reg` から `data` を `len` バイト書く | なし（`len`=0） |
| `0x03` | **SCAN**  | `bus` 上の `0x08`–`0x77` を走査 | 検出アドレス列（`len`=件数） |

### 動作詳細

- **READ**: `beginTransmission(addr) → write(reg) → endTransmission(STOP) → requestFrom(addr,len)`。
  指定数を取得できなければ `NACK`。
- **WRITE**: `beginTransmission(addr) → write(reg) → write(data...) → endTransmission(STOP)`。
  完了後 **6 ms** 待つ（EEPROM 書き込みサイクル対策。レジスタ系デバイスには無害）。
- **SCAN**: 各アドレスへ空トランザクションを送り、ACK が返ったものを列挙。

---

## 4. バス割り当て（RP2040）

| `bus` | ペリフェラル | コード | SDA | SCL |
|-------|--------------|--------|-----|-----|
| `0` | i2c0 | `Wire`  | GPIO12 | GPIO13 |
| `1` | i2c1 | `Wire1` | GPIO26 | GPIO27 |

クロック: 両バスとも 100 kHz。

---

## 5. ステータスコード

| 値 | 名前 | 意味 |
|----|------|------|
| `0x00` | OK | 成功 |
| `0x01` | NACK | デバイス無応答 / 期待バイト数に届かず |
| `0x02` | BADARG | 不正な引数（例: `bus > 1`） |
| `0x03` | TOOBIG | `len` が上限超過 |

---

## 6. サイズ上限と分割

| 項目 | 値 | 由来 |
|------|-----|------|
| 1 回の READ 最大 | **61 バイト** | `64 − 3`（応答ヘッダ） |
| 1 回の WRITE 最大 | **58 バイト** | `64 − 6`（要求ヘッダ） |

フロント側（`index.html`）の実装上限と分割方針:

- READ: **60 バイト/回**でチャンク分割し、`reg` を進めて連結。
- WRITE: **56 バイト/回** かつ **8 バイトページ境界**を跨がないよう分割
  （24Cxx 系 EEPROM のページ書き込み対策）。

`reg` は 8bit のため **オフセットは 0–255**。SFP の 256 バイトページ内で完結する。
A0h（0x50）/ A2h（0x51）のページ切替が要る場合はアドレス自体を変える。

---

## 7. 通信シーケンス例

### PING
```
OUT: seq=01 cmd=00 ...
IN : seq=01 status=00 len=02 data=[01 00]   // FW v1.0
```

### SFP 基本情報を 16 バイト読む（0x50, offset 0x14 = Vendor name 先頭）
```
OUT: seq=02 cmd=01 bus=00 addr=50 reg=14 len=10
IN : seq=02 status=00 len=10 data=[... ASCII ...]
```

### 1 バイト書き込み（0x50, offset 0x7F に 0xAB）
```
OUT: seq=03 cmd=02 bus=00 addr=50 reg=7F len=01 data=[AB]
IN : seq=03 status=00 len=00
```

### バススキャン（bus0）
```
OUT: seq=04 cmd=03 bus=00 ...
IN : seq=04 status=00 len=02 data=[50 51]    // 0x50, 0x51 検出
```

---

## 8. 注意事項

- ホストは **1 要求 → 1 応答** を直列に行うこと。ファームは応答未送信中の新規
  OUT レポートを無視する（単一スロット）。
- 応答タイムアウトはフロント側で 2000 ms 推奨。
- SFP の EEPROM は書き込み保護領域・パスワード保護領域がある場合がある
  （A0h の一部、A2h の制御/パスワード領域など）。WRITE 失敗時は `NACK` が返る。
