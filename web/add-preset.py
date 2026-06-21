#!/usr/bin/env python3
"""SFP/QSFP の EEPROM bin を種別判定して配置し、preset-*.json を更新する。

使い方:
    python add-preset.py <bin> [<bin> ...]
    python add-preset.py --form SFP+ foo.bin          # 種別を手動指定
    python add-preset.py --name "Cisco FOO" bar.bin   # 表示名を手動指定
    python add-preset.py --dry-run *.bin              # 移動も書込もせず判定だけ表示

動作:
  1. bin の SFF-8472(SFP) / SFF-8636(QSFP) を読み、種別(SFP/SFP+/SFP28/QSFP/QSFP28)を自動判定
  2. SFP 系は bin/ へ、QSFP 系は bin/qsfp/ へ bin を移動
  3. ベンダー名 / 型番(PN) を抽出し、該当する preset-*.json の presets に追記
     （同名ファイルが既に登録済みならスキップ）

実 SN は書込時に UI が自動維持するため、bin の SN は問いません。
"""
import argparse, json, shutil, sys
from collections import OrderedDict
from pathlib import Path

WEB = Path(__file__).resolve().parent

# 種別 -> (preset json, bin 配置ディレクトリ)。WEB からの相対。
FORM_INFO = OrderedDict([
    ("SFP",    ("preset-sfp.json",      "bin")),
    ("SFP+",   ("preset-sfp-plus.json", "bin")),
    ("SFP28",  ("preset-sfp28.json",    "bin")),
    ("QSFP",   ("preset-qsfp.json",     "bin/qsfp")),
    ("QSFP28", ("preset-qsfp28.json",   "bin/qsfp")),
])


def classify(b: bytes) -> str:
    """SFF 識別子と公称ビットレートから書込先モジュール種別を判定。index.html と同一ロジック。"""
    ident = b[0]
    if ident in (0x0c, 0x0d):                 # QSFP / QSFP+
        return "QSFP"
    if ident in (0x11, 0x12, 0x18, 0x19, 0x1e):  # QSFP28 / QSFP-DD 等
        return "QSFP28"
    # SFP 系 (識別子 0x03 等) は公称ビットレートで分類
    b12 = b[12]
    b66 = b[66] if len(b) > 66 else 0
    if b12 == 0xff:                           # 拡張BR(byte66, 250Mbps単位)
        return "SFP28" if b66 * 250 >= 20000 else "SFP+"   # >=20G を 25G SFP28
    if b[3] & 0xf0:                           # 10GBASE 互換ビット
        return "SFP+"
    if 0x40 <= b12 <= 0x71:                   # 8G/10G/16G FC 域
        return "SFP+"
    return "SFP"


def _ascii(b: bytes, start: int, length: int) -> str:
    return bytes(b[start:start + length]).decode("ascii", "replace").strip().replace("\x00", "").strip()


def parse_fields(b: bytes, form: str):
    """(vendor, pn, sn) を抽出。SFP は SFF-8472(A0)、QSFP は SFF-8636 のオフセット。"""
    if form.startswith("QSFP"):
        return _ascii(b, 148, 16), _ascii(b, 168, 16), _ascii(b, 196, 16)
    return _ascii(b, 20, 16), _ascii(b, 40, 16), _ascii(b, 68, 16)


def add_one(src: Path, args) -> bool:
    if not src.is_file():
        print(f"  ✗ ファイルがありません: {src}")
        return False
    b = src.read_bytes()
    if len(b) < 96:
        print(f"  ✗ サイズが小さすぎます({len(b)}B): {src}")
        return False

    form = args.form or classify(b)
    json_name, bin_dir = FORM_INFO[form]
    vendor, pn, sn = parse_fields(b, form)
    name = args.name or " ".join(x for x in (vendor, pn) if x) or src.stem

    dst_dir = WEB / bin_dir
    dst = dst_dir / src.name
    rel_file = f"{bin_dir}/{src.name}"

    jp = WEB / json_name
    data = json.loads(jp.read_text(encoding="utf-8"), object_pairs_hook=OrderedDict)
    presets = data.setdefault("presets", [])
    if any(p.get("file") == rel_file for p in presets):
        print(f"  - 既に登録済みのためスキップ: {rel_file}")
        return False

    entry = OrderedDict([
        ("name", name), ("file", rel_file),
        ("vendor", vendor), ("pn", pn), ("note", form),
    ])

    print(f"  → {form:6} {name}")
    print(f"      vendor={vendor!r} pn={pn!r} sn={sn!r}")
    print(f"      move {src}  ->  {rel_file}")
    print(f"      append -> {json_name}")
    if args.dry_run:
        return True

    dst_dir.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.resolve() != src.resolve():
        print(f"      ⚠️ 既存 bin を上書き: {rel_file}")
    if dst.resolve() != src.resolve():
        shutil.move(str(src), str(dst))
    presets.append(entry)
    jp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def main():
    ap = argparse.ArgumentParser(description="bin を種別判定して配置し preset-*.json を更新")
    ap.add_argument("bins", nargs="+", help="追加する .bin のパス")
    ap.add_argument("--form", choices=list(FORM_INFO), help="種別を手動指定(自動判定を上書き)")
    ap.add_argument("--name", help="表示名を手動指定(自動は 'ベンダー 型番')")
    ap.add_argument("--dry-run", action="store_true", help="移動・書込せず判定だけ表示")
    args = ap.parse_args()
    if args.name and len(args.bins) > 1:
        ap.error("--name は bin を1つ指定する場合のみ使えます")

    n = 0
    for path in args.bins:
        print(f"[{path}]")
        if add_one(Path(path), args):
            n += 1
    print(f"\n{'(dry-run) ' if args.dry_run else ''}{n} 件を追加しました。")


if __name__ == "__main__":
    main()
