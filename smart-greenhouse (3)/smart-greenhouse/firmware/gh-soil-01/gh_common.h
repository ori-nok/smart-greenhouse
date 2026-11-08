// gh_common.h - общий сетевой слой узла теплицы (Wi-Fi + MQTT + протокол).
// Один и тот же файл используется во всех трёх проектах Wokwi.
//
// Возможности:
//   * LWT + retained-статус + периодический heartbeat (контроль доступности);
//   * последовательные номера сообщений (seq), boot-id, метки времени (NTP);
//   * буферизация телеметрии в RAM при потере связи и отправка по восстановлении;
//   * удалённая конфигурация (retained-топик config) и команды с подтверждением (ack);
//   * неблокирующая работа: локальная автоматика узла НЕ зависит от связи.
#pragma once
#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <sys/time.h>
#include <time.h>
#if __has_include(<esp_random.h>)
#include <esp_random.h>
#endif
#include <esp_system.h>
#include "gh_logic.h"

// ------------------------------------------------------------------ настройки
// Корень топиков. Должен совпадать с TOPIC_ROOT в .env (см. tools/set_topic_root.py).
#define GH_TOPIC_ROOT "gh-k7q2x9/greenhouse"
#define GH_MQTT_HOST "broker.hivemq.com"
#define GH_MQTT_PORT 1883
#define GH_WIFI_SSID "Wokwi-GUEST"
#define GH_WIFI_PASS ""
#define GH_WIFI_CHANNEL 6

#define GH_MSG_MAX 384         // макс. длина одного сообщения телеметрии
#define GH_BUF_N 24            // сколько сообщений хранить при обрыве связи
#define GH_HEARTBEAT_MS 10000  // период heartbeat

namespace gh {

typedef bool (*CmdHandler)(const char* cmd, const char* payload, char* reason, size_t reasonLen);
typedef void (*CfgHandler)(const char* payload);

class Node {
 public:
  Node(const char* devId, const char* devType, const char* fwVer)
      : dev_(devId), type_(devType), fw_(fwVer), mqtt_(net_) {}

  uint32_t intervalMs = 5000;
  bool autoMode = true;
  uint32_t cfgVer = 0;
  CmdHandler onCommand = nullptr;
  CfgHandler onConfig = nullptr;

  void begin() {
    bootId_ = (uint32_t)esp_random();
    if (bootId_ == 0) bootId_ = 1;
    snprintf(tTele_, sizeof(tTele_), "%s/%s/telemetry", GH_TOPIC_ROOT, dev_);
    snprintf(tStatus_, sizeof(tStatus_), "%s/%s/status", GH_TOPIC_ROOT, dev_);
    snprintf(tEvent_, sizeof(tEvent_), "%s/%s/event", GH_TOPIC_ROOT, dev_);
    snprintf(tCmd_, sizeof(tCmd_), "%s/%s/cmd", GH_TOPIC_ROOT, dev_);
    snprintf(tCmdAck_, sizeof(tCmdAck_), "%s/%s/cmd/ack", GH_TOPIC_ROOT, dev_);
    snprintf(tCfg_, sizeof(tCfg_), "%s/%s/config", GH_TOPIC_ROOT, dev_);
    snprintf(tCfgAck_, sizeof(tCfgAck_), "%s/%s/config/ack", GH_TOPIC_ROOT, dev_);
    snprintf(clientId_, sizeof(clientId_), "%s-%08lx", dev_, (unsigned long)bootId_);
    snprintf(will_, sizeof(will_), "{\"v\":1,\"dev\":\"%s\",\"state\":\"offline\",\"reason\":\"lwt\"}", dev_);

    WiFi.mode(WIFI_STA);
    WiFi.begin(GH_WIFI_SSID, GH_WIFI_PASS, GH_WIFI_CHANNEL);
    mqtt_.setServer(GH_MQTT_HOST, GH_MQTT_PORT);
    mqtt_.setBufferSize(512);
    mqtt_.setKeepAlive(15);
    mqtt_.setSocketTimeout(3);
    mqtt_.setCallback([this](char* topic, uint8_t* payload, unsigned int len) {
      this->onMessage(topic, payload, len);
    });
    Serial.printf("[%s] boot=%lu\n", dev_, (unsigned long)bootId_);
  }

  // Вызывать как можно чаще. Не блокирует дольше таймаутов соединения (~3 с при попытке подключения).
  void loop() {
    uint32_t now = millis();
    bool wifiOk = WiFi.status() == WL_CONNECTED;
    if (wifiOk && !ntpStarted_) {
      configTime(0, 0, "pool.ntp.org", "time.google.com");
      ntpStarted_ = true;
    }
    bool connected = wifiOk && mqtt_.connected();
    if (connected) {
      lastConnectedMs_ = now;
      mqtt_.loop();
      flushBuffer();
      if (now - lastHeartbeat_ >= GH_HEARTBEAT_MS) publishStatus();
    } else if (wifiOk) {
      tryConnect(now);
    }
    wasConnected_ = connected;
  }

  bool connected() { return WiFi.status() == WL_CONNECTED && mqtt_.connected(); }
  uint32_t offlineMs(uint32_t now) { return connected() ? 0 : now - lastConnectedMs_; }
  size_t bufferedCount() const { return ring_.size(); }
  uint32_t bootId() const { return bootId_; }

  // metricsJson - JSON-объект с измерениями. Нет связи -> сообщение уходит в кольцевой буфер (buf=1).
  void publishTelemetry(const char* metricsJson, uint32_t effItvMs) {
    uint32_t s = ++seq_;
    uint64_t ts = nowEpochMs();
    char msg[GH_MSG_MAX];
    bool direct = connected() && ring_.empty();
    int n = build(msg, sizeof(msg), s, ts, !direct, effItvMs, metricsJson);
    if (n <= 0 || n >= (int)sizeof(msg)) {
      oversize_++;
      return;
    }
    if (direct) {
      if (mqtt_.publish(tTele_, msg)) return;
      build(msg, sizeof(msg), s, ts, true, effItvMs, metricsJson);
    }
    ring_.push(msg);
  }

  void publishEvent(const char* type, const char* severity, const char* text) {
    if (!connected()) return;
    char msg[240];
    snprintf(msg, sizeof(msg),
             "{\"v\":1,\"dev\":\"%s\",\"boot\":%lu,\"ts\":%llu,\"type\":\"%s\",\"sev\":\"%s\",\"msg\":\"%s\"}", dev_,
             (unsigned long)bootId_, (unsigned long long)nowEpochMs(), type, severity, text);
    mqtt_.publish(tEvent_, msg);
  }

  static uint64_t nowEpochMs() {
    struct timeval tv;
    gettimeofday(&tv, nullptr);
    if (tv.tv_sec < 1700000000) return 0;  // время ещё не синхронизировано по NTP
    return (uint64_t)tv.tv_sec * 1000ULL + (uint64_t)(tv.tv_usec / 1000);
  }

 private:
  const char* dev_;
  const char* type_;
  const char* fw_;
  WiFiClient net_;
  PubSubClient mqtt_;
  MsgRing<GH_BUF_N, GH_MSG_MAX> ring_;
  uint32_t bootId_ = 1, seq_ = 0, oversize_ = 0, reconnects_ = 0;
  uint32_t lastHeartbeat_ = 0, lastTry_ = 0, backoff_ = 2000, lastConnectedMs_ = 0;
  bool ntpStarted_ = false, wasConnected_ = false;
  char tTele_[96], tStatus_[96], tEvent_[96], tCmd_[96], tCmdAck_[96], tCfg_[96], tCfgAck_[96];
  char clientId_[48], will_[128], rx_[448], lastCmdId_[32] = "";

  int build(char* out, size_t len, uint32_t seq, uint64_t ts, bool buffered, uint32_t itv, const char* m) {
    return snprintf(out, len,
                    "{\"v\":1,\"dev\":\"%s\",\"type\":\"%s\",\"boot\":%lu,\"seq\":%lu,\"ts\":%llu,\"up\":%lu,"
                    "\"buf\":%d,\"drop\":%lu,\"mode\":\"%s\",\"itv\":%lu,\"rssi\":%d,\"m\":%s}",
                    dev_, type_, (unsigned long)bootId_, (unsigned long)seq, (unsigned long long)ts,
                    (unsigned long)millis(), buffered ? 1 : 0, (unsigned long)ring_.dropped,
                    autoMode ? "auto" : "manual", (unsigned long)itv, (int)WiFi.RSSI(), m);
  }

  void tryConnect(uint32_t now) {
    if (now - lastTry_ < backoff_ && lastTry_ != 0) return;
    lastTry_ = now;
    if (mqtt_.connect(clientId_, nullptr, nullptr, tStatus_, 1, true, will_)) {
      uint32_t downMs = (reconnects_ > 0) ? now - lastConnectedMs_ : 0;
      backoff_ = 2000;
      lastConnectedMs_ = now;
      mqtt_.subscribe(tCmd_, 1);
      mqtt_.subscribe(tCfg_, 1);
      publishStatus();
      if (reconnects_ > 0) {
        char t[96];
        snprintf(t, sizeof(t), "link restored: outage_s=%lu buffered=%u", (unsigned long)(downMs / 1000),
                 (unsigned)ring_.size());
        publishEvent("link_restored", "info", t);
      }
      reconnects_++;
      Serial.printf("[%s] MQTT connected\n", dev_);
    } else {
      backoff_ = backoff_ * 2 > 10000 ? 10000 : backoff_ * 2;
    }
  }

  void publishStatus() {
    lastHeartbeat_ = millis();
    char msg[256];
    snprintf(msg, sizeof(msg),
             "{\"v\":1,\"dev\":\"%s\",\"type\":\"%s\",\"fw\":\"%s\",\"state\":\"online\",\"boot\":%lu,\"up\":%lu,"
             "\"rssi\":%d,\"heap\":%lu,\"buf_n\":%u,\"cfg_ver\":%lu}",
             dev_, type_, fw_, (unsigned long)bootId_, (unsigned long)millis(), (int)WiFi.RSSI(),
             (unsigned long)ESP.getFreeHeap(), (unsigned)ring_.size(), (unsigned long)cfgVer);
    mqtt_.publish(tStatus_, msg, true);  // retained
  }

  void flushBuffer() {
    int sent = 0;
    while (!ring_.empty() && sent < 3) {
      if (!mqtt_.publish(tTele_, ring_.front())) break;
      ring_.pop();
      sent++;
    }
  }

  void ack(const char* topic, const char* idKey, const char* id, const char* status, const char* reason) {
    char msg[200];
    snprintf(msg, sizeof(msg), "{\"v\":1,\"dev\":\"%s\",\"%s\":\"%s\",\"status\":\"%s\",\"reason\":\"%s\"}", dev_,
             idKey, id, status, reason ? reason : "");
    mqtt_.publish(topic, msg);
  }

  void onMessage(const char* topic, const uint8_t* payload, unsigned int len) {
    // Копируем сразу: публикация внутри callback перезаписывает буфер PubSubClient.
    if (len == 0 || len >= sizeof(rx_)) return;
    memcpy(rx_, payload, len);
    rx_[len] = 0;
    if (strcmp(topic, tCmd_) == 0) handleCmd(rx_);
    else if (strcmp(topic, tCfg_) == 0) handleCfg(rx_);
  }

  void handleCmd(const char* p) {
    char id[32] = "", cmd[24] = "", reason[40] = "";
    gh::jsonGetStr(p, "cmd_id", id, sizeof(id));
    gh::jsonGetStr(p, "cmd", cmd, sizeof(cmd));
    if (!cmd[0]) {
      ack(tCmdAck_, "cmd_id", id, "rejected", "bad_request");
      return;
    }
    if (id[0] && strcmp(id, lastCmdId_) == 0) {  // повторная доставка QoS 1
      ack(tCmdAck_, "cmd_id", id, "ok", "duplicate");
      return;
    }
    strncpy(lastCmdId_, id, sizeof(lastCmdId_) - 1);
    bool ok;
    if (strcmp(cmd, "ping") == 0) {
      ok = true;
    } else if (strcmp(cmd, "reboot") == 0) {
      ack(tCmdAck_, "cmd_id", id, "ok", "rebooting");
      mqtt_.loop();
      delay(300);
      ESP.restart();
      return;
    } else if (onCommand) {
      ok = onCommand(cmd, p, reason, sizeof(reason));
    } else {
      ok = false;
      strcpy(reason, "unsupported");
    }
    ack(tCmdAck_, "cmd_id", id, ok ? "ok" : "rejected", reason);
  }

  void handleCfg(const char* p) {
    double ver = 0;
    if (!gh::jsonGetNum(p, "cfg_ver", ver)) return;
    uint32_t v = (uint32_t)ver;
    if (v == cfgVer && cfgVer != 0) return;  // уже применено
    double d;
    if (gh::jsonGetNum(p, "interval_ms", d)) intervalMs = (uint32_t)gh::clampf((float)d, 1000.0f, 60000.0f);
    char mode[12];
    if (gh::jsonGetStr(p, "mode", mode, sizeof(mode))) autoMode = strcmp(mode, "manual") != 0;
    if (onConfig) onConfig(p);
    cfgVer = v;
    char msg[160];
    snprintf(msg, sizeof(msg), "{\"v\":1,\"dev\":\"%s\",\"cfg_ver\":%lu,\"status\":\"ok\"}", dev_, (unsigned long)v);
    mqtt_.publish(tCfgAck_, msg);
    Serial.printf("[%s] config v%lu applied\n", dev_, (unsigned long)v);
  }
};

}  // namespace gh
