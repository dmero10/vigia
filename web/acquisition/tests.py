"""Pruebas de M1. Usan una cámara falsa, así que no necesitan una webcam real."""

from unittest import mock

import numpy as np
from django.test import SimpleTestCase

from acquisition.camera import OPEN_READ_ATTEMPTS, Camera, CameraError
from acquisition.validation import (
    FpsMeter,
    has_enough_light,
    mean_brightness,
    meets_min_fps,
)


def make_image(value=100):
    return np.full((480, 640, 3), value, dtype=np.uint8)


class FakeCapture:
    """Imita a cv2.VideoCapture. `reads` indica qué devuelve cada lectura."""

    def __init__(self, opened=True, reads=None):
        self.opened = opened
        self.reads = list(reads) if reads is not None else None
        self.released = False
        self.properties = {}

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.properties[prop] = value
        return True

    def read(self):
        if self.reads is None:
            return True, make_image()
        if self.reads:
            return self.reads.pop(0)
        return False, None

    def release(self):
        self.released = True


def make_camera(capture):
    return Camera(capture_factory=lambda index, backend: capture)


class CameraOpenTests(SimpleTestCase):
    def test_open_failure_raises_and_releases(self):
        capture = FakeCapture(opened=False)
        with self.assertRaisesMessage(CameraError, 'No se pudo abrir'):
            make_camera(capture).open()
        self.assertTrue(capture.released)

    def test_open_without_images_raises_and_releases(self):
        capture = FakeCapture(reads=[])
        camera = make_camera(capture)
        with self.assertRaisesMessage(CameraError, 'no entrega imágenes'):
            camera.open()
        self.assertTrue(capture.released)
        self.assertFalse(camera.is_open)

    def test_open_tolerates_slow_first_image(self):
        reads = [(False, None)] * (OPEN_READ_ATTEMPTS - 1) + [(True, make_image())]
        camera = make_camera(FakeCapture(reads=reads))
        camera.open()
        self.assertTrue(camera.is_open)

    def test_open_twice_creates_one_capture(self):
        factory = mock.Mock(return_value=FakeCapture())
        camera = Camera(capture_factory=factory)
        camera.open()
        camera.open()
        factory.assert_called_once()


class CameraReadTests(SimpleTestCase):
    def test_read_returns_frame(self):
        with make_camera(FakeCapture()) as camera:
            frame = camera.read()
        self.assertEqual(frame.image.shape, (480, 640, 3))

    def test_read_without_open_raises(self):
        with self.assertRaisesMessage(CameraError, 'no está abierta'):
            make_camera(FakeCapture()).read()

    def test_read_failure_raises(self):
        # La primera lectura la consume open(); la segunda ya falla.
        capture = FakeCapture(reads=[(True, make_image()), (False, None)])
        with make_camera(capture) as camera:
            with self.assertRaisesMessage(CameraError, 'dejó de entregar'):
                camera.read()

    def test_read_rejects_empty_image(self):
        empty = np.empty((0, 0, 3), dtype=np.uint8)
        capture = FakeCapture(reads=[(True, make_image()), (True, empty)])
        with make_camera(capture) as camera:
            with self.assertRaises(CameraError):
                camera.read()

    def test_timestamps_strictly_increase_with_frozen_clock(self):
        # Con el reloj detenido, todos los fotogramas caen en el mismo milisegundo.
        with mock.patch('acquisition.camera.time.monotonic', return_value=100.0):
            with make_camera(FakeCapture()) as camera:
                stamps = [camera.read().timestamp_ms for _ in range(5)]
        self.assertEqual(stamps, [0, 1, 2, 3, 4])

    def test_timestamps_follow_the_clock(self):
        times = iter([10.0, 10.5, 11.0])
        with mock.patch('acquisition.camera.time.monotonic', lambda: next(times)):
            with make_camera(FakeCapture()) as camera:
                stamps = [camera.read().timestamp_ms for _ in range(2)]
        self.assertEqual(stamps, [500, 1000])

    def test_context_manager_releases_on_error(self):
        capture = FakeCapture()
        with self.assertRaises(ValueError):
            with make_camera(capture):
                raise ValueError
        self.assertTrue(capture.released)

    def test_close_twice_is_safe(self):
        capture = FakeCapture()
        camera = make_camera(capture)
        camera.open()
        camera.close()
        camera.close()
        self.assertFalse(camera.is_open)


class BrightnessTests(SimpleTestCase):
    def test_mean_brightness_of_uniform_image(self):
        self.assertAlmostEqual(mean_brightness(make_image(100)), 100.0)

    def test_mean_brightness_limits(self):
        self.assertEqual(mean_brightness(make_image(0)), 0.0)
        self.assertEqual(mean_brightness(make_image(255)), 255.0)

    def test_light_threshold_rf02(self):
        # RF-02: se degrada con luz menor que 40, así que 40 todavía es válido.
        self.assertFalse(has_enough_light(39.9))
        self.assertTrue(has_enough_light(40))


class FpsTests(SimpleTestCase):
    def test_fps_threshold_rf01(self):
        self.assertFalse(meets_min_fps(24.9))
        self.assertTrue(meets_min_fps(25))

    def test_zero_until_two_frames(self):
        meter = FpsMeter()
        self.assertEqual(meter.fps, 0.0)
        meter.tick(now=1.0)
        self.assertEqual(meter.fps, 0.0)

    def test_constant_rate(self):
        meter = FpsMeter()
        for i in range(31):
            meter.tick(now=i / 30)
        self.assertAlmostEqual(meter.fps, 30.0)

    def test_window_forgets_old_frames(self):
        meter = FpsMeter(window_seconds=2.0)
        # 1 s a 10 FPS y luego 5 s a 30 FPS: sólo debe reflejar la velocidad reciente.
        for i in range(10):
            meter.tick(now=i / 10)
        for i in range(150):
            meter.tick(now=1.0 + i / 30)
        self.assertAlmostEqual(meter.fps, 30.0)

    def test_same_instant_does_not_divide_by_zero(self):
        meter = FpsMeter()
        meter.tick(now=1.0)
        meter.tick(now=1.0)
        self.assertEqual(meter.fps, 0.0)
