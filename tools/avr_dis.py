#!/usr/bin/env python3
"""Minimal AVR disassembler for analyzing the Cisco/Avago SFP MCU firmware.
Covers the common ATmega instruction subset well enough to read control flow,
I/O register access (TWI/I2C), and EEPROM access.
Usage: python avr_dis.py <bin> [start_word] [end_word]
"""
import sys

def u16(b, i):
    return b[i] | (b[i+1] << 8)

# I/O register names (ATmega88/168/328 style extended I/O for TWI). We map
# both low I/O (in/out, 0x00-0x3f) and data-space addrs (0x20 + io).
IO = {
    0x23:"PINB",0x24:"DDRB",0x25:"PORTB",0x26:"PINC",0x27:"DDRC",0x28:"PORTC",
    0x29:"PIND",0x2a:"DDRD",0x2b:"PORTD",
    0x35:"TIFR0",0x36:"TIFR1",0x37:"TIFR2",
    0x3a:"GPIOR1",0x3b:"GPIOR2",0x3c:"PCIFR",0x3d:"EIFR",0x3e:"EIMSK",0x3f:"GPIOR0",
    0x40:"EECR",0x41:"EEDR",0x42:"EEARL",0x43:"EEARH",0x44:"GTCCR",0x45:"TCCR0A",
    0x46:"TCCR0B",0x47:"TCNT0",0x48:"OCR0A",0x49:"OCR0B",
    0x4c:"SPCR",0x4d:"SPSR",0x4e:"SPDR",0x50:"ACSR",
    0x53:"SMCR",0x54:"MCUSR",0x55:"MCUCR",0x57:"SPMCSR",
    0x5d:"SPL",0x5e:"SPH",0x5f:"SREG",
    0x60:"WDTCSR",0x61:"CLKPR",0x64:"PRR",0x66:"OSCCAL",
    0x68:"PCICR",0x69:"EICRA",0x6b:"PCMSK0",0x6c:"PCMSK1",0x6d:"PCMSK2",
    0x6e:"TIMSK0",0x6f:"TIMSK1",0x70:"TIMSK2",
    0x78:"ADCL",0x79:"ADCH",0x7a:"ADCSRA",0x7b:"ADCSRB",0x7c:"ADMUX",
    0x7e:"DIDR0",0x7f:"DIDR1",
    0x80:"TCCR1A",0x81:"TCCR1B",0x82:"TCCR1C",0x84:"TCNT1L",0x85:"TCNT1H",
    0x86:"ICR1L",0x87:"ICR1H",0x88:"OCR1AL",0x89:"OCR1AH",0x8a:"OCR1BL",0x8b:"OCR1BH",
    0xb0:"TCCR2A",0xb1:"TCCR2B",0xb2:"TCNT2",0xb3:"OCR2A",0xb4:"OCR2B",0xb6:"ASSR",
    0xb8:"TWBR",0xb9:"TWSR",0xba:"TWAR",0xbb:"TWDR",0xbc:"TWCR",0xbd:"TWAMR",
    0xc0:"UCSR0A",0xc1:"UCSR0B",0xc2:"UCSR0C",0xc4:"UBRR0L",0xc5:"UBRR0H",0xc6:"UDR0",
}

def ioname(a, dataspace=False):
    addr = a + 0x20 if not dataspace else a
    return IO.get(addr, None)

def dis(b, start=0, end=None):
    n = len(b)
    if end is None: end = n // 2
    out = []
    i = start * 2
    while i < end * 2 and i + 1 < n:
        w = u16(b, i)
        addr = i // 2
        nextw = u16(b, i+2) if i+2 < n else 0
        size = 1
        s = ".dw 0x%04x" % w

        # 32-bit ops first
        if (w & 0xfe0e) == 0x940c:  # jmp
            k = ((w >> 4) & 0x1f) << 1 | (w & 1)
            k = (k << 16) | nextw
            s = "jmp  0x%05x" % k; size = 2
        elif (w & 0xfe0e) == 0x940e:  # call
            k = ((w >> 4) & 0x1f) << 1 | (w & 1)
            k = (k << 16) | nextw
            s = "call 0x%05x" % k; size = 2
        elif (w & 0xfe0f) == 0x9000:  # lds Rd,k
            d = (w >> 4) & 0x1f
            s = "lds  r%d, 0x%04x" % (d, nextw); size = 2
        elif (w & 0xfe0f) == 0x9200:  # sts k,Rr
            d = (w >> 4) & 0x1f
            nm = IO.get(nextw, None)
            tgt = ("0x%04x" % nextw) + (" ;%s" % nm if nm else "")
            s = "sts  %s, r%d" % (tgt, d); size = 2
        # rjmp / rcall
        elif (w & 0xf000) == 0xc000:
            k = w & 0xfff
            if k >= 0x800: k -= 0x1000
            s = "rjmp 0x%05x" % (addr + 1 + k)
        elif (w & 0xf000) == 0xd000:
            k = w & 0xfff
            if k >= 0x800: k -= 0x1000
            s = "rcall 0x%05x" % (addr + 1 + k)
        # branches (BRBS/BRBC)
        elif (w & 0xf800) == 0xf000 or (w & 0xf800) == 0xf400:
            k = (w >> 3) & 0x7f
            if k >= 0x40: k -= 0x80
            sbit = w & 7
            setclr = (w & 0xfc00) == 0xf000
            names = {0:"cs/lo",1:"eq",2:"mi",3:"vs",4:"lt",5:"hs",6:"ts",7:"ie"}
            cc = {0:"brcs",1:"breq",2:"brmi",3:"brvs",4:"brlt",5:"brhs",6:"brts",7:"brie"} if setclr \
                else {0:"brcc",1:"brne",2:"brpl",3:"brvc",4:"brge",5:"brlo",6:"brtc",7:"brid"}
            s = "%s 0x%05x" % (cc[sbit], addr + 1 + k)
        # ldi
        elif (w & 0xf000) == 0xe000:
            d = ((w >> 4) & 0xf) + 16
            k = ((w >> 4) & 0xf0) | (w & 0xf)
            s = "ldi  r%d, 0x%02x" % (d, k)
        # in / out
        elif (w & 0xf800) == 0xb000:  # in
            d = (w >> 4) & 0x1f
            a = ((w >> 5) & 0x30) | (w & 0xf)
            nm = ioname(a); s = "in   r%d, 0x%02x%s" % (d, a, (" ;%s"%nm if nm else ""))
        elif (w & 0xf800) == 0xb800:  # out
            r = (w >> 4) & 0x1f
            a = ((w >> 5) & 0x30) | (w & 0xf)
            nm = ioname(a); s = "out  0x%02x, r%d%s" % (a, r, (" ;%s"%nm if nm else ""))
        # sbi/cbi/sbic/sbis
        elif (w & 0xff00) == 0x9a00:
            a=(w>>3)&0x1f; bb=w&7; nm=ioname(a); s="sbi  0x%02x, %d%s"%(a,bb,(" ;%s"%nm if nm else ""))
        elif (w & 0xff00) == 0x9800:
            a=(w>>3)&0x1f; bb=w&7; nm=ioname(a); s="cbi  0x%02x, %d%s"%(a,bb,(" ;%s"%nm if nm else ""))
        elif (w & 0xff00) == 0x9b00:
            a=(w>>3)&0x1f; bb=w&7; nm=ioname(a); s="sbis 0x%02x, %d%s"%(a,bb,(" ;%s"%nm if nm else ""))
        elif (w & 0xff00) == 0x9900:
            a=(w>>3)&0x1f; bb=w&7; nm=ioname(a); s="sbic 0x%02x, %d%s"%(a,bb,(" ;%s"%nm if nm else ""))
        # sbrc/sbrs
        elif (w & 0xfe08) == 0xfc00:
            d=(w>>4)&0x1f; bb=w&7; s="sbrc r%d, %d"%(d,bb)
        elif (w & 0xfe08) == 0xfe00:
            d=(w>>4)&0x1f; bb=w&7; s="sbrs r%d, %d"%(d,bb)
        # cpi
        elif (w & 0xf000) == 0x3000:
            d=((w>>4)&0xf)+16; k=((w>>4)&0xf0)|(w&0xf); s="cpi  r%d, 0x%02x"%(d,k)
        # andi/ori/subi/sbci
        elif (w & 0xf000) == 0x7000:
            d=((w>>4)&0xf)+16; k=((w>>4)&0xf0)|(w&0xf); s="andi r%d, 0x%02x"%(d,k)
        elif (w & 0xf000) == 0x6000:
            d=((w>>4)&0xf)+16; k=((w>>4)&0xf0)|(w&0xf); s="ori  r%d, 0x%02x"%(d,k)
        elif (w & 0xf000) == 0x5000:
            d=((w>>4)&0xf)+16; k=((w>>4)&0xf0)|(w&0xf); s="subi r%d, 0x%02x"%(d,k)
        elif (w & 0xf000) == 0x4000:
            d=((w>>4)&0xf)+16; k=((w>>4)&0xf0)|(w&0xf); s="sbci r%d, 0x%02x"%(d,k)
        # 2-reg ALU
        elif (w & 0xfc00) == 0x1c00: s=alu2("adc",w)
        elif (w & 0xfc00) == 0x0c00: s=alu2("add",w)
        elif (w & 0xfc00) == 0x2000: s=alu2("and",w)
        elif (w & 0xfc00) == 0x1400: s=alu2("cp",w)
        elif (w & 0xfc00) == 0x0400: s=alu2("cpc",w)
        elif (w & 0xfc00) == 0x1000: s=alu2("cpse",w)
        elif (w & 0xfc00) == 0x2400: s=alu2("eor",w)
        elif (w & 0xfc00) == 0x2c00: s=alu2("mov",w)
        elif (w & 0xfc00) == 0x2800: s=alu2("or",w)
        elif (w & 0xfc00) == 0x0800: s=alu2("sbc",w)
        elif (w & 0xfc00) == 0x1800: s=alu2("sub",w)
        elif (w & 0xfc00) == 0x9c00: s=alu2("mul",w)
        # movw
        elif (w & 0xff00) == 0x0100:
            d=((w>>4)&0xf)*2; r=(w&0xf)*2; s="movw r%d, r%d"%(d,r)
        # single-reg
        elif (w & 0xfe0f) == 0x9403: s="inc  r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x940a: s="dec  r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x9400: s="com  r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x9401: s="neg  r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x9405: s="asr  r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x9406: s="lsr  r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x9407: s="ror  r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x9402: s="swap r%d"%((w>>4)&0x1f)
        # push/pop
        elif (w & 0xfe0f) == 0x920f: s="push r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x900f: s="pop  r%d"%((w>>4)&0x1f)
        # ld/st X,Y,Z variants (common subset)
        elif (w & 0xfe0f) == 0x900c: s="ld   r%d, X"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x900d: s="ld   r%d, X+"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x900e: s="ld   r%d, -X"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x920c: s="st   X, r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x920d: s="st   X+, r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x920e: s="st   -X, r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x8008: s="ld   r%d, Y"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x9009: s="ld   r%d, Y+"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x900a: s="ld   r%d, -Y"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x8208: s="st   Y, r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x9209: s="st   Y+, r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x920a: s="st   -Y, r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x8000: s="ld   r%d, Z"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x9001: s="ld   r%d, Z+"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x9002: s="ld   r%d, -Z"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x8200: s="st   Z, r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x9201: s="st   Z+, r%d"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x9202: s="st   -Z, r%d"%((w>>4)&0x1f)
        # ldd/std with displacement (Y/Z)
        elif (w & 0xd208) == 0x8008:
            d=(w>>4)&0x1f; q=((w>>8)&0x20)|((w>>7)&0x18)|(w&7)
            base="Z" if (w&8)==0 else "Y"
            if (w&0x0200): s="std  %s+%d, r%d"%(base,q,d)
            else: s="ldd  r%d, %s+%d"%(d,base,q)
        # lpm
        elif w == 0x95c8: s="lpm"
        elif (w & 0xfe0f) == 0x9004: s="lpm  r%d, Z"%((w>>4)&0x1f)
        elif (w & 0xfe0f) == 0x9005: s="lpm  r%d, Z+"%((w>>4)&0x1f)
        # misc no-operand
        elif w == 0x9508: s="ret"
        elif w == 0x9518: s="reti"
        elif w == 0x9588: s="sleep"
        elif w == 0x95a8: s="wdr"
        elif w == 0x9408: s="sec"
        elif w == 0x9478: s="sei"
        elif w == 0x94f8: s="cli"
        elif w == 0x0000: s="nop"
        elif (w & 0xff8f) == 0x9408:
            bset=(w>>4)&7; s="bset %d"%bset
        elif (w & 0xff8f) == 0x9488:
            bclr=(w>>4)&7; s="bclr %d"%bclr

        out.append((addr, w, size, s))
        i += size * 2
    return out

def alu2(name, w):
    d=(w>>4)&0x1f; r=((w>>5)&0x10)|(w&0xf)
    return "%-4s r%d, r%d"%(name,d,r)

if __name__ == "__main__":
    fn = sys.argv[1]
    b = open(fn,"rb").read()
    start = int(sys.argv[2],0) if len(sys.argv)>2 else 0
    end = int(sys.argv[3],0) if len(sys.argv)>3 else None
    for addr,w,size,s in dis(b,start,end):
        print("%05x:\t%04x\t%s" % (addr, w, s))
