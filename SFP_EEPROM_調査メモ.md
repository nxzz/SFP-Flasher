# Avago/Broadcom SFP ファーム解析メモ（avr_flash_dump.bin）

対象: `avr_flash_dump.bin`（32768 byte = 16K word）
目的: i2c から内部 EEPROM を書き換える / **型番(Vendor PN)を変更する**

---

## 1. ハードウェア / ファーム構造（確定）

| 項目 | 結果 |
|---|---|
| MCU | **ATmega328P**（Flash 32KB / 内蔵 EEPROM 1KB） |
| アーキ | `avr-objdump -m avr:5` で正しくデコード（reset = `jmp 0x46fe`） |
| i2c 方式 | **全てビットバンギング**（HW TWI=0xB8–0xBC へのアクセス皆無）。INT0(EIMSK=0x1d/EIFR=0x1c) で SCL 検出 |
| データの真の保管先 | **ATmega328P の内蔵 EEPROM**（avr-libc 標準ドライバを確認） |

### 確認した主要ルーチン
- `0x44c4` = `eeprom_write_byte`（EEARH/EEARL/EEDR→EEMPE→EEPE）
- `0x44d4` / `0x44e4` = `eeprom_read_byte`
- `0x438c` = **i2c マスター書き込み**（start=0x4736, sendbyte=0x419a, ack=0x41e8, stop=0x4224）→ 外部デバイス 0xA0/0xB0 等へ
- `0x4326` = ページ(A2h)シャドウ書き込み API、`0x3cf6` = ホストからの A2h 書き込みハンドラ

### データフロー
```
[ATmega328P 内蔵EEPROM] --(起動/更新時にコピー)--> [外部0xA0 EEPROM = ホストが読むA0h]
        ↑真の保管先                                       ↑被参照コピー
```
内蔵 EEPROM が「正本」。A0h ページは内蔵 EEPROM から書き出される。

---

## 2. 書き込み可否マップ（ホスト i2c 経由）

ホスト書き込みハンドラ(0x3cf6–0x3d96)解析結果：受信バイトは RAM シャドウには反映されるが、
**内蔵 EEPROM への永続化はオフセットを `0x80 ≤ off < 0xF8` でゲート**（`cpi 0x80`/`cpi 0xF8`）。

| 領域 | i2c addr | 書込 | 永続化 | 備考 |
|---|---|---|---|---|
| A2h 0x80–0xF7 | 0x51 | ✅ | ✅ | **ユーザー領域。ノーガードで書ける** |
| A2h 0x00–0x7F / 0xF8–0xFF | 0x51 | △RAMのみ | ❌ | DDM/閾値。リセットで復帰 |
| A0h（ベンダ名/**型番**等） | 0x50 | ホスト書込ハンドラ無し | ❌ | 内蔵EEPROMから書き出される＝実質read-only |

### パスワード
**無し。** SFF-8472 のパスワードオフセット(0x7B–0x7E)比較も4バイトキー照合も存在しない。
ゲートは「オフセット範囲」のみ。

---

## 3. 型番(Vendor PN)変更について ★本命

### 結論: ホスト i2c（前回の選択肢 C 等）では型番は変えられない
- 型番 = **A0h オフセット 0x28–0x37（40–55, 16文字ASCII）** + Rev 0x38–0x3B（SFF-8079）
- 選択肢 C が書けるのは A2h 0x80–0xF7 → **別ページなので無関係**
- A0h にはホスト側スレーブ書込ハンドラが存在しない。仮に外部0xA0 EEPROMを直接書けても、内蔵EEPROMから再コピーされ上書きされる

### 確実な方法: ISP で内蔵 EEPROM を直接書き換える
内蔵 EEPROM → A0h の線形マッピングを確定済み：

> **A0h オフセット N  ↔  内蔵 EEPROM アドレス 0x0300 + N**
> （site1: EEPROM[0x300..0x37F]→A0h[0x00..0x7F]、site2: EEPROM[0x380..0x3FF]→A0h[0x80..0xFF]）

したがって内蔵 EEPROM 上では：

| A0h 内容 | A0h offset | 内蔵EEPROM addr |
|---|---|---|
| Vendor name | 0x14–0x23 | 0x0314–0x0323 |
| **Vendor PN（型番）** | **0x28–0x37** | **0x0328–0x0337** |
| Vendor Rev | 0x38–0x3B | 0x0338–0x033B |
| **CC_BASE チェックサム** | 0x3F | **0x033F** |

#### 手順
```bash
sudo apt install avrdude
# USBasp 等を ATmega328P の ISP端子(MISO/MOSI/SCK/RESET/VCC/GND)へ接続

# 1) バックアップ（必須）
avrdude -c usbasp -p m328p -U eeprom:r:eeprom_backup.bin:r
avrdude -c usbasp -p m328p -U flash:r:flash_backup.bin:r

# 2) eeprom_backup.bin の 0x0328–0x0337 を新しい型番(ASCII, 16byte, 余りは空白0x20)に編集
# 3) CC_BASE 再計算: A0h[0x3F] = (A0h[0x00..0x3E] の総和) & 0xFF
#    = 内蔵EEPROM[0x0300..0x033E] の総和 & 0xFF を [0x033F] に書く
#    ※再計算しないとホスト/NICがモジュールを弾く可能性

# 4) 書き戻し
avrdude -c usbasp -p m328p -U eeprom:w:eeprom_new.bin:r
```
注意: lock bit で保護されていると read/write 不可の場合あり（その時は fuse 確認）。

---

## 4. 実機で i2c 書き込みを試す場合（ユーザー領域のみ）
```bash
sudo apt install i2c-tools
i2cdetect -y <bus>            # 0x50,0x51 確認
i2cdump  -y <bus> 0x51        # バックアップ
i2cset   -y <bus> 0x51 0x80 0xAB   # A2h 0x80–0xF7 のみ永続
i2cget   -y <bus> 0x51 0x80
```

---

## 5. ファーム全体の役割（推定）

reset(0x46fe): SP/フレームポインタ初期化 → 自己チェック(0x4764) → main(0x475e) 無限ループ。
ATmega328P 自身の ADC/USART/SPI は未使用（アナログは外部ICに委譲）。

最頻出コール: `0x438c`(i2cマスター書込) **93回**, `0x44d4`(内蔵EEPROM読) **49回**, 固定小数点演算群(`0x4744`乗算, `0x35c2/0x366a/0x372c`)。

役割まとめ:
1. **ホスト向け i2c スレーブ**(ビットバング, INT0/SCL): A0h/A2h を提供
2. **i2c マスター・オーケストレータ**: 外部アナログ IC を制御
   | 外部addr | 7bit | 回数 | 推定役割 |
   |---|---|---|---|
   | **0xB0** | 0x58 | 96 | アナログ制御IC(レーザ駆動/変調/RX監視)。多数レジスタ設定 |
   | 0xA8 | 0x54 | 12 | 補助デバイス(追加EEPROM/センサ) |
   | 0xA2/0xA0 | 0x51/0x50 | 23/3 | ページEEPROMの被参照コピー書出し |
3. **内蔵EEPROMから較正値読出し** → 0xB0 IC設定 & A0h/A2hページ生成（内蔵EEPROM=正本）
4. **DDM**: 0xB0から温度/電圧/TXbias/TX・RXパワーの生値読込→固定小数点でスケーリング→A2h 0x60-0x6F に書込→閾値比較でアラーム/警告フラグ(0x70+)生成（∴0x00-0x7Fは常時上書き=非永続）
5. **ウォッチドッグ**: `wdr`を98箇所で蹴る

> 一言: **「ホストにSFPとして振る舞うi2cスレーブ」兼「外部アナログIC(0x58)を制御するi2cマスター」で、内蔵EEPROMの較正値でレーザ制御＋DDM監視を行うSFPコントローラ。**

## 6. 改造ファーム：ISP一回で「i2cから型番書換」可能化（実装済み）

方針: **ユーザー領域(A2h 0x80–0xF7)は未使用**なので、そのままの素通しマップで A0h へ振り替える（モードフラグ/コマンド不要、オフセット計算のみ）。

### マッピング
> **A2h(0x51) オフセット (0x80 + k) への i2c 書込 → 内蔵EEPROM[0x300 + k] = A0h[k]**（k=0x00..0x77）

| A0h 内容 | A0h off | 書込先 i2c | (0x51) offset |
|---|---|---|---|
| Vendor name | 0x14–0x23 | 0x51 | 0x94–0xA3 |
| **Vendor PN（型番）** | 0x28–0x37 | 0x51 | **0xA8–0xB7** |
| Vendor Rev | 0x38–0x3B | 0x51 | 0xB8–0xBB |
| **CC_BASE** | 0x3F | 0x51 | **0xBF** |
| CC_EXT | 0x5F | 0x51 | 0xDF |

### パッチ内容（12バイト, `make_patch.py` で生成）
| アドレス | 元 | 改 |
|---|---|---|
| 0x3d80 | `call 0x44c4` | `call 0x4800`（フック） |
| 0x4800 | (空) | `subi r16,0x80` |
| 0x4802 | (空) | `ldi r17,0x03` |
| 0x4804 | (空) | `jmp 0x44c4`（eeprom_write 末尾Jmp） |

出力: `avr_flash_patched.bin`

### 焼き込み（ISP・1回のみ）
```bash
avrdude -c usbasp -p m328p -U flash:r:flash_backup.bin:r     # 退避(必須)
avrdude -c usbasp -p m328p -U eeprom:r:eeprom_backup.bin:r
avrdude -c usbasp -p m328p -U flash:w:avr_flash_patched.bin:r  # 改造ファーム書込
# ※EEPROMは触らない＝現行の型番/較正はそのまま温存
```

### 以降の型番変更（i2cのみ・ISP不要）
1. PN(0x28–0x37, 16byte ASCII, 余りは空白0x20) を i2c 0x51 の 0xA8–0xB7 へ書く
2. **CC_BASE 再計算**: `A0h[0x3F] = Σ A0h[0x00..0x3E] & 0xFF` を 0x51 の 0xBF へ書く（host側で計算）
3. **SFPを抜き挿し（電源再投入）** → 起動時に内蔵EEPROM→A0hが再生成され新型番反映

→ 付属 `recode_pn.sh <bus> "<新型番>"` が PN書込＋CC_BASE再計算を自動化。

### 注意 / フェイルセーフ
- A2h ユーザー領域(0x80–0xF7)は本改造で A0h 編集専用になる（未使用前提）
- CC_BASE 不一致だとホスト/NICがモジュールを弾く可能性 → 必ず再計算
- 失敗しても flash/eeprom バックアップから ISP で復旧可

## 7. 解析環境
- `avr-objdump -D -b binary -m avr:5 avr_flash_dump.bin > fw.asm`
- i2c-tools / avrdude は apt で導入
