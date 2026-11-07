/*
 * PULSERA DE ALERTA DE SOMNOLENCIA - M5Stack StickS3 (K150)
 * ---------------------------------------------------------------
 * CORRECCION CLAVE: el puerto Grove del StickS3 usa los pines
 * G9 y G10 (NO G1/G2, que son del Hat2-Bus). Usamos G9.
 *
 * Motor de vibracion M5Stack U059 en el PUERTO GROVE.
 * Recibe el nivel de riesgo (0-4) por BLE y hace vibrar el motor.
 *
 * RF-08 / HU-03: cada nivel produce un patron de vibracion distinto,
 * que se intensifica al subir de nivel (mas tiempo encendido, menos
 * pausa y mas intensidad). Maximo 210 para evitar reinicios.
 * RF-09 / HU-04: sin mensajes durante 3 s, el motor se detiene.
 */

#include <M5Unified.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// PIN CORREGIDO: el Grove real del StickS3 es G9 (antes probamos
// G1 y G2 por error, que son del header Hat2-Bus).
#define PIN_MOTOR 9
#define FREQ_PWM  5000
#define RES_PWM   8

#define SERVICE_UUID        "12345678-1234-1234-1234-1234567890ab"
#define CHARACTERISTIC_UUID "abcd1234-5678-90ab-cdef-1234567890ab"

BLEServer* pServer = NULL;
BLECharacteristic* pCaracteristica = NULL;
bool dispositivoConectado = false;
int nivelActual = 0;
unsigned long ultimoMensaje = 0;
const unsigned long TIMEOUT_MS = 3000;

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer* pServer) {
    dispositivoConectado = true;
    ultimoMensaje = millis();
  }
  void onDisconnect(BLEServer* pServer) {
    dispositivoConectado = false;
    nivelActual = 0;
    ledcWrite(PIN_MOTOR, 0);
    BLEDevice::startAdvertising();
  }
};

class CaracteristicaCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* pCaracteristica) {
    String valor = pCaracteristica->getValue();
    if (valor.length() > 0) {
      nivelActual = valor[0] - '0';
      if (nivelActual < 0) nivelActual = 0;
      if (nivelActual > 4) nivelActual = 4;
      ultimoMensaje = millis();
    }
  }
};

// Patron por nivel: {ms encendido, ms apagado, intensidad 0-255}
struct Patron { unsigned long encendido; unsigned long apagado; int intensidad; };
const Patron PATRONES[5] = {
  {0,    0,    0},    // 0: sin vibracion
  {200,  1300, 150},  // 1: pulso corto y espaciado
  {300,  700,  175},  // 2: pulsos mas largos y frecuentes
  {400,  300,  200},  // 3: pulsos casi seguidos
  {1000, 0,    210},  // 4: vibracion continua
};
int nivelPatron = -1;
unsigned long inicioPatron = 0;

void aplicarVibracion(int nivel) {
  if (nivel != nivelPatron) {   // al cambiar de nivel, el patron empieza
    nivelPatron = nivel;        // encendido: respuesta inmediata (RNF-01)
    inicioPatron = millis();
  }
  const Patron& p = PATRONES[nivel];
  if (p.intensidad == 0) { ledcWrite(PIN_MOTOR, 0); return; }
  if (p.apagado == 0)    { ledcWrite(PIN_MOTOR, p.intensidad); return; }
  unsigned long t = (millis() - inicioPatron) % (p.encendido + p.apagado);
  ledcWrite(PIN_MOTOR, t < p.encendido ? p.intensidad : 0);
}

void mostrarPantalla(int nivel) {
  M5.Display.setCursor(0, 0);
  M5.Display.fillScreen(BLACK);
  M5.Display.setTextSize(3);
  M5.Display.printf("Nivel: %d\n", nivel);
  M5.Display.setTextSize(2);
  M5.Display.println(dispositivoConectado ? "BLE OK" : "Esperando...");
  M5.Display.println("Pin: G9");
}

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Display.setRotation(1);
  M5.Display.fillScreen(BLACK);

  // Activa el 5V del Grove (confirmado correcto por la doc oficial)
  M5.Power.setExtOutput(true);

  // PWM del motor en G9 (pin real del Grove)
  ledcAttach(PIN_MOTOR, FREQ_PWM, RES_PWM);
  ledcWrite(PIN_MOTOR, 0);

  // BLE
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

  if (dispositivoConectado && (millis() - ultimoMensaje > TIMEOUT_MS)) {
    nivelActual = 0;
  }
  aplicarVibracion(nivelActual);

  static int ultimoMostrado = -1;
  if (nivelActual != ultimoMostrado) {
    mostrarPantalla(nivelActual);
    ultimoMostrado = nivelActual;
  }
  delay(10);
}
