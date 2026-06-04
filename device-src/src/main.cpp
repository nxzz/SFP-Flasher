#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_TinyUSB.h>

// ---------------------------------------------------------------------------
// WebHID <-> I2C ゲートウェイ
//
//   Bus 0 : SDA=IO12 / SCL=IO13 -> i2c0 (Wire)
//   Bus 1 : SDA=IO26 / SCL=IO27 -> i2c1 (Wire1)
//
// 64バイト固定のHID IN/OUTレポートでコマンドをやり取りする汎用ゲートウェイ。
//
// OUTレポート(ブラウザ -> デバイス, 64B):
//   [0] seq    : 任意のシーケンス番号(応答にそのまま返す)
//   [1] cmd    : 0x00 PING / 0x01 READ / 0x02 WRITE / 0x03 SCAN
//   [2] bus    : 0 or 1
//   [3] addr   : 7bit I2Cアドレス
//   [4] reg    : レジスタ/オフセット(READ/WRITEの開始位置)
//   [5] len    : READ=読み出しバイト数 / WRITE=書き込みバイト数
//   [6..]      : WRITEデータ
//
// INレポート(デバイス -> ブラウザ, 64B):
//   [0] seq    : 要求のseqをエコー
//   [1] status : 0x00 OK / 0x01 NACK / 0x02 BADARG / 0x03 TOOBIG
//   [2] len    : 有効データ長
//   [3..]      : データ(READ=読み出し値 / SCAN=見つかったアドレス列 / PING=版数)
// ---------------------------------------------------------------------------

#define REPORT_LEN 64
#define FW_MAJOR   1
#define FW_MINOR   0

// コマンド
enum {
  CMD_PING  = 0x00,
  CMD_READ  = 0x01,
  CMD_WRITE = 0x02,
  CMD_SCAN  = 0x03,
};

// ステータス
enum {
  ST_OK     = 0x00,
  ST_NACK   = 0x01,
  ST_BADARG = 0x02,
  ST_TOOBIG = 0x03,
};

static const uint8_t MAX_READ  = REPORT_LEN - 3; // ヘッダ3バイトを除いた上限(=61)
static const uint8_t MAX_WRITE = REPORT_LEN - 6; // ヘッダ6バイトを除いた上限(=58)

// HIDレポートディスクリプタ: ベンダ定義(usage page 0xFF00)の64バイトIN/OUT
uint8_t const desc_hid_report[] = {
  TUD_HID_REPORT_DESC_GENERIC_INOUT(REPORT_LEN)
};

Adafruit_USBD_HID usb_hid;

// 受信したOUTレポートを保持し、loop()で処理する(I2Cブロッキングをコールバック外へ)
static volatile bool    g_pending = false;
static uint8_t          g_req[REPORT_LEN];

static TwoWire *busPtr(uint8_t bus) {
  return (bus == 1) ? &Wire1 : &Wire;
}

// OUTレポート受信コールバック。データを退避してフラグを立てるだけ。
static void setReportCb(uint8_t report_id, hid_report_type_t report_type,
                        uint8_t const *buffer, uint16_t bufsize) {
  (void)report_id;
  (void)report_type;
  if (g_pending) return; // 直前の応答が未送信なら無視(ブラウザは応答待ちで送る)
  uint16_t n = (bufsize < REPORT_LEN) ? bufsize : REPORT_LEN;
  memset(g_req, 0, sizeof(g_req));
  memcpy(g_req, buffer, n);
  g_pending = true;
}

// I2C: regポインタを書いてからlenバイト読む
static uint8_t i2cRead(uint8_t bus, uint8_t addr, uint8_t reg, uint8_t len, uint8_t *out) {
  TwoWire *w = busPtr(bus);
  w->beginTransmission(addr);
  w->write(reg);
  if (w->endTransmission(true) != 0) return ST_NACK;

  uint8_t got = w->requestFrom((int)addr, (int)len);
  for (uint8_t i = 0; i < got && i < len; i++) {
    out[i] = w->read();
  }
  return (got == len) ? ST_OK : ST_NACK;
}

// I2C: regポインタ + データを1トランザクションで書く
static uint8_t i2cWrite(uint8_t bus, uint8_t addr, uint8_t reg, uint8_t len, const uint8_t *data) {
  TwoWire *w = busPtr(bus);
  w->beginTransmission(addr);
  w->write(reg);
  for (uint8_t i = 0; i < len; i++) w->write(data[i]);
  uint8_t e = w->endTransmission(true);
  delay(6); // EEPROM書き込みサイクル待ち(レジスタ系には無害)
  return (e == 0) ? ST_OK : ST_NACK;
}

// SCAN: 0x08..0x77 を走査し、見つかったアドレスをoutに詰める
static uint8_t i2cScan(uint8_t bus, uint8_t *out, uint8_t maxOut) {
  TwoWire *w = busPtr(bus);
  uint8_t n = 0;
  for (uint8_t a = 0x08; a <= 0x77 && n < maxOut; a++) {
    w->beginTransmission(a);
    if (w->endTransmission(true) == 0) out[n++] = a;
  }
  return n;
}

// 1件のリクエストを処理して応答レポートを送る
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

  if (bus > 1 && cmd != CMD_PING) {
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

      default:
        resp[1] = ST_BADARG;
        break;
    }
  }

  // 応答送信(送れるまで軽く待つ)
  uint32_t t0 = millis();
  while (!usb_hid.ready() && (millis() - t0) < 50) delay(1);
  usb_hid.sendReport(0, resp, REPORT_LEN);
}

void setup() {
  // USB HID初期化(Serialより前に)
  usb_hid.setPollInterval(2);
  usb_hid.setReportDescriptor(desc_hid_report, sizeof(desc_hid_report));
  usb_hid.setReportCallback(NULL, setReportCb);
  usb_hid.begin();

  Serial.begin(115200);

  // I2C 2バスを起動
  Wire.setSDA(12);
  Wire.setSCL(13);
  Wire.begin();
  Wire.setClock(100000);

  Wire1.setSDA(26);
  Wire1.setSCL(27);
  Wire1.begin();
  Wire1.setClock(100000);

  // USBマウント待ち
  uint32_t t0 = millis();
  while (!TinyUSBDevice.mounted() && (millis() - t0) < 3000) delay(10);
  Serial.println("\nWebHID <-> I2C gateway ready (bus0=12/13, bus1=26/27)");
}

void loop() {
  if (g_pending) {
    processRequest(g_req);
    g_pending = false;
  }
}
