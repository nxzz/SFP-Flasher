#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_TinyUSB.h>

// ---------------------------------------------------------------------------
// Avago 解析/書換用 ゲートウェイ (WebHID <-> I2C + AVR ISP)
//
// device-src の汎用ゲートウェイに、Avago/Broadcom 内蔵 ATmega を SPI で
// シリアルプログラミング(ISP)する機能を足した「専用ボード用」ファーム。
// 既存ファームとは独立。bus0 ピンもこのボードの配線に合わせてある。
//
// 用途(方式A): ISP で内部 EEPROM 0x01FF=0x55 を 1 バイト書く → 電源再投入で
// プログラミングモード起動 → 0x58 隠し mailbox が I2C で生き、EEPROM を書ける。
//
// 専用ボード配線 (VCC/GND 以外は SFPピン番号 = GPIO番号):
//   MOSI = SFP pin4  -> GP4   (I2C bus0 SDA と共用)
//   SCK  = SFP pin5  -> GP5   (I2C bus0 SCL と共用)
//   MISO = SFP pin9  -> GP9
//   RST  = SFP pin15 -> GP15
//   VCC  = SFP pin16 -> 3V3,   GND = SFP pin1
//
// OUT(64B): [0]seq [1]cmd [2]bus [3]addr [4]reg [5]len [6..]data
// IN (64B): [0]seq [1]status [2]len [3..]data
// ---------------------------------------------------------------------------

#define REPORT_LEN 64
#define FW_MAJOR   2
#define FW_MINOR   0   // 2.0: Avago ISP 専用ファーム

// コマンド
enum {
  CMD_PING  = 0x00,
  CMD_READ  = 0x01,
  CMD_WRITE = 0x02,
  CMD_SCAN  = 0x03,
  // AVR ISP (内蔵 ATmega を SPI でシリアルプログラミング)
  CMD_ISP_BEGIN = 0x10,  // RESET Low → Programming Enable。応答 data[0..2]=signature
  CMD_ISP_XFER  = 0x11,  // len バイトを全二重 SPI 転送し、受信 len バイトを返す
  CMD_ISP_END   = 0x12,  // RESET 解放 → I2C(bus0) 復帰
};

// ステータス
enum {
  ST_OK     = 0x00,
  ST_NACK   = 0x01,
  ST_BADARG = 0x02,
  ST_TOOBIG = 0x03,
};

static const uint8_t MAX_READ  = REPORT_LEN - 3; // =61
static const uint8_t MAX_WRITE = REPORT_LEN - 6; // =58

// --- I2C bus0 ピン (このボードの配線。SFP pin4/5 = GP4/GP5) ---
#define BUS0_SDA   4
#define BUS0_SCL   5

// --- ISP 用ピン (RP2040 GPIO = SFP コネクタピン番号) ---
#define ISP_MOSI   4    // SFP pin4  (I2C SDA と共用)
#define ISP_SCK    5    // SFP pin5  (I2C SCL と共用)
#define ISP_MISO   9    // SFP pin9
#define ISP_RST    15   // SFP pin15
#define ISP_DLY_US 5    // SCK 半周期 ≒ 100kHz (ターゲット内蔵8MHz の 1/4 未満)

// HIDレポートディスクリプタ: ベンダ定義(usage page 0xFF00)の64バイトIN/OUT
uint8_t const desc_hid_report[] = {
  TUD_HID_REPORT_DESC_GENERIC_INOUT(REPORT_LEN)
};

Adafruit_USBD_HID usb_hid;

static volatile bool g_pending = false;
static uint8_t       g_req[REPORT_LEN];

static TwoWire *busPtr(uint8_t bus) {
  return (bus == 1) ? &Wire1 : &Wire;
}

static void setReportCb(uint8_t report_id, hid_report_type_t report_type,
                        uint8_t const *buffer, uint16_t bufsize) {
  (void)report_id;
  (void)report_type;
  if (g_pending) return;
  uint16_t n = (bufsize < REPORT_LEN) ? bufsize : REPORT_LEN;
  memset(g_req, 0, sizeof(g_req));
  memcpy(g_req, buffer, n);
  g_pending = true;
}

// ---------------- I2C ----------------
static uint8_t i2cRead(uint8_t bus, uint8_t addr, uint8_t reg, uint8_t len, uint8_t *out) {
  TwoWire *w = busPtr(bus);
  w->beginTransmission(addr);
  w->write(reg);
  if (w->endTransmission(true) != 0) return ST_NACK;
  uint8_t got = w->requestFrom((int)addr, (int)len);
  for (uint8_t i = 0; i < got && i < len; i++) out[i] = w->read();
  return (got == len) ? ST_OK : ST_NACK;
}

static uint8_t i2cWrite(uint8_t bus, uint8_t addr, uint8_t reg, uint8_t len, const uint8_t *data) {
  TwoWire *w = busPtr(bus);
  w->beginTransmission(addr);
  w->write(reg);
  for (uint8_t i = 0; i < len; i++) w->write(data[i]);
  uint8_t e = w->endTransmission(true);
  delay(6);
  return (e == 0) ? ST_OK : ST_NACK;
}

static uint8_t i2cScan(uint8_t bus, uint8_t *out, uint8_t maxOut) {
  TwoWire *w = busPtr(bus);
  uint8_t n = 0;
  for (uint8_t a = 0x08; a <= 0x77 && n < maxOut; a++) {
    w->beginTransmission(a);
    if (w->endTransmission(true) == 0) out[n++] = a;
  }
  return n;
}

// ---------------- AVR ISP ----------------
// SPI mode0 ビットバング。SCK アイドル Low、立上りで MISO を採取(MSB first)。
// 通常時 PB3/PB4/PB5 は Hi-Z 入力なので I2C 線(GP4/GP5)と相乗りできる。
static uint8_t ispByte(uint8_t out) {
  uint8_t in = 0;
  for (int8_t i = 7; i >= 0; i--) {
    digitalWrite(ISP_MOSI, (out >> i) & 1);
    delayMicroseconds(ISP_DLY_US);
    digitalWrite(ISP_SCK, HIGH);
    delayMicroseconds(ISP_DLY_US);
    in = (in << 1) | (digitalRead(ISP_MISO) & 1);
    digitalWrite(ISP_SCK, LOW);
  }
  return in;
}

// Programming Enable まで実施。成功(0x53 エコー)で true
static bool ispBegin() {
  Wire.end();                               // GP4/GP5 を I2C から解放
  pinMode(ISP_MISO, INPUT);
  pinMode(ISP_MOSI, OUTPUT);
  pinMode(ISP_SCK,  OUTPUT);
  pinMode(ISP_RST,  OUTPUT);
  digitalWrite(ISP_MOSI, LOW);
  digitalWrite(ISP_SCK,  LOW);              // 突入前に SCK = Low
  digitalWrite(ISP_RST,  HIGH); delay(1);
  digitalWrite(ISP_RST,  LOW);  delay(25);  // RESET 正パルス → Low 保持(>20ms)
  uint8_t c[4] = {0xAC, 0x53, 0x00, 0x00}, r[4];
  for (uint8_t i = 0; i < 4; i++) r[i] = ispByte(c[i]);
  return r[2] == 0x53;
}

// RESET 解放 → AVR 通常起動 → I2C(bus0) 再初期化
static void ispEnd() {
  pinMode(ISP_RST,  INPUT);                 // モジュール側プルアップで Reset 解除
  pinMode(ISP_MOSI, INPUT);
  pinMode(ISP_SCK,  INPUT);
  Wire.setSDA(BUS0_SDA);
  Wire.setSCL(BUS0_SCL);
  Wire.begin();
  Wire.setClock(100000);
}

// ---------------- リクエスト処理 ----------------
static void processRequest(const uint8_t *req) {
  uint8_t resp[REPORT_LEN];
  memset(resp, 0, sizeof(resp));

  const uint8_t seq  = req[0];
  const uint8_t cmd  = req[1];
  const uint8_t bus  = req[2];
  const uint8_t addr = req[3];
  const uint8_t reg  = req[4];
  const uint8_t len  = req[5];

  resp[0] = seq;
  resp[1] = ST_OK;
  resp[2] = 0;

  if (bus > 1 && cmd != CMD_PING && cmd < 0x10) {
    resp[1] = ST_BADARG;
  } else {
    switch (cmd) {
      case CMD_PING:
        resp[2] = 2;
        resp[3] = FW_MAJOR;
        resp[4] = FW_MINOR;
        break;

      case CMD_READ:
        if (len > MAX_READ) {
          resp[1] = ST_TOOBIG;
        } else {
          resp[1] = i2cRead(bus, addr, reg, len, &resp[3]);
          resp[2] = (resp[1] == ST_OK) ? len : 0;
        }
        break;

      case CMD_WRITE:
        if (len > MAX_WRITE) {
          resp[1] = ST_TOOBIG;
        } else {
          resp[1] = i2cWrite(bus, addr, reg, len, &req[6]);
          resp[2] = 0;
        }
        break;

      case CMD_SCAN:
        resp[2] = i2cScan(bus, &resp[3], MAX_READ);
        break;

      case CMD_ISP_BEGIN: {
        bool ok = ispBegin();
        uint8_t c[4], r[4];
        for (uint8_t i = 0; i < 3; i++) {        // signature 3バイト
          c[0] = 0x30; c[1] = 0x00; c[2] = i; c[3] = 0x00;
          for (uint8_t k = 0; k < 4; k++) r[k] = ispByte(c[k]);
          resp[3 + i] = r[3];
        }
        resp[1] = ok ? ST_OK : ST_NACK;
        resp[2] = 3;
        break;
      }

      case CMD_ISP_XFER:
        if (len > MAX_WRITE) {
          resp[1] = ST_TOOBIG;
        } else {
          for (uint8_t i = 0; i < len; i++) resp[3 + i] = ispByte(req[6 + i]);
          resp[1] = ST_OK;
          resp[2] = len;
        }
        break;

      case CMD_ISP_END:
        ispEnd();
        resp[1] = ST_OK;
        resp[2] = 0;
        break;

      default:
        resp[1] = ST_BADARG;
        break;
    }
  }

  uint32_t t0 = millis();
  while (!usb_hid.ready() && (millis() - t0) < 50) delay(1);
  usb_hid.sendReport(0, resp, REPORT_LEN);
}

void setup() {
  usb_hid.setPollInterval(2);
  usb_hid.setReportDescriptor(desc_hid_report, sizeof(desc_hid_report));
  usb_hid.setReportCallback(NULL, setReportCb);
  usb_hid.begin();

  Serial.begin(115200);

  Wire.setSDA(BUS0_SDA);
  Wire.setSCL(BUS0_SCL);
  Wire.begin();
  Wire.setClock(100000);

  Wire1.setSDA(26);
  Wire1.setSCL(27);
  Wire1.begin();
  Wire1.setClock(100000);

  uint32_t t0 = millis();
  while (!TinyUSBDevice.mounted() && (millis() - t0) < 3000) delay(10);
  Serial.println("\nAvago ISP gateway ready (bus0=4/5, bus1=26/27, ISP=4/5/9/15)");
}

void loop() {
  if (g_pending) {
    processRequest(g_req);
    g_pending = false;
  }
}
