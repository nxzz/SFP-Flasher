# sfp-eerom-web

ブラウザ（WebHID）から RP2040(Pico) I2C ゲートウェイ経由で SFP モジュールの EEPROM を読み書きするツール一式。
ダンプ／解析、クローン書込（実シリアル番号を維持）、DWDM 波長設定をブラウザだけで行えます。

```
┌────────────┐   WebHID    ┌──────────────┐    I2C     ┌──────────────┐
│  ブラウザ   │ ─ 64B HID ─ │ RP2040(Pico) │ ─ 0x50/   │  SFP module  │
│ (web/)     │   レポート   │  gateway     │   0x51 ── │  EEPROM      │
└────────────┘             └──────────────┘            └──────────────┘
```

## 構成

| ディレクトリ | 内容 |
|---|---|
| [`web/`](web/) | WebHID Web アプリ（Firebase Hosting にデプロイ）。ダンプ／クローン書込／DWDM 設定。詳細は [`web/README.md`](web/README.md) |
| [`device-src/`](device-src/) | RP2040(Pico) ゲートウェイのファームウェア（PlatformIO / arduino-pico コア） |

## WebHID ⇄ I2C プロトコル

64 バイト固定の HID IN/OUT レポートでコマンドをやり取りする汎用 I2C ゲートウェイです。
2 系統の I2C バスに対応します（bus0 = SDA/SCL = IO12/IO13、bus1 = IO26/IO27）。

OUT レポート（ブラウザ → デバイス, 64B）:

| オフセット | 内容 |
|---|---|
| `[0]` seq | 任意のシーケンス番号（応答にそのままエコー） |
| `[1]` cmd | `0x00` PING / `0x01` READ / `0x02` WRITE / `0x03` SCAN |
| `[2]` bus | 0 or 1 |
| `[3]` addr | 7bit I2C アドレス |
| `[4]` reg | レジスタ／オフセット（READ/WRITE 開始位置） |
| `[5]` len | READ=読み出しバイト数 / WRITE=書き込みバイト数 |
| `[6..]` | WRITE データ |

IN レポート（デバイス → ブラウザ, 64B）:

| オフセット | 内容 |
|---|---|
| `[0]` seq | 要求の seq をエコー |
| `[1]` status | `0x00` OK / `0x01` NACK / `0x02` BADARG / `0x03` TOOBIG |
| `[2]` len | 有効データ長 |
| `[3..]` | データ（READ=読み出し値 / SCAN=見つかったアドレス列 / PING=版数） |

- READ は 1 回最大 61 バイト、WRITE は 1 回最大 58 バイト。
- WRITE は 8 バイトページ境界を跨がないよう Web アプリ側で自動分割します（24Cxx 系 EEPROM 対策）。

## デバイスのビルド／書き込み

[PlatformIO](https://platformio.org/) を使用します（arduino-pico / Earle Philhower コア、USB スタックは Adafruit TinyUSB）。

```bash
cd device-src
pio run                 # ビルド
pio run --target upload  # Pico へ書き込み（BOOTSEL でマウント）
```

## Web アプリの実行

```bash
cd web
npx serve .
#  → 表示された http://localhost:xxxx を Chrome / Edge で開く
```

デプロイ手順や機能の詳細は [`web/README.md`](web/README.md) を参照してください。

## 動作要件

- **Chrome / Edge など Chromium 系ブラウザ**（WebHID 対応。iOS Safari は非対応。Android Chrome は対応）
- RP2040(Pico) ゲートウェイ（`device-src/` のファームウェアを書き込んだもの）

## 注意

クローン書込は SFP の EEPROM を上書きします。先に「ダンプ／解析」でバックアップを取得してください。
