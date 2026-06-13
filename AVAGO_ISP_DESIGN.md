# Avago(AVR内蔵)SFP — Pico経由ISPでEEPROMを書き換える設計

## 0. 背景（検証済みの前提）

I2C(0x50/0x51)経由ではこの個体は書けない、と実機＋ファーム解析で確定済み：

- 対象モジュール: ラベル BROCADE 57-1000117-01 / OEM実体 `AFBR-57D9AMZ-BR1`（= Avago/Broadcom, **ATmega328内蔵**）
- 0x50への書込みは **status=OK でも値が変わらない**（AVRが読取専用エミュレート）。SFFパスワード(0/Finisar)は無効。
- 隠しmailbox `0x58/0x5A` は **NACK**（ハンドラ無効）。有効化条件は内部EEPROM `0x01FF==0x55`（起動時にRAM `0x0336` へ複製、`cpi 0x55` で判定 ＝ ファームで確認済み）。
- ファームに **SPM命令なし** ＝ I2C/シリアルのブートローダ無し。**再書込みはISP(SPI)のみ**。

→ 結論：**内蔵ATmega328のEEPROMをISPで直接書く**。これなら0x55ゲートの鶏卵問題を完全に回避できる。

---

## 1. 全体像

```
                      ┌─ I2Cモード: SFP pin4/5 = SDA/SCL (現状のゲートウェイ)
 Pico (RP2040) ──┤
                      └─ ISPモード: pin4=MOSI pin5=SCK pin9=MISO pin15=RESET (新規)
```

Picoファームに「ISPモード」を追加し、同じUSB-HID(64B)枠でISP用コマンドを足す。
ホスト側(Python)がAVR ISPプロトコルを実装して、EEPROMの吸出し/書込み/照合を行う。

SCK/MOSI は**現状のI2C配線(GPIO12/13)を流用**できる（SFP pin4=SDA=MOSI, pin5=SCL=SCK）。
追加で必要な物理配線は **MISOとRESETの2本だけ**。

---

## 2. 配線（専用ボード: RP2040 ↔ SFPエッジコネクタ）

**VCC/GND 以外は「SFPピン番号 = RP2040 GPIO番号」**で結線した専用ボード。

| 信号 | RP2040 GPIO | SFPピン | 備考 |
|---|---|---|---|
| MOSI | **GP4** | pin4 | I2C bus0 SDA と共用（データ線） |
| SCK  | **GP5** | pin5 | I2C bus0 SCL と共用（クロック線） |
| MISO | **GP9** | pin9 | 入力。成否確認用（無くてもブラインド書込可） |
| /RESET | **GP15** | pin15 | active-low、ISP中ずっとLow |
| VCC 3.3V | 3V3(OUT) | pin16 | モジュール給電 |
| GND | GND | pin1 | コモングラウンド必須 |

> SDA↔MOSI(GP4) / SCL↔SCK(GP5) は役割が一致するので相乗り可。通常時 PB3/PB4/PB5 は
> Hi-Z 入力（ファームで DDRB=0 を確認済み）なので I2C 動作と衝突しない。
> ファームは ISP モードで GP4/5 をビットバングに切替え、ISP_END で I2C(bus0=4/5) に戻す。

ISP突入時は **先にRESETをLow** にしてからSPIを叩く。RESET Lowの間AVRは全ポートをHi-Zにするので、
SCK/MOSIがI2Cプルアップと衝突しない。

---

## 3. ISP突入シーケンス（AVRシリアルプログラミング）

1. SCK = Low（アイドル）にしておく
2. /RESET = Low（GPIO15 Low）
3. 20ms以上待つ
4. **Programming Enable** を送信：`0xAC 0x53 0x00 0x00`
   - 3バイト目の応答エコーが `0x53` なら突入成功。違えばRESETをトグルしてリトライ。
5. 終了時：/RESET = High に戻す（GPIO15開放）→ AVR通常起動 → I2Cに復帰

SCK周波数は **ターゲットクロックの1/4未満**。SFPのAVRは内蔵8MHz想定なので **100–250kHz** で安全側。
Picoのビットバングで十分。

---

## 4. 使うAVR ISPコマンド（4バイト/コマンド, MISO上の応答を読む）

| 操作 | バイト列 | 戻り |
|---|---|---|
| Programming Enable | `AC 53 00 00` | 3バイト目に`53`エコー |
| Read Signature | `30 00 0x 00` (x=0/1/2) | ATmega328=`1E 95 14` / 328P=`1E 95 0F` |
| Read Lock bits | `58 00 00 00` | lockバイト |
| Read Fuse(low/high/ext) | `50 00 00 00` / `58 08 00 00` / `50 08 00 00` | 各fuse |
| **Read EEPROM** 1byte | `A0 00 ad 00` | data（ad=8bit, 1KBなら上位は別途） |
| **Write EEPROM** 1byte | `C0 00 ad dd` | — (書込後 約4ms待つ or RDY/BSYポーリング `F0 00 00 00`) |
| Chip Erase | `AC 80 00 00` | フラッシュ＋EEPROM全消去（**通常は使わない**） |

> ATmega328のEEPROMは1KB(アドレス10bit)。`A0/C0` の上位アドレスビットは2バイト目に載る形式に注意
> （ホスト側で `addr` を 16bit として上位/下位に展開）。バイト単位R/Wで十分（ページ書込みは任意の最適化）。

---

## 5. Picoファーム拡張のインタフェース（device-src/src/main.cpp）

既存の64B HID枠（seq/cmd/...）にISP系を追加。I2Cコマンド(0x00–0x03)はそのまま。

| `cmd` | 名前 | OUT引数 | 動作 / IN応答 |
|---|---|---|---|
| `0x10` | ISP_BEGIN | — | GPIO12/13をWireから切離しSPIビットバングに切替、RESET Low、Programming Enable送信。`data[0]`=成功可否、`data[1..3]`=signature |
| `0x11` | ISP_XFER | `len`=4, `data[6..9]`=4バイト | 4バイトSPIトランザクションを実行し、4バイトの応答を `data[0..3]` で返す（汎用。EEPROM R/Wも全部これで表現） |
| `0x12` | ISP_END | — | RESET High、GPIO12/13をWire(I2C)に復帰 |

設計ポイント：
- **モード排他**：ISP_BEGIN中はI2C(Wire)を `end()`、ISP_END で `Wire.begin()` し直す。GP4/5の機能切替を明示。
- ISP_XFER は薄い「4バイト出して4バイト受ける」だけ。AVR ISPの賢さは全部ホスト側に置く（ファームを単純・汎用に保つ）。
- 速度最適化が要るなら後で `0x13 ISP_READ_EEPROM_BLOCK`（連続n byte吸出し）を足せるが、まずはXFERのみでよい。

---

## 6. ホスト側 Python（tools/avr_isp.py 予定）の責務

`tools/pico_i2c.py` と同じHIDトランスポートを再利用。`Pico` クラスに ISP_BEGIN/XFER/END を足すだけ。

- `enter()/exit()`、`signature()`、`read_lock()/read_fuses()`
- `eeprom_read(addr,n)` / `eeprom_write(addr,bytes)`（XFERの組合せ＋書込後ポーリング）
- `dump_eeprom()` … 1KB全吸出し
- `discover_layout()` … 吸い出したEEPROMと、I2Cで読んだ 0x50/0x51 ダンプ(`pico_i2c.py`)を**バイト照合**して
  「内部EEPROMのどのオフセットが 0x50/0x51 のどこか」を**実測で確定**（下記§7の仮説を裏取り）
- `write_image()` … 改変済み 0x50/0x51 像を対応オフセットへ書込み → 再吸出しでベリファイ → I2C再読込で最終確認

---

## 7. 内部EEPROMレイアウト仮説（ISP吸出しで確定する）

`0x01FF` がゲート＝A2ページ末尾、という事実から：

| 内部EEPROM | I2C側 | 根拠 |
|---|---|---|
| `0x000–0x0FF` | A0 (0x50) 0x00–0xFF | ― |
| `0x100–0x1FF` | A2 (0x51) 0x00–0xFF | `0x1FF`=ゲート=A2末尾と整合 / mailbox 0x5B「128B→A2:0x80-0xFF」とも整合 |
| `0x200–0x3FF` | DDM校正・予備 | ― |

照合で確定後、書きたいのは基本 `0x000–0x0FF`（A0:ベンダ/PN/SN/波長/チェックサム0x3F）と
必要なら `0x100–0x1FF`（A2:0x5F/0x95等チェックサム）。**SN維持クローン**なら 0x44–0x5B を残して上書き。

---

## 8. 作業ワークフロー（実装済み）

1. 専用ボード配線（§2）。pin15→GP15 を RESET として駆動できることを確認。
2. 専用ファームを書込み：`cd device-avago-isp && pio run -t upload`（BOOTSELでマウント）。既存 `device-src` は無改変。
3. `python tools/avr_isp.py` … PING/signature/lock/fuse 確認（ロック時はここで判明）
4. `python tools/avr_isp.py --backup` … EEPROM 1KB全吸出し → `avr_eeprom_backup.bin`（**必須バックアップ**）
5. `python tools/avr_isp.py --set` … `EEPROM[0x01FF]=0x55` を書込み（元値は表示されるので控える）
6. **電源再投入** → `python tools/pico_i2c.py 0` で `0x58` が出れば成功（プログラミングモード）
7. 以降は I2C mailbox(0x58) で EEPROM を読み書き（Web版「Avago Mailbox」タブ or pico_i2c.py 拡張）
8. 仕上げに `0x01FF` を最終値へ（`--restore 0xNN`、またはmailbox）→ 電源再投入で完成

---

## 9. リスクと注意

- **ロックビット**：Cisco/Avago量産品はフラッシュ読出禁止のことあり。ただし `Cisco_AVAGO_10GSC_farm.BIN`(32KB吸出し)が存在＝吸出し元はロック無し。実機もロック無しならEEPROM R/W可。ロック時は §3-4 のsignature/lock読みで判明し、Chip Erase以外で解除不可（Eraseはフラッシュも消える＝モジュール死亡。やらない）。
- **Chip Erase は使わない**（フラッシュ＝AVRファームまで消える）。EEPROMは `C0` で消去不要の上書きが可能。`avr_isp.py` は Chip Erase コマンドを実装していない。
- **必ず先に全EEPROMをバックアップ**（`--backup`）してから書く。
- 電圧は3.3V系で統一。5Vは使わない。
- ISP中はI2C不可（GP4/5を占有）。ISP_BEGIN/ISP_END でフェーズを分ける。
- `avr_isp.py` は既定が**ドライラン**。書込みは `--set` を明示したときだけ。

---

## 10. ファーム & ツール

- **`device-avago-isp/`** … Avago専用ファーム（FW v2.0）。I2Cゲートウェイ＋ISP 3コマンド。**既存 `device-src` は無改変**。
- `tools/avr_isp.py` … ISP ホスト（signature/fuse/lock、EEPROM R/W、`--set`でゲート書込、`--backup`で吸出し）
- `tools/pico_i2c.py` … I2Cダンプ/検証・mailbox前後の確認
- `tools/avr_dis.py` … ファーム逆アセンブラ（ゲート 0x01FF→0x0336 / 0x55 判定の解析に使用済み）
