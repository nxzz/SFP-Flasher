#!/usr/bin/env python3
"""AVR ISP over the Pico gateway (FW >= 1.1) — Avago書換 方式A の bootstrap.

The Avago/Broadcom SFP serves a read-only emulated EEPROM; the only write path
is its hidden I2C mailbox, gated by internal-EEPROM 0x01FF == 0x55 (read at boot
into RAM 0x0336). This tool ISP-writes that single gate byte so the next power-up
boots into programming mode and the 0x58 mailbox becomes usable over I2C.

Wiring (per board): SCK=GP12, MISO=GP13, MOSI=GP14, RESET=GP15, VCC=3V3, GND.

Usage:
    python avr_isp.py                 # dry run: signature/fuse/lock + read gate
    python avr_isp.py --backup        # also dump full 1KB EEPROM -> avr_eeprom_backup.bin
    python avr_isp.py --set           # write EEPROM[0x01FF]=0x55 (then power-cycle)
    python avr_isp.py --restore 0xNN  # write EEPROM[0x01FF]=0xNN (restore original)
"""
import time, sys
try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows cp932 console で記号が出せるように
except Exception:
    pass
from pico_i2c import Pico, ST

GATE_ADDR = 0x01FF
A0_BASE   = 0x0300        # 内部EEPROM上の A0(0x50) ページ先頭 (実測・チェックサムで確定)
SIGS = {(0x1E, 0x95, 0x0F): "ATmega328P",
        (0x1E, 0x95, 0x14): "ATmega328"}

def fix_a0_checksum(b):
    b = bytearray(b)
    b[0x3F] = sum(b[0x00:0x3F]) & 0xFF   # CC_BASE = Σ0x00..0x3E
    b[0x5F] = sum(b[0x40:0x5F]) & 0xFF   # CC_EXT  = Σ0x40..0x5E
    return bytes(b)

def a0_summary(b):
    a = lambda s, e: bytes(b[s:e + 1]).decode("latin1").strip()
    return f"Vendor={a(20,35)!r} PN={a(40,55)!r} SN={a(68,83)!r} Date={a(84,91)!r}"

class IspPico(Pico):
    # --- gateway ISP framing ---
    def isp_begin(self):
        st, p = self._xfer(0x10, timeout=3.0)
        sig = tuple(p[:3]) if len(p) >= 3 else (0, 0, 0)
        return st == 0, sig

    def isp_xfer(self, b):
        st, p = self._xfer(0x11, length=len(b), data=bytes(b))
        if st:
            raise IOError("ISP_XFER " + ST.get(st, hex(st)))
        return bytes(p[:len(b)])

    def isp_end(self):
        self._xfer(0x12)

    # --- AVR serial-programming primitives (result is byte[3] of the 4-byte cmd) ---
    def lock(self):       return self.isp_xfer([0x58, 0x00, 0x00, 0x00])[3]
    def fuse_low(self):   return self.isp_xfer([0x50, 0x00, 0x00, 0x00])[3]
    def fuse_high(self):  return self.isp_xfer([0x58, 0x08, 0x00, 0x00])[3]
    def fuse_ext(self):   return self.isp_xfer([0x50, 0x08, 0x00, 0x00])[3]

    def ee_read(self, a):
        return self.isp_xfer([0xA0, (a >> 8) & 0x03, a & 0xFF, 0x00])[3]

    def ee_write(self, a, v):
        self.isp_xfer([0xC0, (a >> 8) & 0x03, a & 0xFF, v & 0xFF])
        time.sleep(0.006)            # EEPROM write cycle (tWD_EEPROM ~3.6ms)

    def ee_dump(self, n=1024):
        return bytes(self.ee_read(a) for a in range(n))

    def ee_read_block(self, a, n):
        return bytes(self.ee_read(a + i) for i in range(n))

    def ee_write_block(self, a, data):
        for i, b in enumerate(data):
            self.ee_write(a + i, b)

    def read_a0(self):                       # 0x50 ページ (EEPROM 0x300-0x37F)
        return self.ee_read_block(A0_BASE, 128)

    def write_a0(self, data128):
        self.ee_write_block(A0_BASE, data128)

    # Read Program Memory: low=0x20, high=0x28, word-address。1HIDレポートに
    # 7ワード分(14コマンド=56B)詰めて往復回数を減らす。
    def flash_dump(self, nwords=0x4000, progress=None):
        data = bytearray()
        WPC = 7                              # words per HID xfer (7*8=56 <= 58)
        w = 0
        while w < nwords:
            n = min(WPC, nwords - w)
            cmds = []
            for i in range(n):
                wa = w + i; hi = (wa >> 8) & 0xFF; lo = wa & 0xFF
                cmds += [0x20, hi, lo, 0, 0x28, hi, lo, 0]
            out = self.isp_xfer(cmds)
            for i in range(n):
                data.append(out[i * 8 + 3])  # low byte
                data.append(out[i * 8 + 7])  # high byte
            w += n
            if progress and (w % 0x1000 == 0 or w >= nwords):
                progress(w, nwords)
        return bytes(data)

    # --- フラッシュ書込み (ATmega328: 64ワード=128B/ページ, 256ページ) ---
    PAGE_WORDS = 64
    def chip_erase(self):
        # フラッシュ全消去。EESAVE=0(プログラム済)なら EEPROM は保持される。
        self.isp_xfer([0xAC, 0x80, 0x00, 0x00])
        time.sleep(0.012)

    def _poll_ready(self, timeout=0.05):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if (self.isp_xfer([0xF0, 0x00, 0x00, 0x00])[3] & 1) == 0:
                return
        time.sleep(0.006)

    def flash_write_page(self, page, img):
        base = page * self.PAGE_WORDS               # word address of page
        for i in range(self.PAGE_WORDS):
            wa = base + i
            lo = img[wa * 2]; hi = img[wa * 2 + 1]
            self.isp_xfer([0x40, 0x00, i, lo])      # Load Prog Mem Page (low)
            self.isp_xfer([0x48, 0x00, i, hi])      # Load Prog Mem Page (high)
        self.isp_xfer([0x4C, (base >> 8) & 0xFF, base & 0xFF, 0x00])  # Write Page
        self._poll_ready()

    def flash_write(self, img, progress=None):
        img = bytes(img)
        if len(img) % 2:
            img += b"\xff"
        nwords = len(img) // 2
        npages = (nwords + self.PAGE_WORDS - 1) // self.PAGE_WORDS
        img = img.ljust(npages * self.PAGE_WORDS * 2, b"\xff")
        for pg in range(npages):
            self.flash_write_page(pg, img)
            if progress and (pg % 16 == 0 or pg == npages - 1):
                progress((pg + 1) * self.PAGE_WORDS * 2, npages * self.PAGE_WORDS * 2)


def main():
    args = sys.argv[1:]
    p = IspPico()
    st, ver = p.ping()
    fw = f"{ver[0]}.{ver[1]}" if len(ver) >= 2 else "?"
    print(f"PING {ST.get(st)}  FW v{fw}")
    if len(ver) >= 2 and ver[0] < 2:
        print("!! This needs the Avago ISP firmware (FW v2.x) from device-avago-isp/.")
        print("   The stock gateway (device-src, v1.x) has no ISP. Flash device-avago-isp first.")
        return

    ok, sig = p.isp_begin()
    name = SIGS.get(sig, "UNKNOWN")
    print(f"ISP enable: {'OK' if ok else 'FAILED'}  "
          f"signature={' '.join(f'0x{x:02X}' for x in sig)} ({name})")
    if not ok or sig not in SIGS:
        print("!! Programming enable failed / unknown device.")
        print("   Check: RESET(GP15->pin9) reaches AVR PC6, MOSI/SCK/MISO wiring, module powered.")
        p.isp_end()
        return

    print(f"lock=0x{p.lock():02X}  fuse_lo=0x{p.fuse_low():02X}  "
          f"fuse_hi=0x{p.fuse_high():02X}  fuse_ext=0x{p.fuse_ext():02X}")

    orig = p.ee_read(GATE_ADDR)
    print(f"EEPROM[0x{GATE_ADDR:04X}] (gate) = 0x{orig:02X}"
          + ("  -> already programming-enabled" if orig == 0x55 else ""))

    if "--backup" in args:
        ee = p.ee_dump(1024)
        with open("avr_eeprom_backup.bin", "wb") as f:
            f.write(ee)
        print("EEPROM 1KB dumped -> avr_eeprom_backup.bin (KEEP THIS BACKUP)")

    if "--dump-flash" in args:
        import os
        print("reading flash 32KB via ISP ...")
        fl = p.flash_dump(0x4000, progress=lambda w, n: print(f"  {w*2}/{n*2} bytes", flush=True))
        out = "avr_flash_dump.bin"
        with open(out, "wb") as f:
            f.write(fl)
        print(f"flash -> {out} ({len(fl)} bytes)")
        ref = "Cisco_AVAGO_10GSC_farm.BIN"
        if os.path.exists(ref):
            rb = open(ref, "rb").read()
            if len(rb) == len(fl):
                d = sum(1 for i in range(len(fl)) if fl[i] != rb[i])
                print(f"vs {ref}: " + ("IDENTICAL" if d == 0 else f"{d} bytes differ"))
            else:
                print(f"vs {ref}: size differs ({len(fl)} vs {len(rb)})")

    if "--write-flash" in args or "--restore-flash" in args:
        import os
        flag = "--write-flash" if "--write-flash" in args else "--restore-flash"
        i = args.index(flag)
        path = args[i + 1] if i + 1 < len(args) else None
        if not path or not os.path.exists(path):
            print(f"!! {flag} needs an existing .bin (got {path!r})")
            p.isp_end(); return
        img = open(path, "rb").read()
        print(f"flashing {path} ({len(img)} bytes). EESAVE={'on(EEPROM preserved)' if (p.fuse_high()>>3)&1==0 else 'OFF! EEPROM WILL BE ERASED'}")
        if "--write" not in args:
            print("(dry run — add --write to actually Chip-Erase + program flash)")
        else:
            print("Chip Erase ...")
            p.chip_erase()
            print("programming ...")
            p.flash_write(img, progress=lambda d, n: print(f"  {d}/{n} bytes", flush=True))
            rb = p.flash_dump(min(len(img), 0x8000) // 2)
            d = sum(1 for k in range(min(len(rb), len(img))) if rb[k] != img[k])
            print(f"verify: {len(img)-d}/{len(img)} 一致" + (" OK" if d == 0 else f" 不一致!({d}B)"))
            print(">> 電源再投入して動作確認。EEPROM(校正/A0/ゲート)は保持されています。")

    if "--read-a0" in args:
        a0 = p.read_a0()
        out = "avr_a0_dump.bin"
        with open(out, "wb") as f:
            f.write(a0)
        print(f"A0 page (EEPROM 0x300-0x37F) -> {out}")
        print("  " + a0_summary(a0))

    if "--clone" in args:
        i = args.index("--clone")
        path = args[i + 1] if i + 1 < len(args) else None
        if not path:
            print("!! usage: --clone <a0_image.bin> [--no-keep-sn] [--write]")
            p.isp_end(); return
        src = bytearray(open(path, "rb").read())
        if len(src) < 96:
            print(f"!! source {path} is {len(src)} bytes (need >= 96)")
            p.isp_end(); return
        src = bytearray(src[:128].ljust(128, b"\x00"))
        org = bytearray(p.read_a0())
        keep_sn = "--no-keep-sn" not in args
        merged = bytearray(src)
        if keep_sn:
            merged[68:84] = org[68:84]       # 実SN 0x44-0x53 を維持
            merged[84:92] = org[84:92]       # 製造日 0x54-0x5B を維持
        merged = bytearray(fix_a0_checksum(merged))
        print(f"source : {a0_summary(src)}")
        print(f"current: {a0_summary(org)}")
        print(f"result : {a0_summary(merged)}  (keep_sn={keep_sn})")
        if "--write" in args:
            p.write_a0(merged)
            rb = p.read_a0()
            diff = sum(1 for k in range(128) if rb[k] != merged[k])
            print(f"WRITE EEPROM[0x300] done. verify {128 - diff}/128 "
                  + ("一致 OK" if diff == 0 else f"不一致! ({diff}B)"))
            print(">> 電源再投入して、ホスト/スイッチ側で 0x50 が新IDになっているか確認してください。")
        else:
            print("(dry run — 実書込みは --write を付ける)")

    if "--set" in args:
        p.ee_write(GATE_ADDR, 0x55)
        v = p.ee_read(GATE_ADDR)
        print(f"wrote 0x55 -> readback 0x{v:02X}  {'OK' if v == 0x55 else 'MISMATCH!'}")
        print(f"(original gate value was 0x{orig:02X} — note it to restore later)")
        print(">> Power-cycle the module, then run: python pico_i2c.py 0  and check 0x58 appears.")
    elif "--restore" in args:
        i = args.index("--restore")
        val = int(args[i + 1], 0) if i + 1 < len(args) else orig
        p.ee_write(GATE_ADDR, val)
        v = p.ee_read(GATE_ADDR)
        print(f"wrote 0x{val:02X} -> readback 0x{v:02X}  {'OK' if v == val else 'MISMATCH!'}")
    else:
        print("(dry run — no write. Use --set to enable, --backup to dump, --restore 0xNN to revert)")

    p.isp_end()
    print("ISP released (RESET high) — module running.")


if __name__ == "__main__":
    main()
