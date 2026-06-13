#!/usr/bin/env python3
"""Host client for the RP2040(Pico) WebHID<->I2C gateway, in Python.
Mirrors web/index.html's protocol (USB_PROTOCOL.md). For analyzing/verifying
the Avago hidden-mailbox path against real hardware.
"""
import hid, time, sys

VID, PID = 0x2E8A, 0x000A
REPORT_LEN = 64
CMD = dict(PING=0x00, READ=0x01, WRITE=0x02, SCAN=0x03)
ST  = {0:"OK", 1:"NACK", 2:"BADARG", 3:"TOOBIG"}

A0, A2 = 0x50, 0x51

class Pico:
    def __init__(self):
        # find the vendor-defined (usage_page 0xFF00) interface
        path = None
        for d in hid.enumerate(VID, PID):
            if d.get("usage_page", 0) == 0xFF00:
                path = d["path"]; break
        if path is None:
            # fall back: pick interface MI_02 by path hint
            for d in hid.enumerate(VID, PID):
                if b"mi_02" in d["path"].lower() or b"&mi_02" in d["path"].lower():
                    path = d["path"]; break
        if path is None:
            raise RuntimeError("Pico vendor HID interface not found")
        self.h = hid.device()
        self.h.open_path(path)
        self.seq = 1

    def _xfer(self, cmd, bus=0, addr=0, reg=0, length=0, data=b"", timeout=2.0):
        s = self.seq & 0xff; self.seq += 1
        out = bytearray(REPORT_LEN)
        out[0]=s; out[1]=cmd; out[2]=bus; out[3]=addr; out[4]=reg; out[5]=length
        for i,b in enumerate(data[:REPORT_LEN-6]): out[6+i]=b
        self.h.write(b"\x00"+bytes(out))      # report id 0 prefix for Windows
        t0=time.time()
        while time.time()-t0 < timeout:
            r = self.h.read(REPORT_LEN, 200)
            if r and r[0]==s:
                status=r[1]; ln=r[2]; payload=bytes(r[3:3+ln])
                return status, payload
        raise TimeoutError(f"no response cmd={cmd:#x} addr={addr:#x} reg={reg:#x}")

    def ping(self):
        st,p=self._xfer(CMD["PING"]); return st,p
    def scan(self, bus=0):
        st,p=self._xfer(CMD["SCAN"], bus=bus)
        if st: raise IOError("SCAN "+ST.get(st,hex(st)))
        return list(p)
    def read(self, bus, addr, reg, n):
        out=bytearray()
        off=0
        while off<n:
            chunk=min(60, n-off)
            st,p=self._xfer(CMD["READ"], bus=bus, addr=addr, reg=(reg+off)&0xff, length=chunk)
            if st: raise IOError(f"READ@{addr:#x}+{reg+off:#x}: {ST.get(st,hex(st))}")
            out += p[:chunk]; off+=chunk
        return bytes(out)
    def write_raw(self, bus, addr, reg, data):
        """single-transaction write (no page split) — for mailbox cmds"""
        st,p=self._xfer(CMD["WRITE"], bus=bus, addr=addr, reg=reg, length=len(data), data=data)
        return st
    def close(self):
        try: self.h.close()
        except: pass

def hexdump(b, base=0):
    out=[]
    for i in range(0,len(b),16):
        row=b[i:i+16]
        h=" ".join(f"{x:02x}" for x in row).ljust(47)
        a="".join(chr(x) if 0x20<=x<0x7f else "." for x in row)
        out.append(f"{base+i:04x}  {h}  |{a}|")
    return "\n".join(out)

if __name__ == "__main__":
    p=Pico()
    st,ver=p.ping()
    print(f"PING status={ST.get(st,hex(st))} fw={ver[0] if ver else '?'}.{ver[1] if len(ver)>1 else '?'}")
    bus=int(sys.argv[1]) if len(sys.argv)>1 else 0
    found=p.scan(bus)
    print(f"SCAN bus{bus}: " + " ".join(f"0x{a:02x}" for a in found) if found else f"SCAN bus{bus}: (none)")
    for label,addr in (("A0/0x50",0x50),("A2/0x51",0x51),("B0/0x58",0x58),("B4/0x5a",0x5a)):
        mark = "visible" if addr in found else "NOT visible"
        print(f"  {label}: {mark}")
    if 0x50 in found:
        print("\n-- A0 (0x50) first 96 bytes --")
        print(hexdump(p.read(bus,0x50,0,96)))
    p.close()
