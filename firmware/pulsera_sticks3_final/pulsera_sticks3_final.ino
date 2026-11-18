/*
 * PULSERA DE ALERTA DE SOMNOLENCIA - M5Stack StickS3 (K150)
 * ---------------------------------------------------------------
 * El puerto Grove del StickS3 usa los pines G9 y G10 (NO G1/G2,
 * que son del Hat2-Bus). El motor U059 va en G9.
 *
 * Mensaje BLE (escritura con respuesta), dos digitos ASCII:
 *   1er caracter: nivel 0-3 (0 = sin vibracion; 1, 2, 3 = alerta)
 *   2do caracter: fuerza 1-5 elegida por el usuario (opcional)
 *   Ejemplo: "23" = nivel 2 con fuerza 3. "2" sigue valiendo (fuerza 3).
 *   Un primer caracter que no sea 0-3 se ignora.
 *
 * RF-08 / HU-03: cada nivel tiene un patron distinto que se intensifica.
 * RF-09 / HU-04: sin mensajes durante 3 s, el motor se detiene.
 *
 * PROBLEMA CON BATERIA (sin powerbank): en nivel 3 la pulsera se
 * desconectaba y volvia a conectarse. Lo mas probable es que el
 * motor pida mucha corriente de golpe, el voltaje caiga y el ESP32
 * se reinicie ("brownout"). Para evitarlo:
 *   - Arranque suave: la potencia sube de a poco (RAMPA_PASO cada 10 ms).
 *   - Nivel 3 con mas pausa y menos potencia maxima que antes.
 *   - La fuerza elegida por el usuario escala todos los niveles.
 *   - Pantalla con menos brillo.
 * La pantalla muestra la causa del ultimo reinicio: si dice
 * "VOLTAJE BAJO", el problema es la alimentacion.
 */

#include <M5Unified.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include <esp_system.h>

#define PIN_MOTOR 9
#define FREQ_PWM  5000
#define RES_PWM   8

#define SERVICE_UUID        "12345678-1234-1234-1234-1234567890ab"
#define CHARACTERISTIC_UUID "abcd1234-5678-90ab-cdef-1234567890ab"

// ---------- Ajustes (probar y cambiar con la pulsera real) ----------
const unsigned long TIMEOUT_MS = 3000;   // RF-09
const int  RAMPA_PASO = 12;              // Subida maxima de PWM cada 10 ms (0 -> 180 en ~150 ms)
const int  PWM_MINIMO = 70;              // Debajo de esto el motor podria no girar
const int  BRILLO_PANTALLA = 60;         // 0-255; menos brillo = menos consumo

// Patron por nivel: {ms encendido, ms apagado, intensidad maxima 0-255}
struct Patron { unsigned long encendido; unsigned long apagado; int intensidad; };
const Patron PATRONES[4] = {
  {0,   0,    0},    // 0: sin vibracion
  {200, 1300, 150},  // 1: pulso corto y espaciado
  {300, 700,  170},  // 2: pulsos mas largos y frecuentes
  {350, 450,  185},  // 3: pulsos seguidos (antes 400/300 a 200: se reiniciaba con bateria)
};

// Fuerza 1-5 elegida en la interfaz: porcentaje de la intensidad del patron.
const int PORCENTAJE_FUERZA[6] = {0, 50, 62, 75, 88, 100};

// ---------- Estado ----------
BLEServer* pServer = NULL;
BLECharacteristic* pCaracteristica = NULL;
volatile bool dispositivoConectado = false;
volatile int nivelActual = 0;
volatile int fuerzaActual = 3;
volatile unsigned long ultimoMensaje = 0;

int nivelPatron = -1;
unsigned long inicioPatron = 0;
int pwmActual = 0;
const char* causaReinicio = "";

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer* pServer) {
    dispositivoConectado = true;
    ultimoMensaje = millis();
  }
  void onDisconnect(BLEServer* pServer) {
    dispositivoConectado = false;
    nivelActual = 0;
    BLEDevice::startAdvertising();
  }
};

class CaracteristicaCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* pCaracteristica) {
    String valor = pCaracteristica->getValue();
    if (valor.length() == 0) return;
    int nivel = valor[0] - '0';
    if (nivel < 0 || nivel > 3) return;          // Mensaje invalido: se ignora
    nivelActual = nivel;
    if (valor.length() >= 2) {
      int fuerza = valor[1] - '0';
      if (fuerza >= 1 && fuerza <= 5) fuerzaActual = fuerza;
    }
    ultimoMensaje = millis();
  }
};

int intensidadObjetivo(int nivel) {
  if (nivelPatron != nivel) {        // Al cambiar de nivel, el patron empieza
    nivelPatron = nivel;             // encendido: respuesta inmediata (RNF-01)
    inicioPatron = millis();
  }
  const Patron& p = PATRONES[nivel];
  if (p.intensidad == 0) return 0;
  unsigned long t = (millis() - inicioPatron) % (p.encendido + p.apagado);
  if (t >= p.encendido) return 0;
  int pwm = p.intensidad * PORCENTAJE_FUERZA[fuerzaActual] / 100;
  return pwm < PWM_MINIMO ? PWM_MINIMO : pwm;
}

void aplicarVibracion(int nivel) {
  int objetivo = intensidadObjetivo(nivel);
  if (objetivo > pwmActual) {
    pwmActual += RAMPA_PASO;           // Arranque suave: evita el pico de corriente
    if (pwmActual > objetivo) pwmActual = objetivo;
  } else {
    pwmActual = objetivo;              // Apagar o bajar es inmediato
  }
  ledcWrite(PIN_MOTOR, pwmActual);
}

const char* textoCausaReinicio() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:  return "Encendido";
    case ESP_RST_BROWNOUT: return "VOLTAJE BAJO";
    case ESP_RST_SW:       return "Por software";
    case ESP_RST_PANIC:    return "Error";
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT:      return "Bloqueo (WDT)";
    default:               return "Otro";
  }
}

void mostrarPantalla(int nivel) {
  M5.Display.setCursor(0, 0);
  M5.Display.fillScreen(BLACK);
  M5.Display.setTextSize(3);
  M5.Display.printf("Nivel: %d\n", nivel);
  M5.Display.setTextSize(2);
  M5.Display.printf("Fuerza: %d/5\n", fuerzaActual);
  M5.Display.println(dispositivoConectado ? "BLE OK" : "Esperando...");
  int bateria = M5.Power.getBatteryLevel();
  if (bateria >= 0) M5.Display.printf("Bat: %d%%\n", bateria);
  M5.Display.setTextSize(1);
  M5.Display.printf("Ult. reinicio: %s\n", causaReinicio);
}

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  causaReinicio = textoCausaReinicio();
  M5.Display.setRotation(1);
  M5.Display.setBrightness(BRILLO_PANTALLA);
  M5.Display.fillScreen(BLACK);

  // Activa el 5V del Grove (confirmado correcto por la doc oficial)
  M5.Power.setExtOutput(true);

  ledcAttach(PIN_MOTOR, FREQ_PWM, RES_PWM);
  ledcWrite(PIN_MOTOR, 0);

  BLEDevice::init("PulseraSomnolencia");
  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new ServerCallbacks());
  BLEService* pServicio = pServer->createService(SERVICE_UUID);
  pCaracteristica = pServicio->createCharacteristic(
      CHARACTERISTIC_UUID, BLECharacteristic::PROPERTY_WRITE);
  pCaracteristica->setCallbacks(new CaracteristicaCallbacks());
  pServicio->start();
  BLEAdvertising* pAdvertising = BLEDevice::getAdvertising();
  pAdvertising->addServiceUUID(SERVICE_UUID);
  pAdvertising->start();

  mostrarPantalla(0);
}

void loop() {
  M5.update();

  if (!dispositivoConectado || millis() - ultimoMensaje > TIMEOUT_MS) {
    nivelActual = 0;                   // RF-09 y desconexion: motor apagado
  }
  aplicarVibracion(nivelActual);

  // La pantalla se redibuja al cambiar algo y cada 10 s (bateria).
  static int ultimoNivel = -1, ultimaFuerza = -1;
  static bool ultimaConexion = false;
  static unsigned long ultimoDibujo = 0;
  if (nivelActual != ultimoNivel || fuerzaActual != ultimaFuerza ||
      dispositivoConectado != ultimaConexion || millis() - ultimoDibujo > 10000) {
    mostrarPantalla(nivelActual);
    ultimoNivel = nivelActual;
    ultimaFuerza = fuerzaActual;
    ultimaConexion = dispositivoConectado;
    ultimoDibujo = millis();
  }
  delay(10);
}
